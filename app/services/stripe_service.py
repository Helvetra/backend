"""
Stripe payment integration service.
Handles checkout session creation, webhook processing, and customer management.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tiers import Tier
from app.models.subscription import (
    Subscription,
    SubscriptionProduct,
    SubscriptionSource,
    SubscriptionStatus,
    SubscriptionTier,
)
from app.models.user import User
from app.services.stripe_b2b import tier_from_price_lookup
from app.services.subscription import get_or_create_subscription, sync_usage_period_limit
from app.services.webhook_events import check_idempotency, record_webhook_event

logger = logging.getLogger(__name__)
settings = get_settings()

# Redirect URLs
BASE_URL = "https://helvetra.ch"
SUCCESS_URL = f"{BASE_URL}/pricing/success"
CANCEL_URL = f"{BASE_URL}/pricing/cancel"


@dataclass
class CheckoutResponse:
    """Response from Stripe checkout session creation."""

    success: bool
    gateway_url: str | None = None
    error: str | None = None


async def get_or_create_stripe_customer(
    db: AsyncSession, user: User
) -> str:
    """Get existing Stripe customer ID or create a new one."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    stripe.api_key = settings.stripe_secret_key

    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
    )

    user.stripe_customer_id = customer.id
    await db.flush()

    logger.info(f"Created Stripe customer {customer.id} for user {user.id}")
    return customer.id


async def create_checkout_session(
    db: AsyncSession,
    user: User,
    billing_period: str,
) -> CheckoutResponse:
    """Create a Stripe Checkout Session and return the URL."""
    if not settings.stripe_secret_key:
        logger.error("Stripe secret key not configured")
        return CheckoutResponse(success=False, error="Payment system not configured")

    stripe.api_key = settings.stripe_secret_key

    # Map billing period to Stripe Price ID
    price_id = (
        settings.stripe_price_monthly_id
        if billing_period == "monthly"
        else settings.stripe_price_yearly_id
    )

    if not price_id:
        logger.error(f"No Stripe price configured for {billing_period}")
        return CheckoutResponse(success=False, error="Price not configured")

    try:
        customer_id = await get_or_create_stripe_customer(db, user)

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
            customer_update={"address": "auto"},
            payment_method_types=["card"],
        )

        logger.info(f"Created Stripe checkout session {session.id} for user {user.id}")
        return CheckoutResponse(success=True, gateway_url=session.url)

    except stripe.StripeError as e:
        logger.error(f"Stripe API error: {e}")
        return CheckoutResponse(success=False, error="Payment service error")


def verify_webhook_signature(payload: bytes, sig_header: str) -> stripe.Event | None:
    """Verify Stripe webhook signature and return the event object."""
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
        return event
    except stripe.SignatureVerificationError:
        logger.warning("Invalid Stripe webhook signature")
        return None
    except ValueError:
        logger.warning("Invalid Stripe webhook payload")
        return None


def _detect_b2b_tier_from_subscription(stripe_sub: Any) -> Tier | None:
    """
    Inspect a Stripe subscription's line items to determine whether it
    represents a B2B Starter, B2B Business, or consumer subscription.
    Returns the B2B Tier if found, None for consumer.
    """
    for item in stripe_sub.items.data:
        price = getattr(item, "price", None)
        lookup_key = getattr(price, "lookup_key", None) if price else None
        if lookup_key:
            tier = tier_from_price_lookup(lookup_key)
            if tier:
                return tier
    return None


def _first_recurring_item(stripe_sub: Any) -> Any:
    """
    Pick the first non-metered subscription item for period dates.
    The metered item exists for overage tracking; its period dates match
    the base subscription, but we prefer the licensed base item explicitly.
    """
    for item in stripe_sub.items.data:
        price = getattr(item, "price", None)
        recurring = getattr(price, "recurring", None) if price else None
        usage_type = getattr(recurring, "usage_type", None) if recurring else None
        if usage_type != "metered":
            return item
    return stripe_sub.items.data[0]


_B2B_TIER_TO_DB_TIER = {
    Tier.STARTER: SubscriptionTier.STARTER,
    Tier.BUSINESS: SubscriptionTier.BUSINESS,
}


