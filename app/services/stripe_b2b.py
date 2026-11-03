"""
B2B-specific Stripe integration.

Separate from the consumer stripe_service module so the two product lines
can evolve independently. Resolves Price objects by lookup key (stable
across price rotations), creates B2B Checkout sessions with metered
billing, and reports per-translation meter events.
"""

import logging
import time
import uuid
from dataclasses import dataclass

import stripe

from app.config import get_settings
from app.core.tiers import Tier

logger = logging.getLogger(__name__)
settings = get_settings()


# --- Lookup-key resolver with in-process cache ---

_PRICE_CACHE_TTL_SECONDS = 300  # 5 min
_price_cache: dict[str, tuple[str, float]] = {}


def resolve_price_by_lookup(lookup_key: str) -> str:
    """
    Return the live Stripe Price ID matching a lookup key.

    Stripe lookup keys let us point at the current active Price by a
    stable name we control, instead of hardcoding auto-generated price
    IDs that change every time we rotate pricing.

    Cached in memory for 5 minutes to avoid an extra Stripe API call
    per checkout.
    """
    if not lookup_key:
        raise RuntimeError("Stripe B2B lookup key not configured")

    cached = _price_cache.get(lookup_key)
    now = time.monotonic()
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL_SECONDS:
        return cached[0]

    if not settings.stripe_secret_key:
        raise RuntimeError("Stripe secret key not configured")

    stripe.api_key = settings.stripe_secret_key
    prices = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    if not prices.data:
        raise RuntimeError(
            f"No active Stripe price found for lookup key '{lookup_key}'. "
            "Check the Stripe Dashboard."
        )

    price_id = prices.data[0].id
    _price_cache[lookup_key] = (price_id, now)
    return price_id


def _clear_price_cache_for_tests() -> None:
    """Test-only hook: drop the cache so tests can assert API calls."""
    _price_cache.clear()


# --- Tier configuration on the B2B side ---


@dataclass(frozen=True)
class B2BCheckoutPriceIds:
    """Lookup keys + resolved price IDs for one B2B tier."""

    tier: Tier
    base_lookup: str
    overage_lookup: str
    trial_days: int  # 0 = no trial


def _price_config(tier: Tier) -> B2BCheckoutPriceIds:
    """Return the lookup-key bundle for a given B2B tier."""
    if tier == Tier.BUSINESS:
        return B2BCheckoutPriceIds(
            tier=Tier.BUSINESS,
            base_lookup=settings.stripe_b2b_business_base_lookup,
            overage_lookup=settings.stripe_b2b_business_overage_lookup,
            trial_days=14,
        )
    raise ValueError(f"Tier {tier} is not a B2B tier")


def tier_from_price_lookup(lookup_key: str) -> Tier | None:
    """
    Map a price lookup key back to its B2B tier. Used by webhook handlers
    to set the right tier on the subscription when checkout completes.
    """
    if not lookup_key:
        return None
    if lookup_key == settings.stripe_b2b_business_base_lookup:
        return Tier.BUSINESS
    return None


# --- Checkout session ---


@dataclass
class B2BCheckoutResponse:
    """Result of creating a B2B Checkout session."""

    success: bool
    gateway_url: str | None = None
    error: str | None = None


def create_b2b_checkout_session(
    customer_id: str,
    tier: Tier,
    success_url: str,
    cancel_url: str,
) -> B2BCheckoutResponse:
    """
    Create a Stripe Checkout session for the B2B subscription. Includes both
    the base recurring price and the metered overage price as line items,
    with a 14-day trial.
    """
    if not settings.stripe_secret_key:
        return B2BCheckoutResponse(success=False, error="Payment system not configured")

    try:
        config = _price_config(tier)
        base_price_id = resolve_price_by_lookup(config.base_lookup)
        overage_price_id = resolve_price_by_lookup(config.overage_lookup)
    except (ValueError, RuntimeError) as e:
        logger.error(f"B2B checkout config error: {e}")
        return B2BCheckoutResponse(success=False, error="Payment system misconfigured")

    stripe.api_key = settings.stripe_secret_key
    subscription_data: dict = {}
    if config.trial_days > 0:
        subscription_data["trial_period_days"] = config.trial_days

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[
                {"price": base_price_id, "quantity": 1},
                # Metered prices are added without a quantity — usage flows
                # through meter events instead.
                {"price": overage_price_id},
            ],
            subscription_data=subscription_data or None,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_update={"address": "auto"},
            payment_method_types=["card"],
        )
    except stripe.StripeError as e:
        logger.error(f"Stripe API error creating B2B checkout: {e}")
        return B2BCheckoutResponse(success=False, error="Payment service error")

    logger.info(
        f"Created B2B checkout session {session.id} for customer "
        f"{customer_id} (tier={tier.value})"
    )
    return B2BCheckoutResponse(success=True, gateway_url=session.url)


# --- Meter event reporting ---


async def report_translation_meter_event(
    stripe_customer_id: str | None,
    characters: int,
    idempotency_key: str | None = None,
) -> None:
    """
    Send a usage event to the Stripe Meter after a successful B2B
    translation. Fire-and-forget: errors are logged but never raised,
    so meter problems can't break the translation response.

    The event payload matches the meter's customer mapping
    (stripe_customer_id) and value mapping (value) configured in the
    Stripe Dashboard.
    """
    if not stripe_customer_id:
        logger.debug("Skipping meter event: customer has no stripe_customer_id")
        return
    if characters <= 0:
        return
    if not settings.stripe_b2b_meter_event_name:
        logger.debug("Skipping meter event: STRIPE_B2B_METER_EVENT_NAME not set")
        return
    if not settings.stripe_secret_key:
        logger.warning("Skipping meter event: STRIPE_SECRET_KEY not set")
        return

    payload = {
        "event_name": settings.stripe_b2b_meter_event_name,
        "payload": {
            "stripe_customer_id": stripe_customer_id,
            "value": str(characters),
        },
    }
    if idempotency_key:
        payload["identifier"] = idempotency_key

    try:
        stripe.api_key = settings.stripe_secret_key
        stripe.billing.MeterEvent.create(**payload)
    except stripe.StripeError as e:
        # Never block the translation response on meter reporting.
        logger.error(
            "Failed to report meter event for customer %s (chars=%d): %s",
            stripe_customer_id,
            characters,
            e,
        )
    except Exception as e:
        logger.exception(f"Unexpected error reporting meter event: {e}")


def generate_meter_idempotency_key(user_id: uuid.UUID, characters: int) -> str:
    """
    Generate a per-translation idempotency key for meter events so
    duplicate sends (network retries) don't double-bill the customer.
    Includes a high-resolution timestamp so successive translations from
    the same user are unique.
    """
    return f"{user_id}:{time.time_ns()}:{characters}"


# --- Customer Portal ---


def create_billing_portal_session(
    customer_id: str,
    return_url: str,
) -> str | None:
    """
    Create a Stripe billing portal session and return its URL.

    The portal lets B2B customers self-manage their subscription
    (update payment method, view invoices, cancel) without us having
    to build that UI. Returns None on configuration or API errors;
    callers should surface a friendly message.
    """
    if not settings.stripe_secret_key:
        logger.error("Stripe secret key not configured for portal session")
        return None

    stripe.api_key = settings.stripe_secret_key
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except stripe.StripeError as e:
        logger.error(f"Stripe API error creating billing portal session: {e}")
        return None

    logger.info(f"Created billing portal session for customer {customer_id}")
    return session.url
