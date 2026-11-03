"""
Centralized tier configuration.
Single source of truth for feature access and limits across all subscription tiers.
"""

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    """All user tiers including anonymous."""

    ANONYMOUS = "anonymous"
    FREE = "free"
    PRO = "pro"
    STARTER = "starter"
    BUSINESS = "business"


@dataclass(frozen=True)
class TierConfig:
    """Configuration for a subscription tier."""

    # Character limits
    max_chars_per_request: int
    period_limit: int  # Monthly for registered users, weekly for anonymous
    period_type: str  # "weekly" or "monthly"

    # Feature flags
    formality: bool  # Can use formal/informal toggle

    # API access
    max_api_keys: int  # Active API keys allowed (0 = no API access)


# Consumer tier configurations (web app, iOS app)
CONSUMER_TIER_CONFIGS: dict[Tier, TierConfig] = {
    Tier.ANONYMOUS: TierConfig(
        max_chars_per_request=400,
        period_limit=1_000,
        period_type="weekly",
        formality=True,
        max_api_keys=0,
    ),
    Tier.FREE: TierConfig(
        max_chars_per_request=500,
        period_limit=5_000,
        period_type="monthly",
        formality=True,
        max_api_keys=0,
    ),
    Tier.PRO: TierConfig(
        max_chars_per_request=5_000,
        period_limit=500_000,
        period_type="monthly",
        formality=True,
        max_api_keys=0,
    ),
}

# B2B tier configuration (public API). There is one plan, Business. Starter
# is no longer offered; any remaining Starter subscription gets the same
# limits so it keeps working.
_BUSINESS_CONFIG = TierConfig(
    max_chars_per_request=10_000,
    period_limit=2_000_000,
    period_type="monthly",
    formality=True,
    max_api_keys=10,
)
B2B_TIER_CONFIGS: dict[Tier, TierConfig] = {
    Tier.STARTER: _BUSINESS_CONFIG,
    Tier.BUSINESS: _BUSINESS_CONFIG,
}

# Combined lookup for backward compatibility
TIER_CONFIGS: dict[Tier, TierConfig] = {**CONSUMER_TIER_CONFIGS, **B2B_TIER_CONFIGS}


def get_tier_config(tier: Tier | str, product: str = "consumer") -> TierConfig:
    """Get configuration for a tier, optionally scoped to a product line."""
    if isinstance(tier, str):
        tier = Tier(tier)

    configs = B2B_TIER_CONFIGS if product == "b2b" else CONSUMER_TIER_CONFIGS
    if tier in configs:
        return configs[tier]

    # Fall back to combined lookup
    return TIER_CONFIGS[tier]


def can_use_formality(tier: Tier | str) -> bool:
    """Check if a tier can use the formality feature."""
    return get_tier_config(tier).formality


def get_max_chars(tier: Tier | str) -> int:
    """Get max characters per request for a tier."""
    return get_tier_config(tier).max_chars_per_request


def get_period_limit(tier: Tier | str) -> int:
    """Get period character limit for a tier."""
    return get_tier_config(tier).period_limit
