"""
Languages endpoint tests.
Covers supported languages retrieval.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestLanguagesEndpoint:
    """Tests for GET /api/v1/languages endpoint."""

    def test_get_languages_success(self, client: TestClient):
        """Returns list of supported languages."""
        response = client.get("/api/v1/languages")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert isinstance(data["data"], list)
        assert len(data["data"]) == 11

    def test_get_languages_contains_required(self, client: TestClient):
        """Response contains all required languages."""
        response = client.get("/api/v1/languages")

        data = response.json()
        codes = [lang["code"] for lang in data["data"]]

        assert "de" in codes
        assert "gsw" in codes
        assert "fr" in codes
        assert "it" in codes
        assert "en" in codes
        assert "rm" in codes
        assert "sq" in codes
        assert "pt" in codes
        assert "es" in codes
        assert "hr" in codes
        assert "sr" in codes

    def test_get_languages_format(self, client: TestClient):
        """Each language has required fields."""
        response = client.get("/api/v1/languages")

        data = response.json()
        for lang in data["data"]:
            assert "code" in lang
            assert "name" in lang
            assert "native_name" in lang
            assert isinstance(lang["code"], str)
            assert isinstance(lang["name"], str)
            assert isinstance(lang["native_name"], str)
