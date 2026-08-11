"""
Translation endpoint tests.
Covers input validation, API integration, and output validation.
"""

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.main import app


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


def mock_translation_response(translation: str) -> dict:
    """Build a mock API response with the given translation."""
    return {
        "choices": [
            {
                "message": {
                    "content": translation
                }
            }
        ]
    }


class TestTranslateEndpoint:
    """Tests for POST /api/v1/translate endpoint."""

    def test_translate_success(self, client: TestClient, httpx_mock: HTTPXMock):
        """Successful translation returns correct response format."""
        httpx_mock.add_response(
            json=mock_translation_response("Bonjour")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["translation"] == "Bonjour"
        assert data["data"]["source_lang"] == "en"
        assert data["data"]["target_lang"] == "fr"
        assert "characters" in data["meta"]
        assert "processing_time_ms" in data["meta"]

    def test_translate_empty_text_rejected(self, client: TestClient):
        """Empty text is rejected with validation error."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "",
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 422

    def test_translate_text_over_schema_ceiling_rejected(self, client: TestClient):
        """Text above the absolute schema ceiling (50k) is rejected by validation."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "a" * 50_001,
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 422

    def test_translate_text_over_1000_hits_tier_check_not_schema(self, client: TestClient):
        """Texts between tier limits must reach the tier check, not die in the
        schema: a 1,001-char anonymous request gets a tier-aware TEXT_TOO_LONG,
        and the same text from a paying user is accepted (see TestPaidTierLengths)."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "a" * 1001,
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TEXT_TOO_LONG"

    def test_translate_max_length_accepted(self, client: TestClient, httpx_mock: HTTPXMock):
        """Text at exactly the anonymous per-request limit (400) is accepted."""
        httpx_mock.add_response(
            json=mock_translation_response("translated")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "a" * 400,
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 200

    def test_translate_anonymous_tier_limit_rejected(self, client: TestClient):
        """Text above the anonymous per-request limit is rejected with TEXT_TOO_LONG."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "a" * 401,
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TEXT_TOO_LONG"

    def test_translate_missing_source_lang_rejected(self, client: TestClient):
        """Missing source language is rejected."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 422

    def test_translate_missing_target_lang_rejected(self, client: TestClient):
        """Missing target language is rejected."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
            }
        )

        assert response.status_code == 422

    def test_translate_suspicious_output_rejected(self, client: TestClient, httpx_mock: HTTPXMock):
        """Output that exceeds both 3x input length and an absolute floor is rejected."""
        # Input "Hi" (2 chars). Threshold = max(2*3, 2+80) = 82. Mock 200 chars.
        # Registered twice: a validation rejection triggers one regeneration.
        httpx_mock.add_response(json=mock_translation_response("X" * 200))
        httpx_mock.add_response(json=mock_translation_response("X" * 200))

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hi",
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SUSPICIOUS_OUTPUT"

    def test_translate_short_input_allows_normal_expansion(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Short inputs must allow normal-length translations within the absolute floor."""
        # Input "Here" (4 chars). Old threshold (4*3=12) would reject a normal
        # translation; new threshold max(12, 84) = 84 must allow it.
        httpx_mock.add_response(json=mock_translation_response("Hie isch's"))

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Here",
                "source_lang": "en",
                "target_lang": "gsw",
            }
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hie isch's"

    def test_translate_api_error_handled(self, client: TestClient, httpx_mock: HTTPXMock):
        """Persistent upstream failures surface as 503 with a structured code."""
        # Registered twice: a 5xx triggers one retry before giving up.
        httpx_mock.add_response(status_code=500)
        httpx_mock.add_response(status_code=500)

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 503
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "UPSTREAM_UNAVAILABLE"

    def test_translate_preserves_whitespace(self, client: TestClient, httpx_mock: HTTPXMock):
        """Translation preserves meaningful content."""
        httpx_mock.add_response(
            json=mock_translation_response("  Bonjour  ")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "  Hello  ",
                "source_lang": "en",
                "target_lang": "fr",
            }
        )

        assert response.status_code == 200
        # Response is stripped
        assert response.json()["data"]["translation"] == "Bonjour"

    def test_translate_formality_default_auto(self, client: TestClient, httpx_mock: HTTPXMock):
        """Formality defaults to auto when not specified."""
        httpx_mock.add_response(
            json=mock_translation_response("Hallo")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "de",
            }
        )

        assert response.status_code == 200

    def test_translate_formality_informal_accepted(self, client: TestClient, httpx_mock: HTTPXMock):
        """Informal formality is accepted for German translations."""
        httpx_mock.add_response(
            json=mock_translation_response("Hallo")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "de",
                "formality": "informal",
            }
        )

        assert response.status_code == 200

    def test_translate_formality_formal_accepted(self, client: TestClient, httpx_mock: HTTPXMock):
        """Formal formality is accepted for German translations."""
        httpx_mock.add_response(
            json=mock_translation_response("Guten Tag")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "de",
                "formality": "formal",
            }
        )

        assert response.status_code == 200

    def test_translate_formality_invalid_rejected(self, client: TestClient):
        """Invalid formality value is rejected."""
        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Hello",
                "source_lang": "en",
                "target_lang": "de",
                "formality": "invalid",
            }
        )

        assert response.status_code == 422

    def test_translate_formality_french_accepted(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Formality is accepted for French translations (tu/vous)."""
        httpx_mock.add_response(
            json=mock_translation_response("Comment allez-vous?")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "How are you?",
                "source_lang": "en",
                "target_lang": "fr",
                "formality": "formal",
            }
        )

        assert response.status_code == 200

    def test_translate_formality_italian_accepted(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Formality is accepted for Italian translations (tu/Lei)."""
        httpx_mock.add_response(
            json=mock_translation_response("Come sta?")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "How are you?",
                "source_lang": "en",
                "target_lang": "it",
                "formality": "formal",
            }
        )

        assert response.status_code == 200

    def test_translate_formality_ignored_for_english(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """Formality parameter is accepted but ignored for English (no T-V)."""
        httpx_mock.add_response(
            json=mock_translation_response("Hello")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Bonjour",
                "source_lang": "fr",
                "target_lang": "en",
                "formality": "formal",
            }
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Hello"


class TestPromptStructure:
    """
    Verify the prompt content used to call the LLM resists treating user input
    as instructions. Assertions check the prompt strings and templates directly,
    so they are deterministic and isolated from the HTTP layer.
    """

    def test_user_text_is_wrapped_in_delimiters(self):
        """User text must be wrapped in <text>...</text> tags."""
        from app.services.translation import USER_MESSAGE_TEMPLATE

        wrapped = USER_MESSAGE_TEMPLATE.format(text="Hello world")
        assert "<text>" in wrapped and "</text>" in wrapped
        assert "Hello world" in wrapped

    def test_user_message_includes_post_content_reminder(self):
        """Wrapper must end with a reminder to translate, not respond."""
        from app.services.translation import USER_MESSAGE_TEMPLATE

        wrapped = USER_MESSAGE_TEMPLATE.format(text="Hello").lower()
        assert "output only the translation" in wrapped

    def test_system_prompt_forbids_answering_questions(self):
        """System prompt must instruct the model to translate questions, not answer them."""
        from app.services.translation import SYSTEM_PROMPT

        prompt = SYSTEM_PROMPT.lower()
        assert "translate the question" in prompt
        assert "do not answer" in prompt

    def test_system_prompt_forbids_fulfilling_instructions(self):
        """System prompt must instruct the model to translate instructions, not fulfill them."""
        from app.services.translation import SYSTEM_PROMPT

        prompt = SYSTEM_PROMPT.lower()
        assert "translate the instruction" in prompt
        assert "do not fulfill" in prompt

    def test_system_prompt_forbids_computing_math(self):
        """System prompt must instruct the model to translate math problems, not solve them."""
        from app.services.translation import SYSTEM_PROMPT

        prompt = SYSTEM_PROMPT.lower()
        assert "do not compute" in prompt

    def test_system_prompt_anchors_letter_names(self):
        """System prompt must forbid swapping greeting and signature names."""
        from app.services.translation import SYSTEM_PROMPT

        prompt = SYSTEM_PROMPT.lower()
        assert "never swap" in prompt
        assert "greeting" in prompt and "signature" in prompt

    def test_auto_detect_prompt_inherits_anti_instruction_rules(self):
        """The auto-detect prompt must apply the same anti-instruction rules."""
        from app.services.translation import SYSTEM_PROMPT_AUTO_DETECT

        prompt = SYSTEM_PROMPT_AUTO_DETECT.lower()
        assert "translate the question" in prompt
        assert "translate the instruction" in prompt
        assert "never swap" in prompt


class TestStripWrapperTags:
    """
    The model occasionally echoes the <text>...</text> wrapper back into its
    output. The post-processing helper must remove those tags without altering
    legitimate content.
    """

    def test_strips_leading_and_trailing_tags(self):
        from app.services.translation import strip_wrapper_tags

        assert strip_wrapper_tags("<text>\nHallo\n</text>") == "Hallo"

    def test_strips_only_outer_tags(self):
        """Tags inside the body (legitimate user content) must remain."""
        from app.services.translation import strip_wrapper_tags

        result = strip_wrapper_tags("<text>before <text>inner</text> after</text>")
        assert result == "before <text>inner</text> after"

    def test_passthrough_when_no_tags(self):
        from app.services.translation import strip_wrapper_tags

        assert strip_wrapper_tags("Bonjour le monde") == "Bonjour le monde"

    def test_handles_only_one_side(self):
        from app.services.translation import strip_wrapper_tags

        assert strip_wrapper_tags("<text>\nHallo") == "Hallo"
        assert strip_wrapper_tags("Hallo\n</text>") == "Hallo"

    def test_idempotent_on_doubled_tags(self):
        from app.services.translation import strip_wrapper_tags

        assert strip_wrapper_tags("<text><text>Hi</text></text>") == "Hi"

    def test_strips_trailing_note_after_blank_line(self):
        """Apertus appends '(Note: ...)' explanations after a blank line; strip those."""
        from app.services.translation import strip_wrapper_tags

        raw = (
            'Do\n\n(Note: The original text "Here" does not contain any '
            "translatable content. If this is a placeholder or a test, please "
            "provide actual text to translate.)"
        )
        assert strip_wrapper_tags(raw) == "Do"

    def test_strips_trailing_note_after_single_newline(self):
        """Apertus also uses just one newline before the note; strip those too."""
        from app.services.translation import strip_wrapper_tags

        raw = "Do\n(Note: The original text was very short and ambiguous.)"
        assert strip_wrapper_tags(raw) == "Do"

    def test_strips_trailing_translation_note(self):
        from app.services.translation import strip_wrapper_tags

        raw = "Hallo Welt\n\n*Translation note: This is a casual greeting.*"
        assert strip_wrapper_tags(raw) == "Hallo Welt"

    def test_strips_trailing_this_is_a_literal_translation(self):
        """Apertus uses 'This is a literal translation...' as a meta block."""
        from app.services.translation import strip_wrapper_tags

        raw = "Do isch do\n(This is a literal translation of the text, as the original text is very short and ambiguous.)"
        assert strip_wrapper_tags(raw) == "Do isch do"

    def test_strips_trailing_the_original_text(self):
        from app.services.translation import strip_wrapper_tags

        raw = "Hallo\n(The original text was very short.)"
        assert strip_wrapper_tags(raw) == "Hallo"

    def test_preserves_inline_parenthetical_in_translation(self):
        """Legitimate parentheticals inside the translation must be kept."""
        from app.services.translation import strip_wrapper_tags

        raw = "Hello world (informal version)"
        assert strip_wrapper_tags(raw) == "Hello world (informal version)"

    def test_preserves_parenthetical_letter_line(self):
        """Multi-line letter where a line is parenthetical must be kept."""
        from app.services.translation import strip_wrapper_tags

        raw = "Dear Anna,\n(your friend)\nJohn"
        assert strip_wrapper_tags(raw) == "Dear Anna,\n(your friend)\nJohn"


class TestValidateTranslationOutput:
    """Post-translation validators that catch model rule violations."""

    def test_matching_salutation_names_pass(self):
        from app.services.translation import validate_translation_output

        validate_translation_output(
            "Dear Claudia,\nThanks.\nAlex",
            "Liebe Claudia,\nDanke.\nAlex",
        )

    def test_salutation_name_swapped_raises(self):
        """Source greets Claudia, target greets Alex — model swapped names."""
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Dear Claudia,\nThanks.\nAlex",
                "Lieber Alex,\nDanke.\nAlex",
            )
        assert exc_info.value.code == "NAME_SUBSTITUTION"

    def test_no_salutation_in_source_is_skipped(self):
        from app.services.translation import validate_translation_output

        validate_translation_output("Just a sentence.", "Nur ein Satz.")

    def test_no_salutation_in_target_is_skipped(self):
        from app.services.translation import validate_translation_output

        validate_translation_output("Dear Claudia, hi", "Hallo zusammen")

    def test_french_salutation_preserved(self):
        from app.services.translation import validate_translation_output

        validate_translation_output("Cher Marc,\nMerci.", "Lieber Marc,\nDanke.")

    def test_italian_salutation_swap_raises(self):
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Caro Marco,\nGrazie.",
                "Lieber Stefan,\nDanke.",
            )
        assert exc_info.value.code == "NAME_SUBSTITUTION"

    def test_signature_name_matches_user_name_does_not_swap(self):
        """Source signature 'Alex' must not be reassigned to the greeting."""
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Dear Claudia,\nThanks for your reply.\nKind regards,\nAlex",
                "Lieber Alex,\nDanke für deine Antwort.\nLiebe Grüsse,\nAlex",
            )
        assert exc_info.value.code == "NAME_SUBSTITUTION"

    def test_placeholder_leak_dein_name_raises(self):
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Kind regards,\nAlex",
                "Liebe Grüsse,\n[Dein Name]",
            )
        assert exc_info.value.code == "PLACEHOLDER_LEAK"

    def test_placeholder_leak_your_name_raises(self):
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Kind regards,\nAlex",
                "Best,\n[Your name]",
            )
        assert exc_info.value.code == "PLACEHOLDER_LEAK"

    def test_placeholder_leak_datum_raises(self):
        from app.services.translation import (
            TranslationValidationError,
            validate_translation_output,
        )

        with pytest.raises(TranslationValidationError) as exc_info:
            validate_translation_output(
                "Today is May 26.",
                "Heute ist der [Datum].",
            )
        assert exc_info.value.code == "PLACEHOLDER_LEAK"

    def test_placeholder_in_source_is_preserved(self):
        """If the source contains a placeholder, it's allowed in target too."""
        from app.services.translation import validate_translation_output

        validate_translation_output(
            "Hello [Name], welcome.",
            "Hallo [Name], willkommen.",
        )


class TestTranslateEndpointValidation:
    """The validator turns into a 422 with a structured error code at the API edge."""

    def test_name_substitution_returns_422(self, client: TestClient, httpx_mock: HTTPXMock):
        # Registered twice: a validation rejection triggers one regeneration.
        httpx_mock.add_response(json=mock_translation_response("Lieber Alex,\nDanke.\nAlex"))
        httpx_mock.add_response(json=mock_translation_response("Lieber Alex,\nDanke.\nAlex"))

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Dear Claudia,\nThanks.\nAlex",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "NAME_SUBSTITUTION"

    def test_placeholder_leak_returns_422(self, client: TestClient, httpx_mock: HTTPXMock):
        # Registered twice: a validation rejection triggers one regeneration.
        httpx_mock.add_response(json=mock_translation_response("Liebe Grüsse,\n[Dein Name]"))
        httpx_mock.add_response(json=mock_translation_response("Liebe Grüsse,\n[Dein Name]"))

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Kind regards,\nAlex",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "PLACEHOLDER_LEAK"


class TestPaidTierLengths:
    """Paid-tier texts above the old 1,000-char schema cap must be accepted."""

    def test_pro_user_text_over_1000_accepted(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """A PRO user (5,000/request) translating 1,165 chars gets a 200."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.api.dependencies import get_current_user_optional

        httpx_mock.add_response(json=mock_translation_response("translated"))

        fake_user = MagicMock()
        fake_subscription = MagicMock()
        fake_subscription.tier.value = "pro"

        app.dependency_overrides[get_current_user_optional] = lambda: fake_user
        try:
            with (
                patch(
                    "app.api.routes.translate.get_or_create_subscription",
                    new_callable=AsyncMock,
                    return_value=fake_subscription,
                ),
                patch(
                    "app.api.routes.translate.record_usage",
                    new_callable=AsyncMock,
                ),
            ):
                response = client.post(
                    "/api/v1/translate",
                    json={
                        "text": "a" * 1165,
                        "source_lang": "en",
                        "target_lang": "de",
                    },
                )
        finally:
            app.dependency_overrides.pop(get_current_user_optional, None)

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "translated"


class TestUpstreamRetry:
    """One retry on fast upstream failures, one regeneration on rejection."""

    def test_upstream_500_then_success_recovers(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """A transient upstream 5xx is retried once and succeeds."""
        httpx_mock.add_response(status_code=502)
        httpx_mock.add_response(json=mock_translation_response("Bonjour"))

        response = client.post(
            "/api/v1/translate",
            json={"text": "Hello", "source_lang": "en", "target_lang": "fr"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Bonjour"
        assert len(httpx_mock.get_requests()) == 2

    def test_validation_rejection_then_clean_recovers(
        self, client: TestClient, httpx_mock: HTTPXMock
    ):
        """A placeholder leak on the first sample regenerates and succeeds."""
        httpx_mock.add_response(
            json=mock_translation_response("Liebe Grüsse,\n[Dein Name]")
        )
        httpx_mock.add_response(
            json=mock_translation_response("Liebe Grüsse,\nAlex")
        )

        response = client.post(
            "/api/v1/translate",
            json={
                "text": "Kind regards,\nAlex",
                "source_lang": "en",
                "target_lang": "de",
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["translation"] == "Liebe Grüsse,\nAlex"
        assert len(httpx_mock.get_requests()) == 2

    def test_upstream_4xx_not_retried(self, client: TestClient, httpx_mock: HTTPXMock):
        """A 4xx from upstream is a config problem: fail fast, single attempt."""
        httpx_mock.add_response(status_code=401)

        response = client.post(
            "/api/v1/translate",
            json={"text": "Hello", "source_lang": "en", "target_lang": "fr"},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
        assert len(httpx_mock.get_requests()) == 1


class TestWholeTextFormalityReminder:
    """The whole-text path also gets the strong register reminder (helvetra/backend#25)."""

    def test_informal_adds_reminder(self, client: TestClient, httpx_mock: HTTPXMock):
        import json
        httpx_mock.add_response(json=mock_translation_response("Kannst du helfen?"))
        client.post(
            "/api/v1/translate",
            json={"text": "Can you help?", "source_lang": "en", "target_lang": "de", "formality": "informal"},
        )
        system_prompt = json.loads(httpx_mock.get_requests()[0].content)["messages"][0]["content"]
        assert "IMPORTANT: Use informal address" in system_prompt
        assert "EVERY sentence" in system_prompt

    def test_auto_formality_no_reminder(self, client: TestClient, httpx_mock: HTTPXMock):
        import json
        httpx_mock.add_response(json=mock_translation_response("Hallo"))
        client.post(
            "/api/v1/translate",
            json={"text": "Hello", "source_lang": "en", "target_lang": "de"},
        )
        system_prompt = json.loads(httpx_mock.get_requests()[0].content)["messages"][0]["content"]
        assert "IMPORTANT: Use" not in system_prompt
