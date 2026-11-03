"""
Application configuration loaded from environment variables.
Centralizes all settings to ensure consistent access across the app.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/helvetra"

    # Cache
    redis_url: str = "redis://localhost:6379"

    # Translation API
    apertus_api_base: str = ""
    apertus_api_key: str = ""
    apertus_model: str = ""

    # App settings
    debug: bool = False
    cors_origins: str = "http://localhost:3000"

    # Rate limiting
    rate_limit_per_minute: int = 60
    rate_limit_per_day: int = 500
    # Signed-in accounts: burst protection only, the monthly quota is the
    # real cap. Higher daily limit because edits retranslate per sentence.
    rate_limit_user_per_minute: int = 60
    rate_limit_user_per_day: int = 2000

    # Encryption (for PII)
    encryption_key: str = ""

    # Authentication
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # Apple StoreKit kill-switch. The current verifier (apple_storekit.py)
    # validates against the wrong JWKS (Sign-In-with-Apple instead of App
    # Store Root CA) and disables claim checks; until rewritten against
    # Apple's app-store-server-library, every entry point that depends on
    # transaction verification refuses to process. See helvetra/backend#94.
    apple_storekit_enabled: bool = False

    # Stripe (payments)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_monthly_id: str = ""
    stripe_price_yearly_id: str = ""

    # Stripe B2B — resolved at runtime via lookup keys, not raw price IDs,
    # so price rotations in the Stripe Dashboard don't require code/env changes.
    # Starter is no longer offered; kept so existing env files still load.
    stripe_b2b_starter_base_lookup: str = ""
    stripe_b2b_starter_overage_lookup: str = ""
    stripe_b2b_business_base_lookup: str = ""
    stripe_b2b_business_overage_lookup: str = ""
    stripe_b2b_meter_event_name: str = ""

    # Apple Sign-In and StoreKit
    apple_bundle_id: str = "ch.helvetra.app"
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = ""  # Contents of .p8 file
    apple_app_store_environment: str = "sandbox"  # sandbox or production

    # Email (SMTP)
    smtp_host: str = "mail.infomaniak.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "no-reply@helvetra.ch"
    smtp_from_name: str = "Helvetra"
    email_verification_expire_hours: int = 24
    email_verification_base_url: str = "https://helvetra.ch/verify-email"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        return [origin.strip() for origin in self.cors_origins.split(",")]


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
