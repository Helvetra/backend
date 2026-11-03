"""
Authentication request and response schemas.
"""

import re
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.services.auth import is_common_password


class RegisterRequest(BaseModel):
    """User registration request."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    locale: str | None = Field(
        default=None, description="User's preferred language (en, de, fr, it)"
    )
    use_cookie: bool = Field(
        default=False, description="Store refresh token in HttpOnly cookie instead of body"
    )

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Ensure password meets minimum security requirements."""
        if not re.search(r"[A-Za-z]", v):
            raise ValueError("Password must contain at least one letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one number")
        if is_common_password(v):
            raise ValueError("Password is too common. Please choose a stronger password")
        return v


class LoginRequest(BaseModel):
    """User login request."""

    email: EmailStr
    password: str
    use_cookie: bool = Field(
        default=False, description="Store refresh token in HttpOnly cookie instead of body"
    )


class RefreshRequest(BaseModel):
    """Token refresh request."""

    refresh_token: str


class TokenResponse(BaseModel):
    """Authentication token response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token expiry in seconds")


class UserResponse(BaseModel):
    """User profile response."""

    id: UUID
    email: str
    email_verified: bool
    tier: str


class AuthResponse(BaseModel):
    """Standard auth response wrapper."""

    success: bool
    data: dict | None = None
    error: dict[str, str] | None = None


class MessageResponse(BaseModel):
    """Simple message response for logout etc."""

    success: bool
    message: str


class VerifyEmailRequest(BaseModel):
    """Email verification request."""

    token: str


class ResendVerificationRequest(BaseModel):
    """Resend verification email request."""

    email: EmailStr
    locale: str | None = Field(
        default=None, description="User's preferred language (en, de, fr, it)"
    )


class AppleSignInRequest(BaseModel):
    """Apple Sign-In request with identity token from iOS."""

    identity_token: str = Field(..., description="JWT identity token from Apple Sign-In")
    user_name: str | None = Field(
        default=None, description="User's name (only provided on first sign-in)"
    )
    use_cookie: bool = Field(
        default=False, description="Store refresh token in HttpOnly cookie"
    )
