"""
Quota tests for unverified iOS app requests.
The iOS header is not verified, so requests carrying it keep the free plan's
per-request size but are counted per IP against its monthly budget.
See helvetra/backend#117.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.main import app
from app.services import usage_tracker
from app.services.usage_tracker import UsageResult, anonymous_usage_tracker

IOS_HEADERS = {"X-Client": "helvetra-ios"}


def body(text: str) -> dict:
    """Build a translation request body."""
    return {"text": text, "source_lang": "en", "target_lang": "fr"}


def budget(allowed: bool, used: int = 0) -> UsageResult:
    """Build an iOS budget result for the free plan's 5k monthly limit."""
    return UsageResult(
        allowed=allowed,
        characters_used=used,
        characters_limit=5_000,
        characters_remaining=max(0, 5_000 - used),
        reset_at=9999999999,
    )


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestIosHeaderBudget:
    """Requests with the iOS header are counted against a per-IP monthly budget."""

    def test_exhausted_budget_is_refused_without_model_call(
        self, client, httpx_mock: HTTPXMock
    ):
        """Once the IP's monthly budget is used up, the header no longer translates."""
        with patch.object(
            anonymous_usage_tracker,
            "check_and_record_ios_usage",
            new_callable=AsyncMock,
            return_value=budget(False, used=5_000),
        ):
            response = client.post(
                "/api/v1/translate", json=body("Hello"), headers=IOS_HEADERS
            )

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "USAGE_LIMIT_EXCEEDED"
        assert httpx_mock.get_requests() == []

    def test_budget_left_keeps_free_request_size(self, client, httpx_mock: HTTPXMock):
        """iOS users keep the free plan's request size (500) and are charged per IP."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "Bonjour"}}]})
        text = "a" * 450
        with (
            patch.object(
                anonymous_usage_tracker,
                "check_and_record_ios_usage",
                new_callable=AsyncMock,
                return_value=budget(True, used=450),
            ) as ios_usage,
            patch.object(
                anonymous_usage_tracker, "check_and_record_usage", new_callable=AsyncMock
            ) as weekly_usage,
        ):
            response = client.post("/api/v1/translate", json=body(text), headers=IOS_HEADERS)

        assert response.status_code == 200
        ios_usage.assert_awaited_once()
        assert ios_usage.await_args.args[1] == 450
        weekly_usage.assert_not_awaited()

    def test_web_requests_still_use_the_weekly_limit(self, client, httpx_mock: HTTPXMock):
        """Without the header, anonymous requests are counted against the weekly limit."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "Bonjour"}}]})
        with patch.object(
            anonymous_usage_tracker, "check_and_record_ios_usage", new_callable=AsyncMock
        ) as ios_usage:
            response = client.post("/api/v1/translate", json=body("Hello"))

        assert response.status_code == 200
        ios_usage.assert_not_awaited()


class FakeScript:
    """Stand-in for the Redis check-and-increment script; records the call."""

    def __init__(self):
        self.keys = None
        self.args = None

    async def __call__(self, keys, args):
        """Allow the request and report the cost as the new total."""
        self.keys, self.args = keys, args
        return [1, args[0]]


class TestIosBudgetTracker:
    """The tracker counts iOS usage per IP and month against the free plan's limit."""

    async def test_counts_per_ip_and_month(self):
        """Usage is keyed by IP and month, with the free plan's monthly limit."""
        tracker = usage_tracker.AnonymousUsageTracker()
        tracker.client = object()
        tracker._check_and_incr = FakeScript()

        with patch.object(usage_tracker, "get_month_key", return_value="2026-11"):
            result = await tracker.check_and_record_ios_usage("1.2.3.4", 300)

        assert tracker._check_and_incr.keys == ["usage:ios:1.2.3.4:2026-11"]
        assert tracker._check_and_incr.args[:2] == [300, 5_000]
        assert result.allowed is True
        assert result.characters_remaining == 4_700

    @pytest.mark.parametrize("today,expected", [
        (datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc), datetime(2026, 12, 1)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), datetime(2027, 1, 1)),
    ])
    def test_budget_resets_on_the_first_of_next_month(self, today, expected):
        """The reset time is the first of next month, including across a year end."""
        with patch.object(usage_tracker, "datetime", wraps=datetime) as fake_datetime:
            fake_datetime.now.return_value = today
            reset_at = usage_tracker.next_month_start()

        assert reset_at == int(expected.replace(tzinfo=timezone.utc).timestamp())
