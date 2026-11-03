"""
Shared test configuration and fixtures.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

# Set test environment before importing app modules
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("APERTUS_API_KEY", "test-key")
os.environ.setdefault("APERTUS_API_BASE", "https://api.test.local")
os.environ.setdefault("APERTUS_MODEL", "test-model")


@pytest.fixture(autouse=True)
def mock_rate_limiter():
    """Auto-mock rate limiter for all tests to avoid Redis dependency."""
    from app.services.rate_limiter import RateLimitResult

    mock_result = RateLimitResult(
        allowed=True,
        remaining=59,
        reset_at=9999999999,
    )

    with patch(
        "app.core.middleware.rate_limiter.check_rate_limit",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        yield


@pytest.fixture(autouse=True)
def mock_anonymous_usage_tracker():
    """Auto-mock anonymous usage tracking to avoid Redis dependency.

    The tracker caches a Redis client bound to the first event loop that
    touches it; TestClient creates a fresh loop per test, so a real client
    poisons every later test ("attached to a different loop").
    """
    from app.services.usage_tracker import UsageResult, anonymous_usage_tracker

    mock_result = UsageResult(
        allowed=True,
        characters_used=0,
        characters_limit=5000,
        characters_remaining=5000,
        reset_at=9999999999,
    )

    with (
        patch.object(
            anonymous_usage_tracker,
            "check_and_record_usage",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
        patch.object(
            anonymous_usage_tracker,
            "check_and_record_ios_usage",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
        patch.object(
            anonymous_usage_tracker,
            "get_usage",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_feedback_redis():
    """Auto-mock the feedback route's Redis client (same loop-binding issue)."""
    fake_redis = AsyncMock()
    fake_redis.incr.return_value = 1
    fake_redis.expire.return_value = True

    with patch(
        "app.api.routes.feedback._get_redis",
        new_callable=AsyncMock,
        return_value=fake_redis,
    ):
        yield
