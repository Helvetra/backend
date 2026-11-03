"""
Public API error tests.
The public translate endpoint maps translation failures to the same
structured errors as the web routes and never echoes internals.
See helvetra/backend#132.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.api.dependencies import get_current_user_from_api_key
from app.core.database import get_db
from app.main import app

BODY = {"text": "Dear Claudia,\nThanks.\nAlex", "source_lang": "en", "target_lang": "de"}


def completion(content: str) -> dict:
    """A model completion with the given content."""
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


@pytest.fixture
def client():
    """An API client authenticated as a Business customer with quota left."""
    user = SimpleNamespace(id=uuid4(), stripe_customer_id="cus_1")
    app.dependency_overrides[get_current_user_from_api_key] = lambda: (user, SimpleNamespace())
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    business = SimpleNamespace(tier=SimpleNamespace(value="business"))
    with (
        patch(
            "app.api.routes.public.get_or_create_subscription",
            new_callable=AsyncMock,
            return_value=business,
        ),
        patch(
            "app.api.routes.public.get_usage_status",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(can_translate=True),
        ),
        patch("app.api.routes.public.record_usage", new_callable=AsyncMock) as record,
        patch("app.api.routes.public.report_translation_meter_event", new_callable=AsyncMock),
        patch("app.api.routes.public.maybe_send_usage_alerts", new_callable=AsyncMock),
    ):
        yield TestClient(app), record
    app.dependency_overrides.clear()


def post(client):
    """Call the public translate endpoint."""
    return client.post("/api/public/v1/translate", json=BODY)


class TestPublicApiErrors:
    """Each translation failure has its own status and code, and is not billed."""

    def test_name_swap_is_422(self, client, httpx_mock: HTTPXMock):
        """A validation rejection was a 500 INTERNAL_ERROR before."""
        api, record = client
        httpx_mock.add_response(json=completion("Lieber Alex,\nDanke.\nAlex"))
        httpx_mock.add_response(json=completion("Lieber Alex,\nDanke.\nAlex"))

        response = post(api)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "NAME_SUBSTITUTION"
        record.assert_not_awaited()

    def test_suspicious_output_is_422(self, client, httpx_mock: HTTPXMock):
        api, record = client
        httpx_mock.add_response(json=completion("x" * 500))
        httpx_mock.add_response(json=completion("x" * 500))

        response = post(api)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SUSPICIOUS_OUTPUT"
        record.assert_not_awaited()

    def test_provider_outage_is_503(self, client, httpx_mock: HTTPXMock):
        """A provider failure was a 500 INTERNAL_ERROR before."""
        api, record = client
        httpx_mock.add_response(status_code=401)

        response = post(api)

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
        record.assert_not_awaited()

    def test_provider_busy_is_503_with_retry_after(self, client, httpx_mock: HTTPXMock):
        api, _ = client
        httpx_mock.add_response(status_code=429)
        httpx_mock.add_response(status_code=429)

        with patch("app.services.translation.asyncio.sleep", new_callable=AsyncMock):
            response = post(api)

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_BUSY"
        assert response.headers["retry-after"] == "10"

    def test_unexpected_error_does_not_leak_details(self, client):
        """An unexpected failure is a generic 500 without the exception text."""
        api, _ = client
        with patch(
            "app.api.routes.public.translate_text",
            side_effect=ValueError("secret internal detail"),
        ):
            response = TestClient(app, raise_server_exceptions=False).post(
                "/api/public/v1/translate", json=BODY
            )

        assert response.status_code == 500
        assert "secret internal detail" not in response.text

    def test_success_is_billed(self, client, httpx_mock: HTTPXMock):
        api, record = client
        httpx_mock.add_response(json=completion("Liebe Claudia,\nDanke.\nAlex"))

        response = post(api)

        assert response.status_code == 200
        record.assert_awaited_once()
