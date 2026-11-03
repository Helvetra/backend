"""
API key management service.
Handles generation, validation, revocation, and rotation of B2B API keys.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tiers import Tier, get_tier_config
from app.models.api_key import ApiKey
from app.models.subscription import Subscription, SubscriptionProduct, SubscriptionStatus

KEY_PREFIX = "hv_live_"


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (full_key, key_prefix, key_hash). The full key is shown once to the user.
    """
    random_part = secrets.token_urlsafe(32)
    full_key = f"{KEY_PREFIX}{random_part}"
    key_prefix = full_key[:12]
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, key_prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    """Hash an API key for lookup."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def has_active_b2b_subscription(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Check if the user has an active B2B subscription."""
    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.product == SubscriptionProduct.B2B,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none() is not None


async def get_b2b_subscription(
    db: AsyncSession, user_id: uuid.UUID
) -> Subscription | None:
    """Return the user's active B2B subscription, if any."""
    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.product == SubscriptionProduct.B2B,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def create_api_key(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> tuple[ApiKey, str]:
    """
    Create a new API key for the user.
    Returns (api_key_record, raw_key). The raw key is only available at creation time.
    """
    # Resolve the user's tier-specific key cap
    subscription = await get_b2b_subscription(db, user_id)
    tier = Tier(subscription.tier.value) if subscription else Tier.BUSINESS
    max_keys = get_tier_config(tier, product="b2b").max_api_keys

    # Check key limit
    count_result = await db.execute(
        select(func.count()).select_from(ApiKey).where(
            ApiKey.user_id == user_id,
            ApiKey.revoked_at.is_(None),
        )
    )
    active_count = count_result.scalar()
    if active_count >= max_keys:
        raise ValueError(
            f"Maximum of {max_keys} active API keys allowed on the {tier.value} tier"
        )

    full_key, key_prefix, key_hash = generate_api_key()

    api_key = ApiKey(
        user_id=user_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    db.add(api_key)
    await db.flush()

    return api_key, full_key


async def list_api_keys(db: AsyncSession, user_id: uuid.UUID) -> list[ApiKey]:
    """List all non-revoked API keys for the user."""
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_api_key(
    db: AsyncSession, user_id: uuid.UUID, key_id: uuid.UUID
) -> ApiKey | None:
    """Revoke an API key. Returns the key if found, None otherwise."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.revoked_at.is_(None),
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        return None

    api_key.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key


async def rotate_api_key(
    db: AsyncSession, user_id: uuid.UUID, key_id: uuid.UUID
) -> tuple[ApiKey, str] | None:
    """Revoke an existing key and create a new one with the same name."""
    old_key = await revoke_api_key(db, user_id, key_id)
    if old_key is None:
        return None

    new_key, raw_key = await create_api_key(db, user_id, old_key.name)
    return new_key, raw_key


async def resolve_api_key(db: AsyncSession, raw_key: str) -> ApiKey | None:
    """Look up an API key by its raw value. Updates last_used_at timestamp."""
    key_hash = hash_api_key(raw_key)
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.revoked_at.is_(None),
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        api_key.last_used_at = datetime.now(timezone.utc)

    return api_key


async def revoke_all_user_keys(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke all active API keys for a user. Returns count of revoked keys."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.user_id == user_id,
            ApiKey.revoked_at.is_(None),
        )
    )
    keys = list(result.scalars().all())
    now = datetime.now(timezone.utc)
    for key in keys:
        key.revoked_at = now
    await db.flush()
    return len(keys)
