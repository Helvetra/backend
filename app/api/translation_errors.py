"""
Translation error mapping.
Turns translation failures into the structured HTTP errors that every
translation endpoint returns, so clients see the same codes everywhere.
"""

import logging

import httpx
from fastapi import HTTPException, status

from app.services.translation import (
    TranslationIncompleteError,
    TranslationTimeoutError,
    TranslationValidationError,
)

logger = logging.getLogger(__name__)


async def translate_or_http_error(coro):
    """
    Await a translation coroutine and map domain failures to HTTP responses,
    so every translation endpoint (web, partial, public API) surfaces the same
    structured errors.
    """
    try:
        return await coro
    except TranslationTimeoutError as e:
        logger.warning("Translation timed out: %s", e)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": "TRANSLATION_TIMEOUT",
                "message": (
                    "The translation took too long. "
                    "Please try again, or translate a shorter text."
                ),
            },
        )
    except TranslationIncompleteError as e:
        logger.warning("Translation incomplete: %s", e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "TRANSLATION_INCOMPLETE",
                "message": (
                    "This text is too long to translate in one go. "
                    "Please split it into shorter parts."
                ),
            },
        )
    except TranslationValidationError as e:
        logger.warning("Translation validation rejected (%s): %s", e.code, e.detail)
        message = (
            "Translation rejected due to suspicious output. "
            "Please provide text to translate, not instructions."
            if e.code == "SUSPICIOUS_OUTPUT"
            else "We couldn't produce a clean translation. "
            "Please try again, or rephrase the text."
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": e.code, "message": message},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 429:
            raise _upstream_unavailable(e)
        # The provider's shared rate limit is exhausted even after a retry.
        # Tell clients to back off for a while instead of retrying per keystroke.
        logger.error("Upstream translation API rate limited after retry: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "UPSTREAM_BUSY",
                "message": (
                    "The translation service is busy right now. "
                    "Please try again in a few seconds."
                ),
                "retry_after": UPSTREAM_BUSY_RETRY_AFTER_SECONDS,
            },
            headers={"Retry-After": str(UPSTREAM_BUSY_RETRY_AFTER_SECONDS)},
        )
    except httpx.HTTPError as e:
        raise _upstream_unavailable(e)


# How long clients should wait after the model provider's rate limit refused
# a request twice. Its budget has been observed to refill within a minute.
UPSTREAM_BUSY_RETRY_AFTER_SECONDS = 10


def _upstream_unavailable(e: httpx.HTTPError) -> HTTPException:
    """Build the response for an upstream model API failure."""
    # Upstream model API failure is not our 500: tell clients the service
    # is temporarily unavailable so they can show a real message.
    logger.error(f"Upstream translation API error: {e}")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": (
                "The translation service is temporarily unavailable. "
                "Please try again in a moment."
            ),
        },
    )
