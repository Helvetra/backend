"""
Authentication endpoints.
Handles user registration, login, token refresh, and logout.
"""

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_client_ip, get_current_user
from app.config import get_settings
from app.core.database import get_db
from app.models.user import RefreshToken, User
from app.schemas.auth import (
    AppleSignInRequest,
    AuthResponse,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.services.audit_log import AuthEvent, log_auth_event
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    get_refresh_token_expiry,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.services.auth_rate_limiter import auth_rate_limiter
from app.services.csrf import (
    clear_csrf_cookie,
    issue_csrf_token,
    require_csrf,
    set_csrf_cookie,
)

settings = get_settings()
router = APIRouter(prefix="/auth")


def get_user_agent(request: Request) -> str | None:
    """Extract user agent from request."""
    return request.headers.get("User-Agent")


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Register a new user with email and password."""
    client_ip = get_client_ip(http_request)
    user_agent = get_user_agent(http_request)

    # Check registration rate limit (10/hour per IP)
    rate_result = await auth_rate_limiter.check_register_limit(client_ip)
    if not rate_result.allowed:
        log_auth_event(
            AuthEvent.RATE_LIMITED,
            client_ip,
            request.email,
            user_agent,
            {"endpoint": "register"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a moment.",
            headers={"Retry-After": str(rate_result.retry_after)},
        )

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == request.email))
    if result.scalar_one_or_none():
        log_auth_event(
            AuthEvent.REGISTER_FAILED,
            client_ip,
            request.email,
            user_agent,
            {"reason": "email_exists"},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Create user
    user = User(
        email=request.email,
        password_hash=hash_password(request.password),
    )
    db.add(user)
    await db.flush()

    # Send verification email
    from app.services.email_verification import send_verification_email

    await send_verification_email(db, user, request.locale)

    # Create tokens
    access_token = create_access_token(user.id)
    raw_refresh, hashed_refresh = create_refresh_token()

    refresh_token = RefreshToken(
        user_id=user.id,
        token_hash=hashed_refresh,
        expires_at=get_refresh_token_expiry(),
    )
    db.add(refresh_token)

    log_auth_event(AuthEvent.REGISTER_SUCCESS, client_ip, request.email, user_agent)

    # New users always start on free tier
    user_payload = UserResponse(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        tier="free",
    ).model_dump()

    if request.use_cookie:
        max_age = settings.refresh_token_expire_days * 24 * 60 * 60
        response.set_cookie(
            key="refresh_token",
            value=raw_refresh,
            httponly=True,
            secure=not settings.debug,
            samesite="strict",
            max_age=max_age,
            path="/api/v1/auth",
        )
        set_csrf_cookie(response, issue_csrf_token(), max_age)
        return AuthResponse(
            success=True,
            data={
                "user": user_payload,
                "tokens": TokenResponse(
                    access_token=access_token,
                    refresh_token="",  # stored in cookie
                    expires_in=settings.access_token_expire_minutes * 60,
                ).model_dump(),
            },
        )

    return AuthResponse(
        success=True,
        data={
            "user": user_payload,
            "tokens": TokenResponse(
                access_token=access_token,
                refresh_token=raw_refresh,
                expires_in=settings.access_token_expire_minutes * 60,
            ).model_dump(),
        },
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    request: LoginRequest,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Authenticate user with email and password."""
    client_ip = get_client_ip(http_request)
    user_agent = get_user_agent(http_request)

    # Check login rate limit (10/min per IP)
    rate_result = await auth_rate_limiter.check_login_limit(client_ip)
    if not rate_result.allowed:
        log_auth_event(
            AuthEvent.RATE_LIMITED,
            client_ip,
            request.email,
            user_agent,
            {"endpoint": "login"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a moment.",
            headers={"Retry-After": str(rate_result.retry_after)},
        )

    # Check if account is locked out
    lockout_result = await auth_rate_limiter.check_account_lockout(request.email, client_ip)
    if lockout_result.locked_out:
        log_auth_event(
            AuthEvent.ACCOUNT_LOCKED,
            client_ip,
            request.email,
            user_agent,
            {"lockout_remaining": lockout_result.lockout_remaining},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Account temporarily locked. Retry in {lockout_result.lockout_remaining}s.",
            headers={"Retry-After": str(lockout_result.lockout_remaining)},
        )

    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        # Record failed attempt for brute force protection
        failed_result = await auth_rate_limiter.record_failed_attempt(request.email, client_ip)
        log_auth_event(
            AuthEvent.LOGIN_FAILED,
            client_ip,
            request.email,
            user_agent,
            {"reason": "invalid_credentials", "attempts_remaining": failed_result.remaining},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not verify_password(request.password, user.password_hash):
        # Record failed attempt for brute force protection
        failed_result = await auth_rate_limiter.record_failed_attempt(request.email, client_ip)
        log_auth_event(
            AuthEvent.LOGIN_FAILED,
            client_ip,
            request.email,
            user_agent,
            {"reason": "invalid_password", "attempts_remaining": failed_result.remaining},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Clear failed attempts on successful login
    await auth_rate_limiter.clear_failed_attempts(request.email, client_ip)
    log_auth_event(AuthEvent.LOGIN_SUCCESS, client_ip, request.email, user_agent)

    # Get subscription tier
    from app.services.subscription import get_or_create_subscription

    subscription = await get_or_create_subscription(db, user.id)

    # Create tokens
    access_token = create_access_token(user.id)
    raw_refresh, hashed_refresh = create_refresh_token()

    refresh_token_obj = RefreshToken(
        user_id=user.id,
        token_hash=hashed_refresh,
        expires_at=get_refresh_token_expiry(),
    )
    db.add(refresh_token_obj)

    # Set refresh token as HttpOnly cookie if requested
    if request.use_cookie:
        max_age = settings.refresh_token_expire_days * 24 * 60 * 60
        response.set_cookie(
            key="refresh_token",
            value=raw_refresh,
            httponly=True,
            secure=not settings.debug,  # Secure in production
            samesite="strict",
            max_age=max_age,
            path="/api/v1/auth",
        )
        set_csrf_cookie(response, issue_csrf_token(), max_age)
        # Don't include refresh token in response body when using cookies
        return AuthResponse(
            success=True,
            data={
                "user": UserResponse(
                    id=user.id,
                    email=user.email,
                    email_verified=user.email_verified,
                    tier=subscription.tier.value,
                ).model_dump(),
                "tokens": TokenResponse(
                    access_token=access_token,
                    refresh_token="",  # Empty, stored in cookie
                    expires_in=settings.access_token_expire_minutes * 60,
                ).model_dump(),
            },
        )

    return AuthResponse(
        success=True,
        data={
            "user": UserResponse(
                id=user.id,
                email=user.email,
                email_verified=user.email_verified,
                tier=subscription.tier.value,
            ).model_dump(),
            "tokens": TokenResponse(
                access_token=access_token,
                refresh_token=raw_refresh,
                expires_in=settings.access_token_expire_minutes * 60,
            ).model_dump(),
        },
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: RefreshRequest | None = None,
    response: Response = None,
    refresh_token_cookie: str | None = Cookie(default=None, alias="refresh_token"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_csrf),
) -> AuthResponse:
    """Exchange a refresh token for new access and refresh tokens."""
    # Accept token from cookie or request body
    raw_token = refresh_token_cookie or (request.refresh_token if request else None)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token required",
        )

    token_hash = hash_refresh_token(raw_token)

    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .where(RefreshToken.revoked == False)  # noqa: E712
    )
    stored_token = result.scalar_one_or_none()

    if not stored_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    # Check expiry
    from datetime import datetime, timezone

    if stored_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )

    # Revoke old token (rotation)
    stored_token.revoked = True

    # Get user
    user_result = await db.execute(select(User).where(User.id == stored_token.user_id))
    user = user_result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    # Create new tokens
    access_token = create_access_token(user.id)
    raw_refresh, hashed_refresh = create_refresh_token()

    new_refresh_token = RefreshToken(
        user_id=user.id,
        token_hash=hashed_refresh,
        expires_at=get_refresh_token_expiry(),
    )
    db.add(new_refresh_token)

    # If refresh token came from cookie, update cookie with new token
    if refresh_token_cookie:
        max_age = settings.refresh_token_expire_days * 24 * 60 * 60
        response.set_cookie(
            key="refresh_token",
            value=raw_refresh,
            httponly=True,
            secure=not settings.debug,
            samesite="strict",
            max_age=max_age,
            path="/api/v1/auth",
        )
        set_csrf_cookie(response, issue_csrf_token(), max_age)
        return AuthResponse(
            success=True,
            data={
                "tokens": TokenResponse(
                    access_token=access_token,
                    refresh_token="",  # Empty, stored in cookie
                    expires_in=settings.access_token_expire_minutes * 60,
                ).model_dump(),
            },
        )

    return AuthResponse(
        success=True,
        data={
            "tokens": TokenResponse(
                access_token=access_token,
                refresh_token=raw_refresh,
                expires_in=settings.access_token_expire_minutes * 60,
            ).model_dump(),
        },
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: RefreshRequest | None = None,
    response: Response = None,
    refresh_token_cookie: str | None = Cookie(default=None, alias="refresh_token"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_csrf),
) -> MessageResponse:
    """Revoke a refresh token to log out."""
    # Accept token from cookie or request body
    raw_token = refresh_token_cookie or (request.refresh_token if request else None)

    if raw_token:
        token_hash = hash_refresh_token(raw_token)
        result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        stored_token = result.scalar_one_or_none()

        if stored_token:
            stored_token.revoked = True

    # Clear the refresh + csrf cookies if they were set
    if refresh_token_cookie:
        response.delete_cookie(key="refresh_token", path="/api/v1/auth")
        clear_csrf_cookie(response)

    return MessageResponse(success=True, message="Logged out successfully")


@router.get("/me", response_model=AuthResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Get current authenticated user profile."""
    from app.services.subscription import get_or_create_subscription

    subscription = await get_or_create_subscription(db, current_user.id)

    return AuthResponse(
        success=True,
        data={
            "user": UserResponse(
                id=current_user.id,
                email=current_user.email,
                email_verified=current_user.email_verified,
                tier=subscription.tier.value,
            ).model_dump(),
        },
    )


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    request: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Verify user's email address with token from verification email."""
    from app.services.email_verification import verify_email_token

    user = await verify_email_token(db, request.token)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )

    return MessageResponse(success=True, message="Email verified successfully")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    request: ResendVerificationRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Resend verification email to user."""
    from app.services.email_verification import can_resend_verification, send_verification_email

    # Find user by email
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user:
        return MessageResponse(
            success=True, message="If the email exists, a verification link has been sent"
        )

    # Already verified
    if user.email_verified:
        return MessageResponse(success=True, message="Email is already verified")

    # Check rate limit
    can_resend, retry_after = await can_resend_verification(db, user.id)
    if not can_resend:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {retry_after} seconds before requesting another email",
            headers={"Retry-After": str(retry_after)},
        )

    # Send verification email
    await send_verification_email(db, user, request.locale)

    return MessageResponse(success=True, message="Verification email sent")


@router.delete("/account", response_model=MessageResponse)
async def delete_account(
    http_request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_csrf),
) -> MessageResponse:
    """
    Delete user account and all associated data.
    Cancels any active subscription before deletion.
    """
    from app.models.subscription import SubscriptionSource, SubscriptionStatus
    from app.services.stripe_service import cancel_stripe_subscription

    client_ip = get_client_ip(http_request)
    user_agent = get_user_agent(http_request)

    # Get user's subscription to check for active payment subscription
    from app.services.subscription import get_or_create_subscription

    subscription = await get_or_create_subscription(db, current_user.id)

    # Cancel active subscription on the relevant provider. Legacy
    # PAYREXX rows (status set to CANCELLED in the 2026-05-13 cleanup)
    # are skipped; no new ones can be created.
    if subscription.status == SubscriptionStatus.ACTIVE and subscription.external_id:
        if subscription.source == SubscriptionSource.STRIPE:
            await cancel_stripe_subscription(subscription.external_id)

    # Log the deletion event before deleting
    log_auth_event(
        AuthEvent.ACCOUNT_DELETED,
        client_ip,
        current_user.email,
        user_agent,
    )

    # Delete user (cascades to all related data)
    await db.delete(current_user)

    # Clear any refresh token cookie
    response.delete_cookie(key="refresh_token", path="/api/v1/auth")

    return MessageResponse(success=True, message="Account deleted successfully")


@router.post("/apple", response_model=AuthResponse)
async def apple_sign_in(
    request: AppleSignInRequest,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """
    Authenticate with Apple Sign-In.
    Creates account on first sign-in, links to existing account if Apple ID matches.
    """
    from app.services.apple_auth import validate_identity_token

    client_ip = get_client_ip(http_request)
    user_agent = get_user_agent(http_request)

    # Validate Apple identity token
    apple_user = await validate_identity_token(request.identity_token)
    if not apple_user:
        log_auth_event(
            AuthEvent.LOGIN_FAILED,
            client_ip,
            None,
            user_agent,
            {"reason": "invalid_apple_token"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Apple identity token",
        )

    # Check if user exists by Apple ID
    result = await db.execute(select(User).where(User.apple_id == apple_user.apple_id))
    user = result.scalar_one_or_none()

    if not user and apple_user.email:
        # Auto-link Apple ID to an existing local account only when BOTH
        # sides confirm ownership of the email: Apple says email_verified=True
        # AND the local account already had email_verified=True. Otherwise
        # an attacker who controls an unverified local-account email could
        # piggy-back any Apple Sign-In that maps to the same address.
        # See helvetra/backend#104.
        result = await db.execute(select(User).where(User.email == apple_user.email))
        user = result.scalar_one_or_none()
        if user:
            if not (apple_user.email_verified and user.email_verified):
                log_auth_event(
                    AuthEvent.LOGIN_FAILED,
                    client_ip,
                    apple_user.email,
                    user_agent,
                    {"reason": "apple_link_unverified_email"},
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Verify your email on the existing account before linking Apple Sign-In",
                )
            user.apple_id = apple_user.apple_id

    if not user:
        # Create new user with Apple ID
        email = apple_user.email or f"{apple_user.apple_id}@privaterelay.appleid.com"
        user = User(
            email=email,
            apple_id=apple_user.apple_id,
            email_verified=apple_user.email_verified,
        )
        db.add(user)
        await db.flush()
        log_auth_event(AuthEvent.REGISTER_SUCCESS, client_ip, email, user_agent)

    log_auth_event(AuthEvent.LOGIN_SUCCESS, client_ip, user.email, user_agent)

    # Get subscription tier
    from app.services.subscription import get_or_create_subscription

    subscription = await get_or_create_subscription(db, user.id)

    # Create tokens
    access_token = create_access_token(user.id)
    raw_refresh, hashed_refresh = create_refresh_token()

    refresh_token_obj = RefreshToken(
        user_id=user.id,
        token_hash=hashed_refresh,
        expires_at=get_refresh_token_expiry(),
    )
    db.add(refresh_token_obj)

    # Set refresh token as HttpOnly cookie if requested
    if request.use_cookie:
        max_age = settings.refresh_token_expire_days * 24 * 60 * 60
        response.set_cookie(
            key="refresh_token",
            value=raw_refresh,
            httponly=True,
            secure=not settings.debug,
            samesite="strict",
            max_age=max_age,
            path="/api/v1/auth",
        )
        set_csrf_cookie(response, issue_csrf_token(), max_age)
        return AuthResponse(
            success=True,
            data={
                "user": UserResponse(
                    id=user.id,
                    email=user.email,
                    email_verified=user.email_verified,
                    tier=subscription.tier.value,
                ).model_dump(),
                "tokens": TokenResponse(
                    access_token=access_token,
                    refresh_token="",
                    expires_in=settings.access_token_expire_minutes * 60,
                ).model_dump(),
            },
        )

    return AuthResponse(
        success=True,
        data={
            "user": UserResponse(
                id=user.id,
                email=user.email,
                email_verified=user.email_verified,
                tier=subscription.tier.value,
            ).model_dump(),
            "tokens": TokenResponse(
                access_token=access_token,
                refresh_token=raw_refresh,
                expires_in=settings.access_token_expire_minutes * 60,
            ).model_dump(),
        },
    )
