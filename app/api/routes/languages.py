"""
Languages endpoint.
Returns the list of supported translation languages.
"""

from fastapi import APIRouter

from app.schemas.translate import SUPPORTED_LANGUAGES

router = APIRouter()


@router.get("/languages")
async def get_languages() -> dict:
    """Return all supported languages for translation."""
    return {
        "success": True,
        "data": SUPPORTED_LANGUAGES,
    }
