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

    def test_only_segment_is_translatable_context_is_reference(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """The translatable slot (user message) must contain only the segment;
        the context goes in the system prompt as reference. This is the
        structural guarantee that the model cannot translate the context."""
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
        # Only the segment is in the translatable user message.
        assert "Hello" in user_message
        assert "FIRST SENTENCE" not in user_message
        assert "THIRD SENTENCE" not in user_message
        # The surrounding text is present in the system prompt as reference.
        assert "FIRST SENTENCE" in system_prompt
        assert "THIRD SENTENCE" in system_prompt

    def test_works_without_context(self, client: TestClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=mock_translation_response("Hallo"))

        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hallo"

    def test_echoed_wrapper_tags_stripped(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """If the model echoes the <text> wrapper, strip it from the output."""
        httpx_mock.add_response(
            json=mock_translation_response("<text>Hallo</text>")
        )

        response = client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hallo"

    def test_no_context_omits_reference_block(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """With no surrounding context, the system prompt has no reference block."""
        httpx_mock.add_response(json=mock_translation_response("Hallo"))

        client.post(
            "/api/v1/translate/partial",
            json={"segment": "Hello", "source_lang": "en", "target_lang": "de"},
        )

        system_prompt = json.loads(httpx_mock.get_requests()[0].content)["messages"][0]["content"]
        assert "REFERENCE ONLY" not in system_prompt


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


class TestPartialSourceEcho:
    """A trailing parenthetical echoing the source is dropped; real ones stay."""

    def test_source_echo_gloss_stripped(self, client: TestClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            json=mock_translation_response("Die Seen sind atemberaubend. (The lakes are stunning.)")
        )

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "The lakes are stunning.",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Die Seen sind atemberaubend."

    def test_legitimate_parenthetical_preserved(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        # The parenthetical does not echo the source, so it must stay.
        httpx_mock.add_response(
            json=mock_translation_response("Ich mag es wirklich (sehr).")
        )

        response = client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "I really like it",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Ich mag es wirklich (sehr)."


def test_strip_source_echo_unit():
    """Direct checks on the echo stripper's precision."""
    from app.services.translation import _strip_source_echo

    # Exact echo (punctuation/case ignored) is stripped.
    assert (
        _strip_source_echo("Die Seen sind schön. (The lakes are stunning)", "The lakes are stunning.")
        == "Die Seen sind schön."
    )
    # A non-matching parenthetical is preserved.
    assert (
        _strip_source_echo("Ich mag es (sehr).", "I like it")
        == "Ich mag es (sehr)."
    )
    # No parenthetical: unchanged.
    assert _strip_source_echo("Hallo Welt", "Hello world") == "Hallo Welt"


class TestPartialFormality:
    """The chosen register must survive a differently-toned context."""

    def test_formality_reminder_unit(self):
        from app.services.translation import get_formality_reminder

        de_informal = get_formality_reminder("de", "informal")
        assert "du/ihr" in de_informal and "Sie" in de_informal
        assert "informal" in de_informal
        # Formal picks the opposite forms.
        assert "Sie" in get_formality_reminder("de", "formal")
        # No T-V distinction or auto -> no reminder.
        assert get_formality_reminder("en", "informal") == ""
        assert get_formality_reminder("de", "auto") == ""

    def test_partial_with_context_sends_register_reminder(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """With context + an explicit register, the final reminder is in the
        system prompt so a formal-sounding context can't flip the tone."""
        httpx_mock.add_response(json=mock_translation_response("Kannst du das schicken?"))

        client.post(
            "/api/v1/translate/partial",
            json={
                "segment": "Can you send this?",
                "context_before": "Dear Sir or Madam. ",
                "source_lang": "en",
                "target_lang": "de",
                "formality": "informal",
            },
        )

        system_prompt = json.loads(httpx_mock.get_requests()[0].content)["messages"][0]["content"]
        assert "IMPORTANT: Use informal address" in system_prompt
        # The context block no longer tells the model to copy the context's tone.
        assert "consistency of tone" not in system_prompt

    def test_partial_without_context_has_no_reminder(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(json=mock_translation_response("Kannst du das schicken?"))

        client.post(
            "/api/v1/translate/partial",
            json={"segment": "Can you send this?", "source_lang": "en", "target_lang": "de", "formality": "informal"},
        )

        system_prompt = json.loads(httpx_mock.get_requests()[0].content)["messages"][0]["content"]
        assert "IMPORTANT: Use informal address" not in system_prompt
