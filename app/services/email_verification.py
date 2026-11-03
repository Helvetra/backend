"""
Email verification service.
Handles token generation, validation, and verification flow.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import EmailVerificationToken, User
from app.services.email import email_service

settings = get_settings()


def generate_verification_token() -> tuple[str, str]:
    """Generate a secure verification token. Returns (raw_token, hashed_token)."""
    raw_token = secrets.token_urlsafe(32)
    hashed_token = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, hashed_token


def hash_token(token: str) -> str:
    """Hash a token for storage/lookup."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_verification_token(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Create a new verification token for a user. Returns the raw token."""
    # Invalidate any existing tokens for this user
    await db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user_id)
        .where(EmailVerificationToken.used == False)  # noqa: E712
        .values(used=True)
    )

    # Generate new token
    raw_token, hashed_token = generate_verification_token()

    # Calculate expiry
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.email_verification_expire_hours
    )

    # Store token
    verification_token = EmailVerificationToken(
        user_id=user_id,
        token_hash=hashed_token,
        expires_at=expires_at,
    )
    db.add(verification_token)
    await db.flush()

    return raw_token


async def send_verification_email(
    db: AsyncSession, user: User, locale: str | None = None
) -> bool:
    """Generate token and send verification email to user."""
    raw_token = await create_verification_token(db, user.id)
    return email_service.send_verification_email(user.email, raw_token, locale)


async def verify_email_token(db: AsyncSession, token: str) -> User | None:
    """
    Verify an email token and mark the user's email as verified.
    Returns the user if successful, None if invalid/expired.
    """
    token_hash = hash_token(token)

    # Find the token
    result = await db.execute(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_hash)
        .where(EmailVerificationToken.used == False)  # noqa: E712
    )
    stored_token = result.scalar_one_or_none()

    if not stored_token:
        return None

    # Check expiry
    if stored_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None

    # Mark token as used
    stored_token.used = True

    # Get and update user
    user_result = await db.execute(select(User).where(User.id == stored_token.user_id))
    user = user_result.scalar_one_or_none()

    if not user:
        return None

    # Mark email as verified
    user.email_verified = True
    await db.flush()

    return user


async def can_resend_verification(db: AsyncSession, user_id: uuid.UUID) -> tuple[bool, int]:
    """
    Check if user can request another verification email.
    Returns (can_resend, seconds_until_can_resend).
    Rate limit: 1 email per 60 seconds.
    """
    result = await db.execute(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user_id)
        .order_by(EmailVerificationToken.created_at.desc())
        .limit(1)
    )
    last_token = result.scalar_one_or_none()

    if not last_token:
        return True, 0

    # Allow resend after 60 seconds
    cooldown_seconds = 60
    last_sent = last_token.created_at.replace(tzinfo=timezone.utc)
    time_since_last = datetime.now(timezone.utc) - last_sent

    if time_since_last.total_seconds() < cooldown_seconds:
        remaining = cooldown_seconds - int(time_since_last.total_seconds())
        return False, remaining

    return True, 0
