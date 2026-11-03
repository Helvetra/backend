"""
Usage limit tests for signed-in users.
Signed-in users are refused once their monthly quota is used up, before any
model call, and are rate-limited per account instead of skipping rate limits.
See helvetra/backend#139.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.api.dependencies import get_current_user_optional
from app.config import get_settings
from app.core.database import get_db
from app.main import app
from app.services.auth import create_access_token
from app.services.rate_limiter import RateLimiter, RateLimitResult

settings = get_settings()

TRANSLATE_BODY = {"text": "Hello", "source_lang": "en", "target_lang": "fr"}
PARTIAL_BODY = {
    "segment": "Hello.",
    "context_before": "",
    "context_after": " See you soon.",
    "source_lang": "en",
    "target_lang": "fr",
}


def usage_status(used: int, limit: int, credits: int = 0) -> SimpleNamespace:
    """Build a usage status the way the subscription service reports it."""
    return SimpleNamespace(
        characters_used=used,
        characters_limit=limit,
        credits_remaining=credits,
        can_translate=(limit - used) > 0 or credits > 0,
    )


@pytest.fixture
def user():
    """A signed-in free user, injected in place of JWT resolution."""
    signed_in = SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_current_user_optional] = lambda: signed_in
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    yield signed_in
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


def patch_subscription(status: SimpleNamespace):
    """Patch the route's subscription lookups for a free user with the given usage."""
    free = SimpleNamespace(tier=SimpleNamespace(value="free"))
    return (
        patch(
            "app.api.routes.translate.get_or_create_subscription",
            new_callable=AsyncMock,
            return_value=free,
        ),
        patch(
            "app.api.routes.translate.get_usage_status",
            new_callable=AsyncMock,
            return_value=status,
        ),
        patch("app.api.routes.translate.record_usage", new_callable=AsyncMock),
    )


class TestMonthlyQuotaForSignedInUsers:
    """The monthly quota is enforced before the model is called."""

    @pytest.mark.parametrize("path,body", [
        ("/api/v1/translate", TRANSLATE_BODY),
        ("/api/v1/translate/partial", PARTIAL_BODY),
    ])
    def test_exhausted_quota_is_refused_without_model_call(
        self, client, user, httpx_mock: HTTPXMock, path, body
    ):
        """A user at 100% of the quota gets 429 and Apertus is never called."""
        sub, usage, record = patch_subscription(usage_status(20_000, 20_000))
        with sub, usage, record as recorded:
            response = client.post(path, json=body)

        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "USAGE_LIMIT_EXCEEDED"
        assert error["characters_limit"] == 20_000
        assert httpx_mock.get_requests() == []
        recorded.assert_not_awaited()

    def test_quota_left_translates_and_records_usage(
        self, client, user, httpx_mock: HTTPXMock
    ):
        """A user with quota left is translated and charged as before."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "Bonjour"}}]})
        sub, usage, record = patch_subscription(usage_status(1_000, 20_000))
        with sub, usage, record as recorded:
            response = client.post("/api/v1/translate", json=TRANSLATE_BODY)

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Bonjour"
        recorded.assert_awaited_once()

    def test_credits_allow_translation_past_the_period_limit(
        self, client, user, httpx_mock: HTTPXMock
    ):
        """Purchased credits still let a user translate once the period is used up."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "Bonjour"}}]})
        sub, usage, record = patch_subscription(usage_status(20_000, 20_000, credits=500))
        with sub, usage, record:
            response = client.post("/api/v1/translate", json=TRANSLATE_BODY)

        assert response.status_code == 200


class TestRateLimitForSignedInUsers:
    """Signed-in requests are rate-limited per account, not skipped."""

    def test_signed_in_request_is_counted_per_account(self, client):
        """The middleware counts a signed-in request under the user's own key and limits."""
        user_id = uuid4()
        allowed = RateLimitResult(allowed=True, remaining=59, reset_at=9999999999)
        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=allowed,
        ) as check:
            client.get(
                "/api/v1/languages",
                headers={"Authorization": f"Bearer {create_access_token(user_id)}"},
            )

        check.assert_awaited_once_with(
            f"user:{user_id}",
            minute_limit=settings.rate_limit_user_per_minute,
            day_limit=settings.rate_limit_user_per_day,
        )

    def test_signed_in_request_over_the_limit_is_refused(self, client):
        """A signed-in user over the account rate limit gets 429."""
        refused = RateLimitResult(
            allowed=False, remaining=0, reset_at=9999999999, retry_after=30
        )
        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=refused,
        ):
            response = client.post(
                "/api/v1/translate",
                json=TRANSLATE_BODY,
                headers={"Authorization": f"Bearer {create_access_token(uuid4())}"},
            )

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"


class FakePipeline:
    """Minimal Redis pipeline returning fixed minute and day counts."""

    def __init__(self, counts):
        self.counts = counts

    def get(self, key):
        """Queue a read (counts are fixed)."""

    def incr(self, key):
        """Queue an increment (ignored)."""

    def expire(self, key, seconds):
        """Queue an expiry (ignored)."""

    async def execute(self):
        """Return the fixed counts."""
        return self.counts


class TestDailyLimitReset:
    """The daily limit's reset time is computed correctly on every day of the month."""

    @pytest.mark.parametrize("today", [
        datetime(2026, 10, 31, 14, 0, 0),
        datetime(2026, 12, 31, 23, 59, 30),
        datetime(2027, 2, 28, 9, 0, 0),
    ])
    async def test_day_limit_on_last_day_of_month(self, today):
        """Hitting the daily limit on a month's last day returns 429 data, not a crash."""
        limiter = RateLimiter()
        limiter.client = SimpleNamespace(pipeline=lambda: FakePipeline([b"0", b"500"]))

        with patch("app.services.rate_limiter.datetime") as fake_datetime:
            fake_datetime.now.return_value = today
            result = await limiter.check_rate_limit("1.2.3.4", day_limit=500)

        assert result.allowed is False
        assert 0 < result.retry_after <= 86_400
