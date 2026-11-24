"""
Server-side language detection service.
Uses langdetect as fallback/validation for LLM detection.
"""

import logging

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

logger = logging.getLogger(__name__)

# Make detection deterministic
DetectorFactory.seed = 0

# Map langdetect codes to our language codes
LANG_MAP = {
    "en": "en",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "sq": "sq",
    "pt": "pt",
    "es": "es",
    "hr": "hr",
    # langdetect doesn't support Swiss German (gsw), Romansh (rm) or
    # Serbian (sr); these fall back to LLM detection
}

# Reverse mapping for validation
OUR_CODES = {"en", "de", "fr", "it", "gsw", "rm", "sq", "pt", "es", "hr", "sr"}

# Minimum text length for reliable detection
MIN_LENGTH = 20

# Minimum confidence threshold
MIN_CONFIDENCE = 0.5


def detect_language(text: str) -> str | None:
    """
    Detect language from text.
    Returns language code if detected with confidence, None otherwise.

    Note: Swiss German (gsw), Romansh (rm) and Serbian (sr) are not supported
    by langdetect.
    These should be detected by the LLM.
    """
    if not text or len(text) < MIN_LENGTH:
        return None

    try:
        detections = detect_langs(text)
        if not detections:
            return None

        # Get highest confidence detection
        best = detections[0]
        if best.prob < MIN_CONFIDENCE:
            return None

        # Map to our language code
        return LANG_MAP.get(best.lang)

    except LangDetectException:
        return None
    except Exception as e:
        logger.warning(f"Language detection failed: {e}")
        return None


def validate_llm_detection(
    text: str,
    llm_detected: str,
    target_lang: str,
) -> str:
    """
    Validate LLM detection result.
    If LLM returned the same language as target (suspicious), use langdetect instead.

    Returns the validated language code.
    """
    # If LLM detected same as target, it might be wrong
    if llm_detected == target_lang:
        our_detection = detect_language(text)
        if our_detection and our_detection != target_lang:
            logger.info(
                f"Overriding LLM detection '{llm_detected}' with langdetect '{our_detection}'"
            )
            return our_detection

    return llm_detected
