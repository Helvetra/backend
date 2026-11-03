"""
Auto-detect response parsing tests.
The model returns the translation and detected language as JSON; anything
that is not usable JSON must be rejected, never shown as the translation.
See helvetra/backend#133.
"""

import json

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.main import app
from app.services.translation import TranslationValidationError, _parse_auto_detect_response


class TestParseAutoDetectResponse:
    """Valid and almost-valid JSON is parsed; anything else is rejected."""

    @pytest.mark.parametrize("content,expected", [
        ('{"translation": "Bonjour", "detected_lang": "de"}', ("Bonjour", "de")),
        # Markdown code block around the JSON, as Apertus often writes it
        ('```json\n{"translation": "Bonjour", "detected_lang": "de"}\n```', ("Bonjour", "de")),
        # Raw newline inside the string (invalid JSON, recovered)
        (
            '{"translation": "Liebe Anna,\nbis bald", "detected_lang": "en"}',
            ("Liebe Anna,\nbis bald", "en"),
        ),
        # Escapes are decoded, not shown (ticket case)
        ('{"translation": "Caf\\u00e9\nau lait", "detected_lang": "it"}', ("Café\nau lait", "it")),
        # Escaped quotes followed by a comma do not end the string early
        (
            '{"translation": "Er sagte \\"hallo\\", dann\ngings los", "detected_lang": "en"}',
            ('Er sagte "hallo", dann\ngings los', "en"),
        ),
    ])
    def test_parsed(self, content, expected):
        assert _parse_auto_detect_response(content) == expected

    @pytest.mark.parametrize("content", [
        "Sure! Here is the translation: Bonjour",  # ticket case: was returned as-is
        '{"translation": "", "detected_lang": "de"}',
        '{"detected_lang": "de"}',
        "",
    ])
    def test_unparseable_is_rejected(self, content):
        with pytest.raises(TranslationValidationError) as exc_info:
            _parse_auto_detect_response(content)
        assert exc_info.value.code == "UNPARSEABLE_OUTPUT"


def completion(content: str) -> dict:
    """A model completion with the given content."""
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class TestAutoDetectRoute:
    """The route regenerates once, then refuses instead of showing raw output."""

    def test_regenerates_then_succeeds(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=completion("Sure! Here is the translation: Bonjour"))
        httpx_mock.add_response(
            json=completion(json.dumps({"translation": "Bonjour", "detected_lang": "de"}))
        )

        response = TestClient(app).post(
            "/api/v1/translate",
            json={"text": "Guten Tag", "source_lang": "auto", "target_lang": "fr"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Bonjour"
        assert len(httpx_mock.get_requests()) == 2

    def test_twice_unparseable_is_refused(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=completion("Sure! Here is the translation: Bonjour"))
        httpx_mock.add_response(json=completion("Of course. Bonjour"))

        response = TestClient(app).post(
            "/api/v1/translate",
            json={"text": "Guten Tag", "source_lang": "auto", "target_lang": "fr"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNPARSEABLE_OUTPUT"
