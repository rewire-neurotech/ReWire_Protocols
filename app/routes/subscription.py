"""
Subscription routes for Jolt.

Supports:
  - Promo code redemption (server-side validation)
  - Subscription status check
  - Stripe Checkout for real payments
  - Stripe webhook for payment confirmation
  - Post-redirect session verification (fallback if webhook is slow)
  - Cancel subscription

When STRIPE_SECRET_KEY is not set, the purchase endpoint returns 503.
Promo code redemption always works regardless of Stripe config.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db, SessionLocal
from app.models import User, Subscription, PromoCode
from app.routes.auth import get_current_user_required

r = APIRouter(prefix="/api/subscription", tags=["subscription"])


def _as_aware_utc(dt):
    """
    Normalize a datetime to timezone-aware UTC.

    Postgres TIMESTAMP columns return naive datetimes, but we compare
    against datetime.now(timezone.utc) which is aware. Comparing naive
    and aware datetimes raises TypeError in Python, so we treat naive
    values as UTC (which is how they were written).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _frontend_base_url(request: Request) -> str:
    """
    Base URL to send the user back to after Stripe Checkout.

    Prefer the Origin header (the exact origin the user is on), then the
    Referer's origin, then PUBLIC_BASE_URL. This guarantees the user
    returns to the same origin that holds their login token in
    localStorage, instead of a mismatched domain.
    """
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin.startswith("http://") or origin.startswith("https://"):
        return origin

    referer = (request.headers.get("referer") or "").strip()
    if referer.startswith("http://") or referer.startswith("https://"):
        # Reduce referer to scheme://host[:port]
        try:
            from urllib.parse import urlsplit
            parts = urlsplit(referer)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except Exception:
            pass

    return cfg.PUBLIC_BASE_URL.rstrip("/") if cfg.PUBLIC_BASE_URL else ""


def _plan_expiry(plan: str) -> datetime:
    if plan == "yearly":
        return datetime.now(timezone.utc) + timedelta(days=365)
    return datetime.now(timezone.utc) + timedelta(days=30)


def _activate_subscription(db: Session, user_id: int, plan: str, session_id: str) -> str:
    """
    Create an active subscription for a paid Stripe Checkout session.
    Idempotent on stripe_session_id: safe to call from both the webhook
    and the /verify endpoint without creating duplicates.

    Returns: "created" | "exists"
    """
    existing = (
        db.query(Subscription)
        .filter(Subscription.stripe_session_id == session_id)
        .first()
    )
    if existing:
        return "exists"

    # Deactivate any existing active subscriptions
    old_subs = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id, Subscription.status == "active")
        .all()
    )
    for s in old_subs:
        s.status = "replaced"

    sub = Subscription(
        user_id=user_id,
        plan=plan,
        status="active",
        stripe_session_id=session_id,
        expires_at=_plan_expiry(plan),
    )
    db.add(sub)
    db.commit()
    print(f"[stripe] subscription created for user {user_id}, plan={plan}, session={session_id}")
    return "created"


# --- Request / Response schemas ---

class RedeemRequest(BaseModel):
    code: str


class PurchaseRequest(BaseModel):
    plan: str  # "monthly" or "yearly"


class VerifyRequest(BaseModel):
    session_id: str


class SubscriptionStatusResponse(BaseModel):
    has_subscription: bool
    plan: Optional[str] = None
    status: Optional[str] = None
    expires_at: Optional[str] = None


class RedeemResponse(BaseModel):
    status: str
    message: str


class PurchaseResponse(BaseModel):
    status: str
    message: str = ""
    checkout_url: Optional[str] = None


class VerifyResponse(BaseModel):
    status: str
    has_subscription: bool = False


class StatusResponse(BaseModel):
    status: str


# --- Endpoints ---

