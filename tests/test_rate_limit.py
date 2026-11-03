"""
Rate limiting tests.
Covers middleware behavior and limit enforcement.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.rate_limiter import RateLimitResult


@pytest.fixture(autouse=True)
def disable_auto_mock_rate_limiter(mock_rate_limiter):
    """Override the autouse mock for rate limit tests - we provide our own mocks."""
    pass


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestRateLimitMiddleware:
    """Tests for rate limiting middleware."""

    def test_request_allowed_with_remaining_quota(self, client: TestClient):
        """Request is allowed when within rate limits."""
        mock_result = RateLimitResult(
            allowed=True,
            remaining=59,
            reset_at=1234567890,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.get("/api/v1/languages")

            assert response.status_code == 200
            assert response.headers["X-RateLimit-Remaining"] == "59"
            assert "X-RateLimit-Reset" in response.headers

    def test_request_rejected_when_limit_exceeded(self, client: TestClient):
        """Request is rejected with 429 when rate limit exceeded."""
        mock_result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=1234567890,
            retry_after=30,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.get("/api/v1/languages")

            assert response.status_code == 429
            data = response.json()
            assert data["success"] is False
            assert data["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            assert data["error"]["retry_after"] == 30
            assert response.headers["Retry-After"] == "30"

    def test_health_endpoint_exempt_from_rate_limit(self, client: TestClient):
        """Health endpoint bypasses rate limiting."""
        # Mock that would block if called
        mock_result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=1234567890,
            retry_after=30,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_check:
            # Also mock health check functions to avoid actual service calls
            ok = {"new_callable": AsyncMock, "return_value": "ok"}
            with patch("app.api.routes.health.check_database", **ok), \
                 patch("app.api.routes.health.check_redis", **ok), \
                 patch("app.api.routes.health.check_translation_api", **ok):
                response = client.get("/api/health")

                assert response.status_code == 200
                # Rate limiter should not be called for health endpoint
                mock_check.assert_not_called()

    def test_rate_limit_headers_included_on_success(self, client: TestClient):
        """Successful responses include rate limit headers."""
        mock_result = RateLimitResult(
            allowed=True,
            remaining=10,
            reset_at=1234567890,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.get("/api/v1/languages")

            assert "X-RateLimit-Remaining" in response.headers
            assert "X-RateLimit-Reset" in response.headers

    def test_rate_limit_uses_client_ip(self, client: TestClient):
        """Rate limiter receives client IP address."""
        mock_result = RateLimitResult(
            allowed=True,
            remaining=59,
            reset_at=1234567890,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_check:
            client.get("/api/v1/languages")

            # Verify check_rate_limit was called with an IP
            mock_check.assert_called_once()
            called_ip = mock_check.call_args[0][0]
            assert called_ip is not None

    def test_rate_limit_uses_real_ip(self, client: TestClient):
        """Rate limiter uses X-Real-IP header set by nginx."""
        mock_result = RateLimitResult(
            allowed=True,
            remaining=59,
            reset_at=1234567890,
        )

        with patch(
            "app.core.middleware.rate_limiter.check_rate_limit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_check:
            client.get(
                "/api/v1/languages",
                headers={"X-Real-IP": "203.0.113.50"},
            )

            # Should use X-Real-IP set by nginx (not spoofable X-Forwarded-For)
            mock_check.assert_called_once_with("203.0.113.50")
