"""
Feedback endpoint tests.
Covers consent validation and feedback submission.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app


@pytest.fixture
def mock_db():
    """Create mock database session."""
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def client(mock_db):
    """Create test client with mocked database."""
    app.dependency_overrides[get_db] = lambda: mock_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def valid_feedback():
    """Valid feedback request payload."""
    return {
        "vote": "like",
        "consent": True,
        "source_text": "Hallo Welt",
        "source_lang": "de",
        "translated_text": "Hello World",
        "target_lang": "en",
    }


class TestFeedbackEndpoint:
    """Tests for POST /api/v1/feedback endpoint."""

    def test_feedback_with_consent_accepted(self, client: TestClient, mock_db, valid_feedback):
        """Feedback is stored when consent is given."""
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert mock_db.add.called

    def test_feedback_without_consent_rejected(self, client: TestClient, mock_db, valid_feedback):
        """Feedback is rejected when consent is not given."""
        valid_feedback["consent"] = False
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "CONSENT_REQUIRED"
        assert not mock_db.add.called

    def test_feedback_like_vote(self, client: TestClient, mock_db, valid_feedback):
        """Like vote is accepted."""
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_feedback_dislike_vote(self, client: TestClient, mock_db, valid_feedback):
        """Dislike vote is accepted."""
        valid_feedback["vote"] = "dislike"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_feedback_invalid_vote_rejected(self, client: TestClient, valid_feedback):
        """Invalid vote value is rejected."""
        valid_feedback["vote"] = "invalid"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 422

    def test_feedback_missing_source_text_rejected(self, client: TestClient, valid_feedback):
        """Missing source_text is rejected."""
        del valid_feedback["source_text"]
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 422

    def test_feedback_empty_source_text_rejected(self, client: TestClient, valid_feedback):
        """Empty source_text is rejected."""
        valid_feedback["source_text"] = ""
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 422

    def test_feedback_invalid_source_lang_rejected(self, client: TestClient, valid_feedback):
        """Invalid source_lang is rejected."""
        valid_feedback["source_lang"] = "xx"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 422

    def test_feedback_invalid_target_lang_rejected(self, client: TestClient, valid_feedback):
        """Invalid target_lang is rejected."""
        valid_feedback["target_lang"] = "xx"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 422

    def test_feedback_with_region(self, client: TestClient, mock_db, valid_feedback):
        """Feedback with region is accepted."""
        valid_feedback["region"] = "zurich"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_feedback_with_comment(self, client: TestClient, mock_db, valid_feedback):
        """Feedback with comment is accepted."""
        valid_feedback["comment"] = "Great translation!"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_feedback_gsw_source(self, client: TestClient, mock_db, valid_feedback):
        """Swiss German as source language is accepted."""
        valid_feedback["source_lang"] = "gsw"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True

    @pytest.mark.parametrize("lang", ["rm", "sq", "pt", "es", "hr", "sr"])
    def test_feedback_added_languages(self, client: TestClient, mock_db, valid_feedback, lang):
        """Feedback on Romansh and the added languages is accepted."""
        valid_feedback["target_lang"] = lang
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200

    def test_feedback_gsw_target(self, client: TestClient, mock_db, valid_feedback):
        """Swiss German as target language is accepted."""
        valid_feedback["target_lang"] = "gsw"
        response = client.post("/api/v1/feedback", json=valid_feedback)

        assert response.status_code == 200
        assert response.json()["success"] is True
