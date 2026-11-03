"""
Long-translation tests.
The output budget is sized from the request, and a translation the model
cut off at that budget is refused instead of returned as complete.
See helvetra/backend#128.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.api.dependencies import get_current_user_optional
from app.core.database import get_db
from app.main import app
from app.services.translation import _output_token_budget


def cut_off_response(partial: str) -> dict:
    """A completion that stopped because it reached the output budget."""
    return {
        "choices": [{"message": {"content": partial}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 2000},
    }


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestCutOffTranslation:
    """A cut-off completion is an error, never a result."""

    @pytest.mark.parametrize("path,body", [
        ("/api/v1/translate", {"text": "Hello there.", "source_lang": "en", "target_lang": "de"}),
        (
            "/api/v1/translate/partial",
            {"segment": "Hello there.", "source_lang": "en", "target_lang": "de"},
        ),
    ])
    def test_cut_off_output_is_refused(self, client, httpx_mock: HTTPXMock, path, body):
        """Both routes return 422 TRANSLATION_INCOMPLETE after a single model call."""
        httpx_mock.add_response(json=cut_off_response("Hallo da"))

        response = client.post(path, json=body)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "TRANSLATION_INCOMPLETE"
        # Regenerating with the same budget would be cut off again.
        assert len(httpx_mock.get_requests()) == 1

    def test_cut_off_output_is_not_charged(self, client, httpx_mock: HTTPXMock):
        """A signed-in user is not charged for a translation that was cut off."""
        user = SimpleNamespace(id=uuid4())
        app.dependency_overrides[get_current_user_optional] = lambda: user
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        httpx_mock.add_response(json=cut_off_response("Hallo da"))
        try:
            with (
                patch(
                    "app.api.routes.translate.get_or_create_subscription",
                    new_callable=AsyncMock,
                    return_value=SimpleNamespace(tier=SimpleNamespace(value="free")),
                ),
                patch(
                    "app.api.routes.translate.get_usage_status",
                    new_callable=AsyncMock,
                    return_value=SimpleNamespace(can_translate=True),
                ),
                patch(
                    "app.api.routes.translate.record_usage", new_callable=AsyncMock
                ) as record,
            ):
                response = client.post(
                    "/api/v1/translate",
                    json={"text": "Hello there.", "source_lang": "en", "target_lang": "de"},
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 422
        record.assert_not_awaited()

    def test_completed_output_is_returned(self, client, httpx_mock: HTTPXMock):
        """A completion that finished normally is returned as before."""
        httpx_mock.add_response(
            json={"choices": [{"message": {"content": "Hallo da."}, "finish_reason": "stop"}]}
        )

        response = client.post(
            "/api/v1/translate",
            json={"text": "Hello there.", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hallo da."


class TestOutputBudget:
    """The output budget grows with the request, within fixed bounds."""

    def test_budget_is_sent_with_the_request(self, client, httpx_mock: HTTPXMock):
        """The model call carries a budget sized from the request, not a constant."""
        httpx_mock.add_response(json={"choices": [{"message": {"content": "x"}}]})
        text = "Hello there. " * 35  # 455 characters, under the free limit

        client.post(
            "/api/v1/translate",
            json={"text": text, "source_lang": "en", "target_lang": "de"},
            headers={"X-Client": "helvetra-ios"},  # free-size requests, budget mocked
        )

        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent["max_tokens"] == _output_token_budget(sent["messages"][1]["content"])

    @pytest.mark.parametrize("length,expected", [
        (10, 512),  # short texts keep a usable minimum
        (5_000, 2_756),  # a full Helvetra+ request
        (10_000, 5_256),
        (50_000, 8_192),  # capped
    ])
    def test_budget_bounds(self, length, expected):
        """Half a token per character plus overhead, between 512 and 8,192."""
        assert _output_token_budget("a" * length) == expected
