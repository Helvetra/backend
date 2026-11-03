"""
Translation deadline tests.
A translation that does not finish within its total time budget is answered
with a JSON 504 before the reverse proxy would give up. See helvetra/infra#18.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import translation


async def slow_model_call(*args, **kwargs):
    """A model call that never answers within the test deadline."""
    await asyncio.sleep(5)
    return "too late"


@pytest.mark.parametrize("path,body", [
    ("/api/v1/translate", {"text": "Hello", "source_lang": "en", "target_lang": "de"}),
    ("/api/v1/translate/partial", {"segment": "Hello", "source_lang": "en", "target_lang": "de"}),
])
def test_slow_translation_times_out_with_json(path, body):
    """Both routes answer 504 TRANSLATION_TIMEOUT once the deadline passes."""
    with (
        patch.object(translation, "TRANSLATION_DEADLINE_SECONDS", 0.05),
        patch.object(translation, "_call_model", side_effect=slow_model_call),
    ):
        response = TestClient(app).post(path, json=body)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "TRANSLATION_TIMEOUT"

