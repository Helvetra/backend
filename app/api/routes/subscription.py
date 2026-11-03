"""
Subscription endpoints.
Provides subscription status and usage information.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_client_ip, get_current_user, get_current_user_optional
from app.core.database import get_db
from app.core.tiers import Tier, get_tier_config
from app.models.subscription import (
    Subscription,
    SubscriptionProduct,
    SubscriptionStatus,
    UsagePeriod,
)
from app.models.user import User
from app.schemas.subscription import (
    AnonymousUsageResponse,
    AppleVerifyRequest,
    AppleVerifyResponse,
    B2BSubscriptionResponse,
    SubscriptionResponse,
    TierLimitsResponse,
    UsageHistoryPoint,
    UsageHistoryResponse,
)
from app.services.subscription import get_or_create_subscription, get_usage_status
from app.services.usage_tracker import anonymous_usage_tracker

router = APIRouter(prefix="/subscription")


@router.get("", response_model=SubscriptionResponse)
async def get_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    """Get current subscription status and usage for the authenticated user."""
    status = await get_usage_status(db, user.id)

    return SubscriptionResponse(
        product=status.product,
        tier=status.tier,
        status=status.status,
        characters_used=status.characters_used,
        characters_limit=status.characters_limit,
        credits_remaining=status.credits_remaining,
        can_translate=status.can_translate,
        period_start=status.period_start,
        period_end=status.period_end,
    )


@router.get("/limits", response_model=TierLimitsResponse)
async def get_tier_limits(
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> TierLimitsResponse:
    """Get tier limits for the current user (or anonymous defaults)."""
    if user:
        subscription = await get_or_create_subscription(db, user.id)
        tier = Tier(subscription.tier.value)
    else:
        tier = Tier.ANONYMOUS

    config = get_tier_config(tier)

    return TierLimitsResponse(
        tier=tier.value,
        max_chars_per_request=config.max_chars_per_request,
        period_limit=config.period_limit,
        period_type=config.period_type,
        formality=config.formality,
    )


@router.get("/b2b", response_model=B2BSubscriptionResponse)
async def get_b2b_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> B2BSubscriptionResponse:
    """Return the authenticated user's B2B subscription details for the dashboard."""
    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.product == SubscriptionProduct.B2B,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription or subscription.status != SubscriptionStatus.ACTIVE:
        return B2BSubscriptionResponse(has_subscription=False)

    tier_enum = Tier(subscription.tier.value)
    tier_config = get_tier_config(tier_enum, product="b2b")
    usage = await get_usage_status(db, user.id)

    # Detect trial period heuristically: new subscriptions have a 14-day
    # Stripe trial, so if the subscription was created within the last
    # 14 days we treat the current period as the trial. This avoids an
    # extra Stripe API call per dashboard load.
    is_trialing = False
    if subscription.created_at:
        trial_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        is_trialing = subscription.created_at > trial_cutoff

    return B2BSubscriptionResponse(
        has_subscription=True,
        tier=subscription.tier.value,
        status=subscription.status.value,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
        is_trialing=is_trialing,
        characters_used=usage.characters_used,
        characters_limit=usage.characters_limit,
        characters_remaining=max(0, usage.characters_limit - usage.characters_used),
        max_chars_per_request=tier_config.max_chars_per_request,
        max_api_keys=tier_config.max_api_keys,
    )


@router.get("/b2b/usage-history", response_model=UsageHistoryResponse)
async def get_b2b_usage_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageHistoryResponse:
    """
    Return the user's recent monthly usage periods for the dashboard chart.

    Returns up to 12 most recent periods, oldest first. No daily granularity:
    the usage_periods table aggregates monthly. Each row is independent of
    consumer vs B2B product since usage_periods is shared per user.
    """
    result = await db.execute(
        select(UsagePeriod)
        .where(UsagePeriod.user_id == user.id)
        .order_by(UsagePeriod.period_start.desc())
        .limit(12)
    )
    rows = list(result.scalars().all())

    # Reverse so the chart reads chronologically (oldest left, newest right).
    rows.reverse()

    return UsageHistoryResponse(
        periods=[
            UsageHistoryPoint(
                period_start=row.period_start,
                period_end=row.period_end,
                characters_used=row.characters_used,
                characters_limit=row.characters_limit,
            )
            for row in rows
        ]
    )


@router.get("/anonymous-usage", response_model=AnonymousUsageResponse)
async def get_anonymous_usage(request: Request) -> AnonymousUsageResponse:
    """Get current usage for anonymous users (tracked by IP)."""
    client_ip = get_client_ip(request)
    usage = await anonymous_usage_tracker.get_usage(client_ip)

    return AnonymousUsageResponse(
        characters_used=usage.characters_used,
        characters_limit=usage.characters_limit,
        characters_remaining=usage.characters_remaining,
        reset_at=usage.reset_at,
    )


@router.post("/apple/verify", response_model=AppleVerifyResponse)
async def verify_apple_transaction(
    request: AppleVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppleVerifyResponse:
    """
    Verify an Apple StoreKit 2 transaction and update subscription.
    Called by iOS app after successful in-app purchase.
    """
    from app.models.subscription import (
        SubscriptionSource,
        SubscriptionStatus,
        SubscriptionTier,
    )
    from app.services.apple_storekit import verify_transaction

    # Verify the signed transaction with Apple
    transaction = await verify_transaction(request.signed_transaction)
    if not transaction:
        return AppleVerifyResponse(
            success=False,
            message="Invalid transaction signature",
        )

    # Check if transaction was already upgraded to a new subscription
    if transaction.is_upgraded:
        return AppleVerifyResponse(
            success=False,
            message="Transaction was upgraded",
        )

    # Map Apple product to tier
    if not transaction.tier:
        return AppleVerifyResponse(
            success=False,
            message="Unknown product ID",
        )

    # Update user's subscription
    subscription = await get_or_create_subscription(db, user.id)

    tier_enum = SubscriptionTier(transaction.tier)
    subscription.tier = tier_enum
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.source = SubscriptionSource.APPLE
    subscription.external_id = transaction.original_transaction_id
    subscription.current_period_start = transaction.purchase_date
    subscription.current_period_end = transaction.expires_date

    await db.commit()

    return AppleVerifyResponse(
        success=True,
        tier=transaction.tier,
        expires_at=transaction.expires_date,
        message="Subscription activated",
    )