async def _handle_checkout_completed(
    db: AsyncSession, event_data: Any
) -> bool:
    """Activate consumer PRO or B2B subscription after successful checkout."""
    session = event_data.object
    customer_id = session.customer
    stripe_sub_id = session.subscription

    if not customer_id:
        logger.warning("No customer ID in checkout session")
        return False

    # Find user by Stripe customer ID
    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        logger.warning(f"No user found for Stripe customer {customer_id}")
        return False

    # Retrieve subscription details from Stripe to inspect line items
    stripe.api_key = settings.stripe_secret_key
    stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
    b2b_tier = _detect_b2b_tier_from_subscription(stripe_sub)
    sub_item = _first_recurring_item(stripe_sub)

    if b2b_tier is not None:
        # B2B subscription (Starter or Business)
        subscription = await get_or_create_subscription(
            db, user.id, SubscriptionProduct.B2B
        )
        new_tier = _B2B_TIER_TO_DB_TIER[b2b_tier]
        product_label = "B2B"
    else:
        # Consumer Helvetra+ subscription
        subscription = await get_or_create_subscription(db, user.id)
        new_tier = SubscriptionTier.PRO
        product_label = "consumer"

    old_tier = subscription.tier
    subscription.tier = new_tier
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.source = SubscriptionSource.STRIPE
    subscription.external_id = stripe_sub_id
    subscription.current_period_start = datetime.fromtimestamp(
        sub_item.current_period_start, tz=timezone.utc
    )
    subscription.current_period_end = datetime.fromtimestamp(
        sub_item.current_period_end, tz=timezone.utc
    )

    if old_tier != new_tier:
        await sync_usage_period_limit(db, user.id, new_tier)

    logger.info(
        f"Activated {product_label} {new_tier.value} subscription for user "
        f"{user.id} via Stripe checkout"
    )
    return True


async def _handle_trial_will_end(db: AsyncSession, event_data: Any) -> bool:
    """
    Email the customer that their B2B trial ends in ~3 days so they
    can upgrade, cancel, or do nothing as they prefer. The webhook
    layer already deduplicates events by event_id, so we'll only
    send once per Stripe event delivery.
    """
    stripe_sub = event_data.object
    customer_id = getattr(stripe_sub, "customer", None)
    sub_id = getattr(stripe_sub, "id", "<unknown>")

    if not customer_id:
        logger.warning(f"trial_will_end event has no customer for sub {sub_id}")
        return True

    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        logger.warning(
            f"trial_will_end: no user found for Stripe customer {customer_id}"
        )
        return True

    # Import lazily to avoid pulling the SMTP service into module-import
    # time during webhook handler registration.
    from app.services.email import email_service

    sent = email_service.send_b2b_trial_ending_email(user.email)
    logger.info(
        "Stripe trial_will_end: emailed user %s (sub %s) → %s",
        user.id,
        sub_id,
        "ok" if sent else "skipped (SMTP not configured or send failed)",
    )
    return True


