"""
API dependencies for route injection.
Provides JWT auth for consumer API and API key auth for the B2B public API.
"""

import ipaddress

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.api_key import ApiKey
from app.models.user import User
from app.services.auth import decode_access_token

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


def get_client_ip(request: Request) -> str:
    """Identify the client's network for limits and logs, using trusted proxy headers.

    Uses X-Real-IP set by nginx, which cannot be spoofed by clients, and falls
    back to the direct connection IP for local development. IPv4 addresses are
    returned as-is. IPv6 addresses are reduced to their /64 network: devices
    rotate addresses within it (privacy extensions), so per-address limits
    would hand out a fresh quota with every new address. See helvetra/infra#22.
    """
    # X-Real-IP is set by nginx from the actual TCP connection
    real_ip = request.headers.get("X-Real-IP")
    raw = real_ip.strip() if real_ip else (request.client.host if request.client else "unknown")
    return client_network(raw)


def client_network(address: str) -> str:
    """Return an IPv4 address unchanged and an IPv6 address as its /64 network."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate user from JWT access token."""
    token = credentials.credentials
    user_id = decode_access_token(token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_user_from_api_key(
    x_api_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> tuple[User, ApiKey]:
    """Authenticate via X-API-Key header for the B2B public API."""
    from app.services.api_key import resolve_api_key

    api_key = await resolve_api_key(db, x_api_key)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key owner not found",
        )

    return user, api_key


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Extract user from JWT if present, return None for anonymous requests.

    A request without credentials is a legitimate anonymous request. A request
    WITH credentials that fail validation is rejected instead of being silently
    treated as anonymous — otherwise a paying user with an expired access token
    would be downgraded to anonymous limits without any signal to refresh.
    """
    if credentials is None:
        return None

    token = credentials.credentials
    user_id = decode_access_token(token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_EXPIRED",
                "message": "Access token is invalid or expired. Refresh and retry.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_INVALID",
                "message": "Token does not match a known user.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user
