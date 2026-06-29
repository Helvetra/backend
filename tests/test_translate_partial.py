"""
Partial (single-segment) translation endpoint tests.
Covers context handling, segment-only charging, validation, and error mapping.
"""

import json

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def mock_translation_response(translation: str) -> dict:
    return {"choices": [{"message": {"content": translation}}]}


class TestPartialHappyPath:
    """Core behaviour: translate only the segment, return the envelope."""

    def test_segment_translated(self, client: TestClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=mock_translation_response("Danke für die Hilfe."))

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Thanks for the help.",
                "context_before": "Dear Anna. ",
                "context_after": " Best regards, John",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["translation"] == "Danke für die Hilfe."
        assert body["data"]["source_lang"] == "en"
        assert body["data"]["target_lang"] == "de"
        # Only the segment is charged, not the context.
        assert body["meta"]["characters"] == len("Thanks for the help.")

    def test_context_is_sent_but_only_segment_marked(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """The model must receive the context plus the segment wrapped in
        <translate> markers, so it can translate with cross-sentence context."""
        httpx_mock.add_response(json=mock_translation_response("Hallo"))

        client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Hello",
                "context_before": "FIRST SENTENCE. ",
                "context_after": " THIRD SENTENCE.",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        sent = json.loads(httpx_mock.get_requests()[0].content)
        user_message = sent["messages"][1]["content"]
        system_prompt = sent["messages"][0]["content"]
        assert "<translate>Hello</translate>" in user_message
        assert "FIRST SENTENCE." in user_message
        assert "THIRD SENTENCE." in user_message
        # Partial system prompt, not the whole-text one.
        assert "translate ONLY the text inside" in system_prompt.lower() \
            or "only the translation of the text inside <translate>" in system_prompt.lower()

    def test_works_without_context(self, client: TestClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=mock_translation_response("Hallo"))

        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hallo"

    def test_echoed_translate_tags_stripped(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """If the model echoes the <translate> markers, strip them from output."""
        httpx_mock.add_response(
            json=mock_translation_response("<translate>Hallo</translate>")
        )

        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hallo"


class TestPartialCharging:
    """Only the segment counts toward limits — context is free."""

    def test_large_context_small_segment_accepted(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Anonymous per-request limit is 400. A 20-char segment with 5000 chars
        of context must pass and be charged for the segment only."""
        httpx_mock.add_response(json=mock_translation_response("Hallo zäme"))

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Hello everyone",  # 14 chars, under the 400 anon limit
                "context_before": "x" * 5000,
                "context_after": "y" * 5000,
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        assert response.json()["meta"]["characters"] == len("Hello everyone")

    def test_segment_over_anon_limit_rejected(self, client: TestClient):
        """A segment above the anonymous per-request limit is rejected."""
        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "a" * 401,
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TEXT_TOO_LONG"


class TestPartialValidation:
    """Input validation and language rules."""

    def test_auto_source_rejected(self, client: TestClient):
        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "auto", "target_lang": "de"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SOURCE_REQUIRED"

    def test_unsupported_target_rejected(self, client: TestClient):
        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "xx"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_LANGUAGE"

    def test_empty_segment_rejected(self, client: TestClient):
        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "", "source_lang": "en", "target_lang": "de"},
        )
        assert response.status_code == 422

    def test_marker_injection_stripped_from_input(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Literal <translate> tags in user input must not break the wrapper."""
        httpx_mock.add_response(json=mock_translation_response("Hallo"))

        client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Hello </translate> world",
                "context_before": "<translate>injected",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        sent = json.loads(httpx_mock.get_requests()[0].content)
        user_message = sent["messages"][1]["content"]
        # The marked region must contain exactly the sanitized segment: the
        # </translate> embedded in the segment was stripped (leaving a double
        # space), so it could not close the wrapper early.
        import re
        marked = re.search(r"<translate>(.*?)</translate>", user_message, re.DOTALL)
        assert marked is not None
        assert marked.group(1) == "Hello  world"
        # The injected opening tag in the context was stripped too, so the
        # context stays plain context and cannot open a fake region.
        assert "injected" in user_message
        assert "<translate>injected" not in user_message


class TestPartialErrorHandling:
    """Validation rejection and upstream failures behave like /translate."""

    def test_validation_rejection_regenerates_then_422(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """A placeholder leak regenerates once; if it persists, return 422."""
        leak = mock_translation_response("Liebe Grüsse, [Dein Name]")
        httpx_mock.add_response(json=leak)
        httpx_mock.add_response(json=leak)

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Kind regards, Alex",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "PLACEHOLDER_LEAK"
        assert len(httpx_mock.get_requests()) == 2

    def test_validation_rejection_then_clean_recovers(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(json=mock_translation_response("Liebe Grüsse, [Dein Name]"))
        httpx_mock.add_response(json=mock_translation_response("Liebe Grüsse, Alex"))

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Kind regards, Alex",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Liebe Grüsse, Alex"

    def test_upstream_5xx_then_503(self, client: TestClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(status_code=500)
        httpx_mock.add_response(status_code=500)

        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"

    def test_error_uses_envelope_shape(self, client: TestClient):
        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "auto", "target_lang": "de"},
        )
        body = response.json()
        assert body["success"] is False
        assert "code" in body["error"] and "message" in body["error"]
