"""
Subscription management service.
Handles tier limits, usage tracking, and credit operations.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tiers import Tier, get_tier_config
from app.models.subscription import (
    CreditBalance,
    CreditTransaction,
    Subscription,
    SubscriptionProduct,
    SubscriptionStatus,
    SubscriptionTier,
    UsagePeriod,
)


@dataclass
class UsageStatus:
    """Current usage status for a user."""

    product: SubscriptionProduct
    tier: SubscriptionTier
    status: SubscriptionStatus
    characters_used: int
    characters_limit: int
    credits_remaining: int
    can_translate: bool
    period_start: datetime | None
    period_end: datetime | None


async def get_or_create_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    product: SubscriptionProduct = SubscriptionProduct.CONSUMER,
) -> Subscription:
    """Get existing subscription or create a default one for the given product."""
    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.product == product,
        )
    )
    subscription = result.scalar_one_or_none()

    if subscription is None:
        default_tier = (
            SubscriptionTier.BUSINESS if product == SubscriptionProduct.B2B
            else SubscriptionTier.FREE
        )
        subscription = Subscription(
            user_id=user_id,
            product=product,
            tier=default_tier,
            status=SubscriptionStatus.ACTIVE,
        )
        db.add(subscription)
        await db.flush()

    return subscription


async def get_current_usage_period(db: AsyncSession, user_id: uuid.UUID) -> UsagePeriod | None:
    """Get the active usage period for a user."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(UsagePeriod).where(
            UsagePeriod.user_id == user_id,
            UsagePeriod.period_start <= now,
            UsagePeriod.period_end > now,
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_usage_period(
    db: AsyncSession, user_id: uuid.UUID, tier: SubscriptionTier
) -> UsagePeriod:
    """Get current usage period or create one for the current month."""
    period = await get_current_usage_period(db, user_id)

    if period is None:
        now = datetime.now(timezone.utc)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Calculate end of month
        if now.month == 12:
            period_end = period_start.replace(year=now.year + 1, month=1)
        else:
            period_end = period_start.replace(month=now.month + 1)

        # Map SubscriptionTier to Tier for config lookup
        tier_key = Tier(tier.value)
        config = get_tier_config(tier_key)

        period = UsagePeriod(
            user_id=user_id,
            period_start=period_start,
            period_end=period_end,
            characters_used=0,
            characters_limit=config.period_limit,
        )
        db.add(period)
        await db.flush()

    return period


async def get_credit_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Get current credit balance for a user."""
    result = await db.execute(select(CreditBalance).where(CreditBalance.user_id == user_id))
    balance = result.scalar_one_or_none()
    return balance.balance if balance else 0


async def get_usage_status(db: AsyncSession, user_id: uuid.UUID) -> UsageStatus:
    """Get complete usage status for a user."""
    subscription = await get_or_create_subscription(db, user_id)
    period = await get_or_create_usage_period(db, user_id, subscription.tier)
    credits = await get_credit_balance(db, user_id)

    # User can translate if within limit or has credits
    remaining_in_period = period.characters_limit - period.characters_used
    can_translate = remaining_in_period > 0 or credits > 0

    # Use subscription billing dates if available (e.g. Stripe sets them
    # on checkout/invoice events), otherwise fall back to the rolling
    # monthly usage-period dates.
    billing_start = subscription.current_period_start or period.period_start
    billing_end = subscription.current_period_end or period.period_end

    return UsageStatus(
        product=subscription.product,
        tier=subscription.tier,
        status=subscription.status,
        characters_used=period.characters_used,
        characters_limit=period.characters_limit,
        credits_remaining=credits,
        can_translate=can_translate,
        period_start=billing_start,
        period_end=billing_end,
    )


async def record_usage(db: AsyncSession, user_id: uuid.UUID, characters: int) -> UsageStatus:
    """
    Record character usage, consuming from period allowance first, then credits.
    Returns updated usage status.
    """
    subscription = await get_or_create_subscription(db, user_id)
    period = await get_or_create_usage_period(db, user_id, subscription.tier)

    remaining_in_period = period.characters_limit - period.characters_used

    if characters <= remaining_in_period:
        # Fully covered by period allowance
        period.characters_used += characters
    else:
        # Use remaining period allowance, then credits
        characters_from_credits = characters - remaining_in_period
        period.characters_used = period.characters_limit

        # Deduct from credits
        result = await db.execute(select(CreditBalance).where(CreditBalance.user_id == user_id))
        balance = result.scalar_one_or_none()

        if balance and balance.balance >= characters_from_credits:
            balance.balance -= characters_from_credits

            # Log credit usage
            transaction = CreditTransaction(
                user_id=user_id,
                amount=-characters_from_credits,
                transaction_type="usage",
            )
            db.add(transaction)

    await db.flush()
    return await get_usage_status(db, user_id)


async def add_credits(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: int,
    external_id: str | None = None,
) -> int:
    """
    Add credits to user balance (from top-up purchase).
    Returns new balance.
    """
    result = await db.execute(select(CreditBalance).where(CreditBalance.user_id == user_id))
    balance = result.scalar_one_or_none()

    if balance is None:
        balance = CreditBalance(user_id=user_id, balance=amount)
        db.add(balance)
    else:
        balance.balance += amount

    # Log the transaction
    transaction = CreditTransaction(
        user_id=user_id,
        amount=amount,
        transaction_type="purchase",
        external_id=external_id,
    )
    db.add(transaction)

    await db.flush()
    return balance.balance


async def sync_usage_period_limit(
    db: AsyncSession, user_id: uuid.UUID, tier: SubscriptionTier
) -> None:
    """Update the current usage period's character limit to match the subscription tier."""
    period = await get_current_usage_period(db, user_id)
    if period:
        tier_key = Tier(tier.value)
        config = get_tier_config(tier_key)
        period.characters_limit = config.period_limit


async def update_subscription_tier(
    db: AsyncSession,
    user_id: uuid.UUID,
    tier: SubscriptionTier,
    external_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> Subscription:
    """
    Update user subscription tier (from payment webhook).
    Also updates the usage period limit if tier changes.
    """
    subscription = await get_or_create_subscription(db, user_id)

    old_tier = subscription.tier
    subscription.tier = tier
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.external_id = external_id
    subscription.current_period_start = period_start
    subscription.current_period_end = period_end

    if old_tier != tier:
        await sync_usage_period_limit(db, user_id, tier)

    await db.flush()
    return subscription
