"""
Public API endpoints for B2B customers.
Authenticated via API key (X-API-Key header), separate from the consumer JWT-based API.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user_from_api_key
from app.api.translation_errors import translate_or_http_error
from app.core.database import get_db
from app.core.tiers import Tier, get_tier_config
from app.models.api_key import ApiKey
from app.models.subscription import SubscriptionProduct
from app.models.user import User
from app.schemas.translate import SUPPORTED_LANGUAGE_CODES
from app.services.stripe_b2b import (
    generate_meter_idempotency_key,
    report_translation_meter_event,
)
from app.services.subscription import get_or_create_subscription, get_usage_status, record_usage
from app.services.translation import translate_text
from app.services.usage_alerts import maybe_send_usage_alerts

logger = logging.getLogger(__name__)
router = APIRouter()

# Supported Swiss German dialects (same as consumer API)
SWISS_DIALECTS = {"bern", "zurich", "basel", "stgallen", "wallis", "luzern"}

SUPPORTED_LANGUAGES = [
    {"code": "de", "name": "German", "native_name": "Deutsch"},
    {"code": "gsw", "name": "Swiss German", "native_name": "Schwyzerdütsch"},
    {"code": "fr", "name": "French", "native_name": "Français"},
    {"code": "it", "name": "Italian", "native_name": "Italiano"},
    {"code": "en", "name": "English", "native_name": "English"},
    {"code": "rm", "name": "Romansh", "native_name": "Rumantsch"},
]


# --- Schemas ---


class PublicTranslateRequest(BaseModel):
    """Translation request for the public API."""

    text: str = Field(
        ...,
        min_length=1,
        description=(
            "Text to translate, up to 10,000 characters per request."
        ),
        examples=["Grüezi mitenand! Schön, dass ihr da sind."],
    )
    source_lang: str = Field(
        ...,
        min_length=2,
        max_length=4,
        description=(
            "ISO 639-1/3 source language code, or `auto` to let the engine "
            "detect the source language. Supported codes: `de`, `gsw`, "
            "`fr`, `it`, `en`, `rm`."
        ),
        examples=["gsw"],
    )
    target_lang: str = Field(
        ...,
        min_length=2,
        max_length=3,
        description=(
            "ISO 639-1/3 target language code. Supported codes: `de`, "
            "`gsw`, `fr`, `it`, `en`, `rm`. Must differ from `source_lang`."
        ),
        examples=["en"],
    )
    formality: str = Field(
        default="auto",
        description=(
            "Address formality for languages with a T/V distinction "
            "(German, French, Italian). One of `auto`, `informal`, "
            "`formal`. Ignored for English and Swiss German."
        ),
        examples=["auto"],
    )
    dialect: str | None = Field(
        default=None,
        description=(
            "Swiss German dialect to use when `target_lang` is `gsw`. "
            "One of `bern`, `zurich`, `basel`, `stgallen`, `wallis`, "
            "`luzern`. Ignored for other target languages."
        ),
        examples=["bern"],
    )


class PublicTranslateResponse(BaseModel):
    """Translation result from the public API."""

    translation: str = Field(
        ...,
        description="Translated text in the target language.",
        examples=["Hello everyone! Glad you're here."],
    )
    source_lang: str = Field(
        ...,
        description=(
            "Source language code that was used. If you sent `auto`, this "
            "is the detected language."
        ),
        examples=["gsw"],
    )
    target_lang: str = Field(..., description="Target language code.", examples=["en"])
    detected_source_lang: str | None = Field(
        default=None,
        description=(
            "Populated only when the request used `source_lang=auto`. "
            "Contains the language code the engine inferred."
        ),
    )
    characters: int = Field(
        ...,
        description=(
            "Number of characters in the input text. Counts against your "
            "monthly quota."
        ),
        examples=[42],
    )


class PublicLanguageResponse(BaseModel):
    """Language entry in the supported languages list."""

    code: str = Field(..., description="ISO language code.", examples=["gsw"])
    name: str = Field(..., description="English name.", examples=["Swiss German"])
    native_name: str = Field(
        ..., description="Native-language name.", examples=["Schwyzerdütsch"]
    )


class PublicUsageResponse(BaseModel):
    """Current usage status for the authenticated API key owner."""

    characters_used: int = Field(
        ...,
        description="Characters consumed in the current billing period.",
        examples=[12450],
    )
    characters_limit: int = Field(
        ...,
        description=(
            "Total characters included in your subscription for this "
            "period. Usage beyond this is billed as overage at your "
            "tier's per-million rate."
        ),
        examples=[500000],
    )
    characters_remaining: int = Field(
        ...,
        description=(
            "Characters still included before overage starts. May be 0 "
            "or negative once you exceed the included quota."
        ),
        examples=[487550],
    )
    period_start: str | None = Field(
        default=None,
        description="ISO-8601 timestamp marking the start of the billing period.",
    )
    period_end: str | None = Field(
        default=None,
        description="ISO-8601 timestamp marking the end of the billing period.",
    )


class PublicErrorDetail(BaseModel):
    """Error response body shape returned under the `detail` key for 4xx/5xx."""

    code: str = Field(
        ...,
        description=(
            "Stable machine-readable error code. Common values: "
            "`UNSUPPORTED_LANGUAGE`, `UNSUPPORTED_DIALECT`, `TEXT_TOO_LONG`, "
            "`USAGE_LIMIT_EXCEEDED`, `SUSPICIOUS_OUTPUT`, "
            "`INVALID_API_KEY`, `INTERNAL_ERROR`."
        ),
        examples=["TEXT_TOO_LONG"],
    )
    message: str = Field(
        ...,
        description="Human-readable description of the error.",
        examples=["Text exceeds 10000 character limit."],
    )
    detail: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context (e.g. the offending limit).",
    )


# --- Routes ---


@router.post(
    "/translate",
    response_model=PublicTranslateResponse,
    summary="Translate text",
    responses={
        400: {
            "model": PublicErrorDetail,
            "description": "Unsupported language code or dialect.",
        },
        401: {
            "model": PublicErrorDetail,
            "description": "Missing or invalid API key.",
        },
        422: {
            "model": PublicErrorDetail,
            "description": (
                "Translation rejected after one automatic retry: "
                "`SUSPICIOUS_OUTPUT`, `NAME_SUBSTITUTION`, `PLACEHOLDER_LEAK`, "
                "`UNPARSEABLE_OUTPUT` or `TRANSLATION_INCOMPLETE`. Retry, or "
                "simplify or shorten the input."
            ),
        },
        429: {
            "model": PublicErrorDetail,
            "description": "Monthly character limit exceeded for this subscription.",
        },
        500: {
            "model": PublicErrorDetail,
            "description": "Internal translation service error.",
        },
        503: {
            "model": PublicErrorDetail,
            "description": (
                "Translation model unavailable (`UPSTREAM_UNAVAILABLE`) or busy "
                "(`UPSTREAM_BUSY`, honour the Retry-After header)."
            ),
        },
        504: {
            "model": PublicErrorDetail,
            "description": "Translation took too long (`TRANSLATION_TIMEOUT`).",
        },
    },
)
async def public_translate(
    request: PublicTranslateRequest,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
) -> PublicTranslateResponse:
    """
    Translate text from one supported language to another.

    Authenticate with your API key via the `X-API-Key` header. The
    request body specifies the source and target language codes, the
    text to translate, and optionally a formality preference (for
    German, French, Italian) or a Swiss German dialect (when
    `target_lang` is `gsw`).

    Each successful translation counts toward your monthly character
    quota. See `GET /usage` for current consumption.
    """
    user, api_key = auth

    # Validate language codes
    if request.source_lang != "auto" and request.source_lang not in SUPPORTED_LANGUAGE_CODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSUPPORTED_LANGUAGE",
                "message": f"'{request.source_lang}' is not supported.",
            },
        )
    if request.target_lang not in SUPPORTED_LANGUAGE_CODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSUPPORTED_LANGUAGE",
                "message": f"'{request.target_lang}' is not supported.",
            },
        )

    # Validate dialect
    if request.dialect and request.dialect not in SWISS_DIALECTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSUPPORTED_DIALECT",
                "message": f"'{request.dialect}' is not a supported dialect.",
            },
        )

    # Get B2B subscription and tier config
    subscription = await get_or_create_subscription(db, user.id, SubscriptionProduct.B2B)
    tier = Tier(subscription.tier.value)
    config = get_tier_config(tier, product="b2b")
    text_length = len(request.text)

    # Enforce per-request character limit
    if text_length > config.max_chars_per_request:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "TEXT_TOO_LONG",
                "message": f"Text exceeds {config.max_chars_per_request} character limit.",
                "limit": config.max_chars_per_request,
            },
        )

    # Read-only usage check up front (no DB writes) — refusing here means
    # we don't waste an Apertus call on a request we wouldn't bill anyway.
    usage_status = await get_usage_status(db, user.id)
    if not usage_status.can_translate:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "USAGE_LIMIT_EXCEEDED",
                "message": "Monthly character limit exceeded.",
                "characters_used": usage_status.characters_used,
                "characters_limit": usage_status.characters_limit,
            },
        )

    # Translate FIRST. If the translation fails the error unwinds and the
    # outer get_db() rolls back the (empty) transaction, so no usage is
    # billed for a failed request (helvetra/backend#103). Failures map to the
    # same structured errors as the web routes (helvetra/backend#132);
    # anything unexpected is a generic 500 that never echoes internals.
    result = await translate_or_http_error(
        translate_text(
            text=request.text,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            formality=request.formality,
            dialect=request.dialect,
        )
    )

    # Only record usage and meter the customer for a successful response.
    usage_status = await record_usage(db, user.id, text_length)

    await db.commit()

    # Report usage to Stripe. Awaited inline (rather than fire-and-forget
    # via asyncio.create_task) because the task otherwise gets cancelled
    # when the request scope unwinds or the process restarts, silently
    # losing meter events — i.e. unbilled translations. The meter call is
    # sub-100ms and tolerates failure (logged inside the helper) so the
    # latency cost is acceptable. See helvetra/backend#112.
    await report_translation_meter_event(
        stripe_customer_id=user.stripe_customer_id,
        characters=text_length,
        idempotency_key=generate_meter_idempotency_key(user.id, text_length),
    )

    # Fire usage-alert emails for newly-crossed thresholds. The
    # helper is fast in the common case (no thresholds crossed →
    # one indexed read + early return); only the rare crossing
    # translation pays the SMTP cost. Errors are swallowed inside
    # the helper so they cannot block the API response.
    await maybe_send_usage_alerts(db, user, SubscriptionProduct.B2B)

    return PublicTranslateResponse(
        translation=result.translation,
        source_lang=result.detected_source_lang or request.source_lang,
        target_lang=request.target_lang,
        detected_source_lang=result.detected_source_lang,
        characters=text_length,
    )


@router.get(
    "/languages",
    response_model=list[PublicLanguageResponse],
    summary="List supported languages",
    responses={401: {"model": PublicErrorDetail}},
)
async def public_languages(
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
) -> list[PublicLanguageResponse]:
    """
    Return all language codes the API can translate between.

    The set is stable and rarely changes; you can cache the response on
    your side. Swiss German (`gsw`) supports multiple regional dialects
    selectable via the `dialect` field on the translate endpoint.
    """
    return [PublicLanguageResponse(**lang) for lang in SUPPORTED_LANGUAGES]


@router.get(
    "/usage",
    response_model=PublicUsageResponse,
    summary="Get current usage",
    responses={401: {"model": PublicErrorDetail}},
)
async def public_usage(
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
) -> PublicUsageResponse:
    """
    Return how many characters this account has translated in the
    current billing period, along with the included quota and the
    remaining balance.

    Use this to surface usage in your dashboard, send your own alerts
    before hitting overage, or decide when to throttle non-essential
    workloads.
    """
    user, _ = auth

    usage = await get_usage_status(db, user.id)
    remaining = max(0, usage.characters_limit - usage.characters_used)

    return PublicUsageResponse(
        characters_used=usage.characters_used,
        characters_limit=usage.characters_limit,
        characters_remaining=remaining,
        period_start=usage.period_start.isoformat() if usage.period_start else None,
        period_end=usage.period_end.isoformat() if usage.period_end else None,
    )