@r.get("/status", response_model=SubscriptionStatusResponse)
def get_status(
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Check if the current user has an active subscription."""
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active")
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if not sub:
        return SubscriptionStatusResponse(has_subscription=False)

    # Check if expired
    exp = _as_aware_utc(sub.expires_at)
    if exp and exp < datetime.now(timezone.utc):
        sub.status = "expired"
        db.commit()
        return SubscriptionStatusResponse(has_subscription=False)

    return SubscriptionStatusResponse(
        has_subscription=True,
        plan=sub.plan,
        status=sub.status,
        expires_at=sub.expires_at.isoformat() if sub.expires_at else None,
    )


@r.post("/redeem", response_model=RedeemResponse)
def redeem_code(
    req: RedeemRequest,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """
    Validate and redeem a promo code.
    Creates an active subscription if the code is valid.
    """
    code_str = (req.code or "").strip().upper()
    if not code_str:
        raise HTTPException(status_code=400, detail="Code is required")

    # Look up the code
    promo = (
        db.query(PromoCode)
        .filter(PromoCode.code == code_str, PromoCode.is_active == True)
        .first()
    )
    if not promo:
        raise HTTPException(status_code=404, detail="That code didn\u2019t work. Try another?")

    # Check usage limits (0 = unlimited)
    if promo.max_uses > 0 and promo.uses_count >= promo.max_uses:
        raise HTTPException(status_code=410, detail="This code has been fully redeemed")

    # Check if user already has an active subscription
    existing = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active")
        .first()
    )
    if existing:
        return RedeemResponse(status="ok", message="You already have an active subscription")

    # Check if user already used this specific code
    already_used = (
        db.query(Subscription)
        .filter(
            Subscription.user_id == user.id,
            Subscription.code_used == code_str,
        )
        .first()
    )
    if already_used:
        raise HTTPException(status_code=409, detail="You have already used this code")

    # Create subscription
    sub = Subscription(
        user_id=user.id,
        plan="code",
        status="active",
        code_used=code_str,
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    db.add(sub)

    # Increment usage count
    promo.uses_count += 1
    db.commit()

    return RedeemResponse(status="ok", message="Code accepted!")


@r.post("/purchase", response_model=PurchaseResponse)
def purchase(
    req: PurchaseRequest,
    request: Request,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """
    Create a Stripe Checkout session for the chosen plan.
    Returns a checkout_url that the frontend redirects to.
    If Stripe is not configured, returns 503.
    """
    if req.plan not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail="Plan must be 'monthly' or 'yearly'")

    # Check if user already has an active subscription
    existing = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active")
        .first()
    )
    if existing:
        exp = _as_aware_utc(existing.expires_at)
        if exp and exp < datetime.now(timezone.utc):
            existing.status = "expired"
            db.commit()
        else:
            return PurchaseResponse(status="ok", message="You already have an active subscription")

    # Stripe must be configured
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payments are not configured yet")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    # Pick price ID
    price_id = (
        cfg.STRIPE_PRICE_ID_MONTHLY if req.plan == "monthly"
        else cfg.STRIPE_PRICE_ID_YEARLY
    )
    if not price_id:
        raise HTTPException(status_code=503, detail="Payment plan not configured")

    # Create or reuse Stripe customer
    try:
        if user.stripe_customer_id:
            customer_id = user.stripe_customer_id
        else:
            customer = stripe.Customer.create(
                email=user.email,
                name=f"{user.first_name or ''} {user.last_name or ''}".strip() or None,
                metadata={"rewire_user_id": str(user.id)},
            )
            customer_id = customer.id
            user.stripe_customer_id = customer_id
            db.commit()
    except Exception as e:
        print(f"[stripe] customer creation error: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to payment provider")

    # Build redirect URLs from the origin the user is actually on,
    # so they return to the same origin that holds their login token.
    base_url = _frontend_base_url(request)
    if not base_url:
        print("[stripe] WARNING: no origin/referer and PUBLIC_BASE_URL is empty")
    success_url = f"{base_url}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base_url}/?payment=cancel"

    # Create Checkout Session
    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "rewire_user_id": str(user.id),
                "plan": req.plan,
            },
        )
    except Exception as e:
        print(f"[stripe] checkout session error: {e}")
        raise HTTPException(status_code=502, detail="Could not create checkout session")

    return PurchaseResponse(
        status="checkout",
        checkout_url=session.url,
    )


@r.post("/verify", response_model=VerifyResponse)
def verify_session(
    req: VerifyRequest,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """
    Verify a Stripe Checkout session after the success redirect and
    activate the subscription immediately, without waiting for the
    webhook. Idempotent with the webhook via stripe_session_id.
    """
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payments are not configured yet")

    session_id = (req.session_id or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        print(f"[stripe] verify: could not retrieve session {session_id}: {e}")
        raise HTTPException(status_code=404, detail="Checkout session not found")

    # The session must belong to this user
    meta = getattr(session, "metadata", None)
    meta_uid = getattr(meta, "rewire_user_id", None) if meta else None
    if not meta_uid or int(meta_uid) != user.id:
        print(f"[stripe] verify: session {session_id} does not belong to user {user.id}")
        raise HTTPException(status_code=403, detail="This payment does not belong to your account")

    # The session must actually be paid
    payment_status = getattr(session, "payment_status", "") or ""
    if payment_status != "paid":
        print(f"[stripe] verify: session {session_id} not paid (status={payment_status})")
        return VerifyResponse(status="pending", has_subscription=False)

    plan = getattr(meta, "plan", "monthly") if meta else "monthly"
    _activate_subscription(db, user.id, plan, session_id)
    return VerifyResponse(status="ok", has_subscription=True)


@r.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Stripe webhook endpoint. Called by Stripe after successful payment.
    Verifies the webhook signature, then creates a Subscription record.
    """
    if not cfg.STRIPE_SECRET_KEY or not cfg.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, cfg.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        print(f"[stripe] webhook parse error: {e}")
        raise HTTPException(status_code=400, detail="Webhook error")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        # stripe>=8: use attribute access only, no .get() or dict()
        session_id = session.id or ""
        meta = getattr(session, "metadata", None)
        user_id_str = getattr(meta, "rewire_user_id", None) if meta else None
        plan = getattr(meta, "plan", "monthly") if meta else "monthly"

        if not user_id_str:
            print("[stripe] webhook: no rewire_user_id in metadata")
            return {"status": "ignored"}

        user_id = int(user_id_str)

        # Use a fresh DB session for the webhook
        db = SessionLocal()
        try:
            result = _activate_subscription(db, user_id, plan, session_id)
            if result == "exists":
                print(f"[stripe] webhook: subscription already exists for session {session_id}")
        except Exception as e:
            print(f"[stripe] webhook db error: {e}")
            db.rollback()
        finally:
            db.close()

    return {"status": "ok"}


@r.get("/config")
def get_stripe_config():
    """Return the Stripe publishable key for the frontend."""
    return {"publishable_key": cfg.STRIPE_PUBLISHABLE_KEY or ""}


@r.post("/cancel", response_model=StatusResponse)
def cancel(
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Cancel the user's active subscription."""
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active")
        .first()
    )
    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found")

    sub.status = "cancelled"
    db.commit()
    return StatusResponse(status="ok")
