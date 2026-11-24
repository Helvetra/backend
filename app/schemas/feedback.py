"""
Feedback request and response schemas.
"""

from pydantic import BaseModel, Field


class FeedbackRequest(BaseModel):
    """User feedback on a translation."""

    vote: str = Field(..., pattern="^(like|dislike)$")
    consent: bool
    source_text: str = Field(..., min_length=1, max_length=5000)
    source_lang: str = Field(..., pattern="^(en|de|gsw|fr|it|rm|sq|pt|es|hr|sr)$")
    translated_text: str = Field(..., min_length=1, max_length=15000)
    target_lang: str = Field(..., pattern="^(en|de|gsw|fr|it|rm|sq|pt|es|hr|sr)$")
    region: str | None = Field(
        None,
        pattern="^(bern|zurich|basel|stgallen|wallis|luzern)$",
        description="Swiss German dialect region (optional, for gsw translations)",
    )
    comment: str | None = Field(
        None,
        max_length=1000,
        description="Optional user comment explaining their feedback",
    )


class FeedbackResponse(BaseModel):
    """Feedback submission response."""

    success: bool
    error: dict[str, str] | None = None