async def _handle_invoice_payment_succeeded(
    db: AsyncSession, event_data: Any
) -> bool:
    """Renew subscription on successful invoice payment."""
    invoice = event_data.object
    stripe_sub_id = invoice.subscription

    if not stripe_sub_id:
        return True  # One-off invoice, not subscription-related

    # Find subscription by external_id
    result = await db.execute(
        select(Subscription).where(
            Subscription.external_id == stripe_sub_id,
            Subscription.source == SubscriptionSource.STRIPE,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        logger.warning(f"No subscription found for Stripe sub {stripe_sub_id}")
        return False

    # Update period dates from the Stripe subscription
    stripe.api_key = settings.stripe_secret_key
    stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
    sub_item = stripe_sub.items.data[0]

    subscription.status = SubscriptionStatus.ACTIVE
    subscription.current_period_start = datetime.fromtimestamp(
        sub_item.current_period_start, tz=timezone.utc
    )
    subscription.current_period_end = datetime.fromtimestamp(
        sub_item.current_period_end, tz=timezone.utc
    )

    logger.info(f"Renewed subscription {subscription.id} via Stripe invoice")
    return True


async def _handle_invoice_payment_failed(
    db: AsyncSession, event_data: Any
) -> bool:
    """Mark subscription as past due on failed payment."""
    invoice = event_data.object
    stripe_sub_id = invoice.subscription

    if not stripe_sub_id:
        return True

    result = await db.execute(
        select(Subscription).where(
            Subscription.external_id == stripe_sub_id,
            Subscription.source == SubscriptionSource.STRIPE,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        logger.warning(f"No subscription found for failed invoice, sub {stripe_sub_id}")
        return False

    subscription.status = SubscriptionStatus.PAST_DUE
    logger.info(f"Marked subscription {subscription.id} as past_due")
    return True


async def _handle_subscription_deleted(
    db: AsyncSession, event_data: Any
) -> bool:
    """
    Handle a Stripe subscription cancellation. For consumer subscriptions,
    downgrade to FREE. For B2B subscriptions, mark CANCELLED so API key
    auth rejects further calls but the historic tier record is preserved.
    """
    stripe_sub = event_data.object
    stripe_sub_id = stripe_sub.id

    result = await db.execute(
        select(Subscription).where(
            Subscription.external_id == stripe_sub_id,
            Subscription.source == SubscriptionSource.STRIPE,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        logger.warning(f"No subscription found for deleted Stripe sub {stripe_sub_id}")
        return False

    if subscription.product == SubscriptionProduct.B2B:
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.external_id = None
        logger.info(
            f"Cancelled B2B {subscription.tier.value} subscription "
            f"{subscription.id} (Stripe sub deleted)"
        )
        return True

    old_tier = subscription.tier
    subscription.tier = SubscriptionTier.FREE
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.source = None
    subscription.external_id = None
    subscription.current_period_start = None
    subscription.current_period_end = None

    if old_tier != SubscriptionTier.FREE:
        await sync_usage_period_limit(db, subscription.user_id, SubscriptionTier.FREE)

    logger.info(f"Downgraded subscription {subscription.id} to FREE (Stripe sub deleted)")
    return True


async def process_webhook(db: AsyncSession, event: Any) -> dict[str, Any]:
    """Route Stripe webhook events to the appropriate handler."""
    event_id = event.id
    event_type = event.type
    event_data = event.data

    if not event_id:
        return {"success": False, "error": "Missing event ID"}

    # Check idempotency
    existing = await check_idempotency(db, "stripe", event_id)
    if existing and existing.processed:
        logger.info(f"Duplicate Stripe webhook event: {event_id}")
        return {"success": True, "message": "Already processed"}

    # Record event for audit (convert Stripe object to dict for JSON storage)
    payload_dict = event_data.object.to_dict() if event_data else {}
    webhook_event = existing or await record_webhook_event(
        db, "stripe", event_id, event_type, payload_dict
    )

    try:
        if event_type == "checkout.session.completed":
            success = await _handle_checkout_completed(db, event_data)
        elif event_type == "invoice.payment_succeeded":
            success = await _handle_invoice_payment_succeeded(db, event_data)
        elif event_type == "invoice.payment_failed":
            success = await _handle_invoice_payment_failed(db, event_data)
        elif event_type == "customer.subscription.deleted":
            success = await _handle_subscription_deleted(db, event_data)
        elif event_type == "customer.subscription.trial_will_end":
            success = await _handle_trial_will_end(db, event_data)
        else:
            logger.info(f"Unhandled Stripe event type: {event_type}")
            success = True

        webhook_event.processed = success
        await db.flush()

        return {"success": success, "message": f"Processed {event_type} event"}

    except Exception as e:
        logger.exception(f"Error processing Stripe webhook: {e}")
        webhook_event.error = str(e)
        await db.flush()
        return {"success": False, "error": str(e)}


async def cancel_stripe_subscription(external_id: str) -> bool:
    """Cancel a Stripe subscription. Returns True if cancelled successfully."""
    if not settings.stripe_secret_key:
        logger.error("Stripe secret key not configured")
        return False

    stripe.api_key = settings.stripe_secret_key

    try:
        stripe.Subscription.cancel(external_id)
        logger.info(f"Cancelled Stripe subscription {external_id}")
        return True
    except stripe.StripeError as e:
        logger.error(f"Error cancelling Stripe subscription {external_id}: {e}")
        return False
