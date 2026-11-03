"""
Payment endpoints.
Handles checkout session creation for both consumer and B2B subscriptions.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.core.tiers import Tier
from app.models.user import User
from app.services.stripe_b2b import (
    create_b2b_checkout_session,
    create_billing_portal_session,
)
from app.services.stripe_service import (
    create_checkout_session,
    get_or_create_stripe_customer,
)

router = APIRouter(prefix="/payments")

# B2B redirect URLs after Checkout completes or is cancelled. Lives under
# /developers because /api/* is the backend API prefix and nginx proxies
# everything matching /api/ to FastAPI rather than to the Nuxt frontend.
B2B_SUCCESS_URL = "https://helvetra.ch/developers/success"
B2B_CANCEL_URL = "https://helvetra.ch/developers/cancel"
B2B_PORTAL_RETURN_URL = "https://helvetra.ch/developers/dashboard"


class CreateGatewayRequest(BaseModel):
    """Request to create a payment gateway."""

    billing_period: str  # "monthly" or "yearly"


class CreateGatewayResponse(BaseModel):
    """Response with gateway URL."""

    success: bool
    gateway_url: str | None = None
    error: str | None = None


class CreateB2BGatewayRequest(BaseModel):
    """Request to create a B2B subscription checkout."""

    tier: str  # "business" (the only B2B plan)


@router.post("/create-gateway", response_model=CreateGatewayResponse)
async def create_payment_gateway(
    request: CreateGatewayRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreateGatewayResponse:
    """Create a Stripe Checkout Session for subscription purchase."""
    if request.billing_period not in ("monthly", "yearly"):
        raise HTTPException(
            status_code=400,
            detail="Invalid billing period. Must be 'monthly' or 'yearly'.",
        )

    result = await create_checkout_session(
        db=db,
        user=user,
        billing_period=request.billing_period,
    )

    if not result.success:
        return CreateGatewayResponse(
            success=False,
            error=result.error,
        )

    return CreateGatewayResponse(
        success=True,
        gateway_url=result.gateway_url,
    )


class PortalResponse(BaseModel):
    """Response with a Stripe billing portal URL."""

    success: bool
    portal_url: str | None = None
    error: str | None = None


@router.post("/create-b2b-gateway", response_model=CreateGatewayResponse)
async def create_b2b_payment_gateway(
    request: CreateB2BGatewayRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreateGatewayResponse:
    """Create a Stripe Checkout Session for the B2B subscription."""
    if request.tier != "business":
        raise HTTPException(
            status_code=400,
            detail="Invalid tier. Must be 'business'.",
        )

    tier = Tier(request.tier)
    customer_id = await get_or_create_stripe_customer(db, user)
    await db.commit()  # Persist new Stripe customer ID before redirecting

    result = create_b2b_checkout_session(
        customer_id=customer_id,
        tier=tier,
        success_url=B2B_SUCCESS_URL,
        cancel_url=B2B_CANCEL_URL,
    )

    if not result.success:
        return CreateGatewayResponse(success=False, error=result.error)

    return CreateGatewayResponse(success=True, gateway_url=result.gateway_url)


@router.post("/b2b-portal", response_model=PortalResponse)
async def create_b2b_billing_portal(
    user: User = Depends(get_current_user),
) -> PortalResponse:
    """Create a Stripe billing portal session for the authenticated B2B customer."""
    if not user.stripe_customer_id:
        return PortalResponse(success=False, error="No Stripe customer on file")

    portal_url = create_billing_portal_session(
        customer_id=user.stripe_customer_id,
        return_url=B2B_PORTAL_RETURN_URL,
    )
    if not portal_url:
        return PortalResponse(success=False, error="Could not start billing portal")

    return PortalResponse(success=True, portal_url=portal_url)
