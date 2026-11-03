"""
Pricing rule tests (helvetra/backend#140).
Anonymous visitors translate for a limited time before an account is
required, and the API is sold as one plan with a 14-day trial.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.api.dependencies import get_current_user
from app.core.tiers import Tier
from app.main import app
from app.services import stripe_b2b, usage_tracker
from app.services.usage_tracker import anonymous_usage_tracker

DAY = 24 * 60 * 60


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestAnonymousTrial:
    """Translating without an account ends after the trial period."""

    def test_ended_trial_requires_signup(self, client, httpx_mock: HTTPXMock):
        """Once the free period is over, anonymous requests are refused before any model call."""
        with patch.object(
            anonymous_usage_tracker,
            "anonymous_trial_ended",
            new_callable=AsyncMock,
            return_value=True,
        ):
            response = client.post(
                "/api/v1/translate",
                json={"text": "Hello", "source_lang": "en", "target_lang": "de"},
            )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "SIGNUP_REQUIRED"
        assert httpx_mock.get_requests() == []

    def test_ios_header_is_not_affected(self, client, httpx_mock: HTTPXMock):
        """iOS app requests use their own per-IP budget, not the anonymous trial."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "Hallo"}}]})
        with patch.object(
            anonymous_usage_tracker,
            "anonymous_trial_ended",
            new_callable=AsyncMock,
            return_value=True,
        ) as trial:
            response = client.post(
                "/api/v1/translate",
                json={"text": "Hello", "source_lang": "en", "target_lang": "de"},
                headers={"X-Client": "helvetra-ios"},
            )

        assert response.status_code == 200
        trial.assert_not_awaited()


class FakeRedis:
    """Minimal Redis with SET NX and GET."""

    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, ex=None):
        """Store a value, keeping an existing one when nx is set."""
        if nx and key in self.data:
            return None
        self.data[key] = str(value).encode()
        return True

    async def get(self, key):
        """Return the stored value, if any."""
        return self.data.get(key)


class TestAnonymousTrialTracker:
    """The trial is counted from the first anonymous use of an IP."""

    @pytest.mark.parametrize("days_later,ended", [
        (0, False),
        (27, False),
        (28, False),
        (29, True),
    ])
    async def test_trial_length(self, days_later, ended):
        """The free period lasts 28 days from first use."""
        tracker = usage_tracker.AnonymousUsageTracker()
        tracker.client = FakeRedis()
        start = 1_800_000_000

        with patch.object(usage_tracker, "datetime") as fake_datetime:
            fake_datetime.now.return_value.timestamp.return_value = start
            assert await tracker.anonymous_trial_ended("1.2.3.4") is False
            fake_datetime.now.return_value.timestamp.return_value = start + days_later * DAY
            assert await tracker.anonymous_trial_ended("1.2.3.4") is ended

    async def test_trial_is_per_ip(self):
        """Another IP starts its own trial."""
        tracker = usage_tracker.AnonymousUsageTracker()
        tracker.client = FakeRedis()
        start = 1_800_000_000

        with patch.object(usage_tracker, "datetime") as fake_datetime:
            fake_datetime.now.return_value.timestamp.return_value = start
            await tracker.anonymous_trial_ended("1.2.3.4")
            fake_datetime.now.return_value.timestamp.return_value = start + 60 * DAY
            assert await tracker.anonymous_trial_ended("1.2.3.4") is True
            assert await tracker.anonymous_trial_ended("5.6.7.8") is False


class TestSingleApiPlan:
    """The API is sold as one plan, Business, with a 14-day trial."""

    def test_checkout_refuses_starter(self, client):
        """Starter can no longer be bought."""
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u")
        try:
            response = client.post("/api/v1/payments/create-b2b-gateway", json={"tier": "starter"})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 400

    def test_business_checkout_has_a_trial(self):
        """The Business checkout session includes the 14-day trial."""
        with (
            patch.object(stripe_b2b.settings, "stripe_secret_key", "sk_test_x"),
            patch.object(stripe_b2b, "resolve_price_by_lookup", return_value="price_x"),
            patch.object(stripe_b2b.stripe.checkout.Session, "create") as create,
        ):
            create.return_value = MagicMock(id="cs_1", url="https://checkout")
            result = stripe_b2b.create_b2b_checkout_session(
                "cus_1", Tier.BUSINESS, "https://ok", "https://cancel"
            )

        assert result.success is True
        assert create.call_args.kwargs["subscription_data"] == {"trial_period_days": 14}
