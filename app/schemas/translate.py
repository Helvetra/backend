"""
Translation request and response schemas.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# Supported language codes for translation
SUPPORTED_LANGUAGE_CODES = {"de", "gsw", "fr", "it", "en", "rm"}

# Supported Swiss German dialects
SwissDialect = Literal["bern", "zurich", "basel", "stgallen", "wallis", "luzern"]


class TranslateRequest(BaseModel):
    """Incoming translation request."""

    # Sanity ceiling only — must stay above the largest tier's per-request
    # limit (B2B BUSINESS: 50k). The real per-user limit is enforced by the
    # tier check in the route, AFTER authentication. A tighter cap here runs
    # before tier logic and rejects paying users regardless of plan.
    text: str = Field(..., min_length=1, max_length=50_000)
    source_lang: str = Field(..., min_length=2, max_length=4)
    target_lang: str = Field(..., min_length=2, max_length=3)
    formality: Literal["informal", "formal", "auto"] = Field(
        default="auto", description="Formality level for German translations (du/Sie)"
    )
    dialect: SwissDialect | None = Field(
        default=None, description="Swiss German dialect (only used when target_lang is gsw)"
    )


class PartialTranslateRequest(BaseModel):
    """Request to translate a single segment using surrounding text as context."""

    # The one segment to translate. Sanity ceiling only; the per-request tier
    # limit is enforced in the route against this length.
    segment: str = Field(..., min_length=1, max_length=50_000)
    # Surrounding context, not translated and not charged. Capped for safety.
    context_before: str = Field(default="", max_length=50_000)
    context_after: str = Field(default="", max_length=50_000)
    # Partial translation needs an explicit source; "auto" is rejected in the route.
    source_lang: str = Field(..., min_length=2, max_length=4)
    target_lang: str = Field(..., min_length=2, max_length=3)
    formality: Literal["informal", "formal", "auto"] = Field(default="auto")
    dialect: SwissDialect | None = Field(default=None)


class TranslateResponse(BaseModel):
    """Translation response with result or error."""

    success: bool
    data: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    error: dict[str, str] | None = None
