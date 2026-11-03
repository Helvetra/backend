"""
Tier configuration tests.
Locks in the published pricing/features so tier values cannot drift unnoticed.
"""

from app.core.tiers import (
    B2B_TIER_CONFIGS,
    CONSUMER_TIER_CONFIGS,
    Tier,
    get_tier_config,
)


class TestB2BTierConfig:
    """The single API plan is part of the public pricing contract (helvetra/backend#140)."""

    def test_business_pricing_inputs(self):
        config = B2B_TIER_CONFIGS[Tier.BUSINESS]
        assert config.period_limit == 2_000_000
        assert config.period_type == "monthly"
        assert config.max_chars_per_request == 10_000
        assert config.max_api_keys == 10
        assert config.formality is True

    def test_legacy_starter_gets_the_business_limits(self):
        """Starter is no longer sold; a leftover Starter row must keep working."""
        assert B2B_TIER_CONFIGS[Tier.STARTER] == B2B_TIER_CONFIGS[Tier.BUSINESS]


class TestConsumerTierConfig:
    """The consumer limits are part of the public pricing contract (helvetra/backend#140)."""

    def test_anonymous_limits(self):
        config = CONSUMER_TIER_CONFIGS[Tier.ANONYMOUS]
        assert (config.max_chars_per_request, config.period_limit) == (400, 1_000)
        assert config.period_type == "weekly"

    def test_free_limits(self):
        config = CONSUMER_TIER_CONFIGS[Tier.FREE]
        assert (config.max_chars_per_request, config.period_limit) == (500, 5_000)

    def test_pro_limits(self):
        config = CONSUMER_TIER_CONFIGS[Tier.PRO]
        assert (config.max_chars_per_request, config.period_limit) == (5_000, 500_000)

    def test_signing_up_never_lowers_the_limits(self):
        """Each step up (anonymous, free, pro) allows at least as much as the one below."""
        anonymous, free, pro = (
            CONSUMER_TIER_CONFIGS[t] for t in (Tier.ANONYMOUS, Tier.FREE, Tier.PRO)
        )
        assert anonymous.max_chars_per_request <= free.max_chars_per_request
        assert free.max_chars_per_request <= pro.max_chars_per_request
        # Anonymous is weekly for at most four weeks; free is monthly.
        assert anonymous.period_limit * 4 <= free.period_limit
        assert free.period_limit <= pro.period_limit


class TestConsumerHasNoApiAccess:
    """Consumer tiers must not grant API key creation."""

    def test_anonymous_no_keys(self):
        assert CONSUMER_TIER_CONFIGS[Tier.ANONYMOUS].max_api_keys == 0

    def test_free_no_keys(self):
        assert CONSUMER_TIER_CONFIGS[Tier.FREE].max_api_keys == 0

    def test_pro_no_keys(self):
        assert CONSUMER_TIER_CONFIGS[Tier.PRO].max_api_keys == 0


class TestGetTierConfig:
    """Product-scoped lookup must return the B2B config for B2B tiers."""

    def test_b2b_lookup_returns_b2b_config(self):
        config = get_tier_config(Tier.BUSINESS, product="b2b")
        assert config.max_api_keys == 10

    def test_string_tier_resolves(self):
        config = get_tier_config("business", product="b2b")
        assert config.max_chars_per_request == 10_000
