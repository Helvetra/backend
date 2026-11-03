"""
Health endpoint tests.
Covers service health checks and response format.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

# Module whose service checks are patched.
HEALTH = "app.api.routes.health"


@pytest.fixture
def client():
    """Create test client for the API."""
    return TestClient(app)


class TestHealthEndpoint:
    """Tests for GET /api/health endpoint."""

    def test_health_returns_response_format(self, client: TestClient):
        """Health check returns expected response structure."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "ok"
            mock_redis.return_value = "ok"
            mock_api.return_value = "ok"

            response = client.get("/api/health")

            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "services" in data
            assert "version" in data
            assert "db" in data["services"]
            assert "redis" in data["services"]
            assert "translation" in data["services"]

    def test_health_all_services_ok(self, client: TestClient):
        """Overall status is ok when all services are healthy."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "ok"
            mock_redis.return_value = "ok"
            mock_api.return_value = "ok"

            response = client.get("/api/health")

            data = response.json()
            assert data["status"] == "ok"

    def test_health_degraded_when_db_fails(self, client: TestClient):
        """Overall status is degraded when database is unhealthy."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "error"
            mock_redis.return_value = "ok"
            mock_api.return_value = "ok"

            response = client.get("/api/health")

            data = response.json()
            assert data["status"] == "degraded"
            assert data["services"]["db"] == "error"

    def test_health_degraded_when_redis_fails(self, client: TestClient):
        """Overall status is degraded when Redis is unhealthy."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "ok"
            mock_redis.return_value = "error"
            mock_api.return_value = "ok"

            response = client.get("/api/health")

            data = response.json()
            assert data["status"] == "degraded"
            assert data["services"]["redis"] == "error"

    def test_health_degraded_when_translation_fails(self, client: TestClient):
        """Overall status is degraded when translation API is unhealthy."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "ok"
            mock_redis.return_value = "ok"
            mock_api.return_value = "error"

            response = client.get("/api/health")

            data = response.json()
            assert data["status"] == "degraded"
            assert data["services"]["translation"] == "error"

    def test_health_returns_version(self, client: TestClient):
        """Health check includes version information."""
        with patch(f"{HEALTH}.check_database", new_callable=AsyncMock) as mock_db, \
             patch(f"{HEALTH}.check_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{HEALTH}.check_translation_api", new_callable=AsyncMock) as mock_api:
            mock_db.return_value = "ok"
            mock_redis.return_value = "ok"
            mock_api.return_value = "ok"

            response = client.get("/api/health")

            data = response.json()
            assert data["version"] == "0.1.0"
