"""
API entry point.
Initializes the app with middleware and routes.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.public_docs import install_public_docs
from app.api.routes import (
    api_keys,
    auth,
    feedback,
    health,
    languages,
    payments,
    public,
    subscription,
    translate,
    webhooks,
)
from app.config import get_settings
from app.core.errors import install_error_handlers
from app.core.middleware import RateLimitMiddleware

settings = get_settings()

# Refuse to boot with weak or missing production secrets. Catching these at
# module import (before FastAPI is constructed) means a misconfigured deploy
# fails fast in the container logs instead of producing 500s at request time
# or — worse — silently accepting empty defaults. See helvetra/backend#93, #98.
_MIN_SECRET_LEN = 32


def _require_secret(name: str, value: str, min_len: int = _MIN_SECRET_LEN) -> None:
    if len(value) < min_len:
        raise RuntimeError(
            f"{name} must be set and at least {min_len} bytes. "
            "Generate one with: openssl rand -base64 48"
        )


_require_secret("JWT_SECRET_KEY", settings.jwt_secret_key)

# Production-only checks. In debug mode (local dev) we keep these optional
# so the app boots without a full secret kit. The deploy env sets DEBUG=false.
if not settings.debug:
    _require_secret("ENCRYPTION_KEY", settings.encryption_key)
    _require_secret("STRIPE_SECRET_KEY", settings.stripe_secret_key, min_len=20)
    _require_secret("STRIPE_WEBHOOK_SECRET", settings.stripe_webhook_secret, min_len=20)
    _require_secret(
        "STRIPE_B2B_BUSINESS_BASE_LOOKUP", settings.stripe_b2b_business_base_lookup, min_len=4
    )

app = FastAPI(
    title="Helvetra API",
    description="Swiss translation API",
    version="0.1.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# Single error envelope for every failure path (helvetra/backend#119).
install_error_handlers(app)

# Middleware is processed in reverse order (last added = first executed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    # DELETE is required for /api/v1/auth/account and /api/v1/api-keys/{id}.
    allow_methods=["GET", "POST", "DELETE"],
    # Pinned to the headers the web client actually sends. `*` plus
    # allow_credentials=True is a footgun if origins ever widen.
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-API-Key"],
)
app.add_middleware(RateLimitMiddleware)

app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])
app.include_router(languages.router, prefix="/api/v1", tags=["Languages"])
app.include_router(translate.router, prefix="/api/v1", tags=["Translation"])
app.include_router(feedback.router, prefix="/api/v1", tags=["Feedback"])
app.include_router(subscription.router, prefix="/api/v1", tags=["Subscription"])
app.include_router(payments.router, prefix="/api/v1", tags=["Payments"])
app.include_router(api_keys.router, prefix="/api/v1", tags=["API Keys"])
app.include_router(webhooks.router, prefix="/api/v1", tags=["Webhooks"])

# B2B Public API — separate prefix, API key auth
app.include_router(public.router, prefix="/api/public/v1", tags=["Public API"])

# Hosted developer documentation for the public API (always on in prod;
# the consumer /docs and /redoc remain debug-only).
install_public_docs(app)
