"""
Subscription / purchase routes (v5).

Two ways to unlock protocol days 2-5:
  - Per-protocol: a one-time payment ($9) that unlocks a single protocol
    forever. Grants an Entitlement(kind="protocol", protocol_id) and flips
    that protocol's `unlocked` flag.
  - Monthly: a recurring Stripe subscription ($12/mo) that unlocks every
    protocol while active. Grants an Entitlement(kind="monthly").

State lives in the `entitlements` table (the v4 `subscriptions` / promo-code
path is retired). When STRIPE_SECRET_KEY is unset, /checkout returns 503,
except in DEV_MODE where purchases are granted immediately for local testing.
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db, SessionLocal
from app.models import User, Entitlement, Protocol
from app.routes.auth import get_current_user_required

r = APIRouter(prefix="/api/subscription", tags=["subscription"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_aware_utc(dt):
    """Normalize a (possibly naive Postgres) datetime to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _frontend_base_url(request: Request) -> str:
    """Base URL to return the user to after Checkout: prefer Origin, then the
    Referer's origin, then PUBLIC_BASE_URL, so they land back on the same origin
    that holds their login token."""
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin.startswith("http://") or origin.startswith("https://"):
        return origin

    referer = (request.headers.get("referer") or "").strip()
    if referer.startswith("http://") or referer.startswith("https://"):
        try:
            from urllib.parse import urlsplit
            parts = urlsplit(referer)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except Exception:
            pass

    return cfg.PUBLIC_BASE_URL.rstrip("/") if cfg.PUBLIC_BASE_URL else ""


def _monthly_entitlement(user_id, db) -> Optional[Entitlement]:
    """The user's active, non-expired monthly membership, or None."""
    e = (db.query(Entitlement)
         .filter(Entitlement.user_id == user_id,
                 Entitlement.kind == "monthly",
                 Entitlement.status == "active")
         .order_by(Entitlement.created_at.desc())
         .first())
    if not e:
        return None
    exp = _as_aware_utc(e.expires_at)
    if exp and exp < datetime.now(timezone.utc):
        e.status = "expired"
        db.commit()
        return None
    return e


def _unlocked_protocol_ids(user_id, db) -> List[int]:
    rows = (db.query(Entitlement)
            .filter(Entitlement.user_id == user_id,
                    Entitlement.kind == "protocol",
                    Entitlement.status == "active")
            .all())
    return [e.protocol_id for e in rows if e.protocol_id]


def _grant_protocol(db, user_id, protocol_id, session_id) -> str:
    """Idempotent on session_id. Unlocks one protocol forever."""
    existing = db.query(Entitlement).filter(Entitlement.stripe_session_id == session_id).first()
    if existing:
        return "exists"
    db.add(Entitlement(
        user_id=user_id, kind="protocol", protocol_id=protocol_id,
        status="active", stripe_session_id=session_id,
    ))
    p = db.query(Protocol).filter(Protocol.id == protocol_id, Protocol.user_id == user_id).first()
    if p:
        p.unlocked = True
    db.commit()
    print(f"[stripe] protocol {protocol_id} unlocked for user {user_id} (session {session_id})")
    return "created"


def _grant_monthly(db, user_id, session_id, subscription_id, expires_at) -> str:
    """Idempotent on session_id. Replaces any prior active monthly entitlement."""
    existing = db.query(Entitlement).filter(Entitlement.stripe_session_id == session_id).first()
    if existing:
        return "exists"
    olds = (db.query(Entitlement)
            .filter(Entitlement.user_id == user_id,
                    Entitlement.kind == "monthly",
                    Entitlement.status == "active")
            .all())
    for o in olds:
        o.status = "replaced"
    db.add(Entitlement(
        user_id=user_id, kind="monthly", status="active",
        stripe_session_id=session_id, stripe_subscription_id=subscription_id,
        expires_at=expires_at,
    ))
    db.commit()
    print(f"[stripe] monthly membership active for user {user_id} (session {session_id})")
    return "created"


def _sub_period_end(subscription_id: str) -> datetime:
    """Read a Stripe subscription's current period end; fall back to +31 days."""
    try:
        import stripe
        stripe.api_key = cfg.STRIPE_SECRET_KEY
        sub = stripe.Subscription.retrieve(subscription_id)
        cpe = getattr(sub, "current_period_end", None)
        if cpe:
            return datetime.fromtimestamp(int(cpe), tz=timezone.utc)
    except Exception as e:
        print(f"[stripe] could not read period end for {subscription_id}: {e}")
    return datetime.now(timezone.utc) + timedelta(days=31)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class CheckoutReq(BaseModel):
    kind: str                         # "protocol" | "monthly"
    protocol_id: Optional[int] = None


class VerifyReq(BaseModel):
    session_id: str


class CheckoutResp(BaseModel):
    status: str                       # checkout | already_unlocked | already_active | dev_granted
    message: str = ""
    checkout_url: Optional[str] = None


class VerifyResp(BaseModel):
    status: str                       # ok | pending
    unlocked: bool = False


class StatusResp(BaseModel):
    monthly_active: bool
    monthly_expires: Optional[str] = None
    monthly_canceling: bool = False
    unlocked_protocol_ids: List[int] = []


class Ok(BaseModel):
    status: str
    message: str = ""


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@r.get("/config")
def get_config():
    """Publishable key + display prices for the paywall."""
    return {
        "publishable_key": cfg.STRIPE_PUBLISHABLE_KEY or "",
        "protocol_price_usd": cfg.PROTOCOL_PRICE_USD,
        "monthly_price_usd": cfg.MONTHLY_PRICE_USD,
    }


@r.get("/status", response_model=StatusResp)
def get_status(user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    m = _monthly_entitlement(user.id, db)
    return StatusResp(
        monthly_active=bool(m),
        monthly_expires=(_as_aware_utc(m.expires_at).isoformat() if m and m.expires_at else None),
        monthly_canceling=bool(m and m.canceling),
        unlocked_protocol_ids=_unlocked_protocol_ids(user.id, db),
    )


@r.post("/checkout", response_model=CheckoutResp)
def checkout(req: CheckoutReq, request: Request,
             user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    kind = (req.kind or "").strip().lower()
    if kind not in ("protocol", "monthly"):
        raise HTTPException(400, "kind must be 'protocol' or 'monthly'")

    # Pre-checks + ownership.
    if kind == "protocol":
        if not req.protocol_id:
            raise HTTPException(400, "protocol_id required")
        p = db.query(Protocol).filter(Protocol.id == req.protocol_id, Protocol.user_id == user.id).first()
        if not p:
            raise HTTPException(404, "protocol not found")
        if p.unlocked or _monthly_entitlement(user.id, db):
            return CheckoutResp(status="already_unlocked", message="This protocol is already unlocked")
    else:
        if _monthly_entitlement(user.id, db):
            return CheckoutResp(status="already_active", message="You already have an active membership")

    # DEV_MODE: grant immediately so the unlocked flow can be tested without Stripe.
    if cfg.DEV_MODE:
        sid = f"dev_{kind}_{user.id}_{int(time.time())}"
        if kind == "protocol":
            _grant_protocol(db, user.id, req.protocol_id, sid)
        else:
            _grant_monthly(db, user.id, sid, f"dev_sub_{user.id}",
                           datetime.now(timezone.utc) + timedelta(days=30))
        base = _frontend_base_url(request)
        return CheckoutResp(status="dev_granted",
                            checkout_url=f"{base}/?payment=success&session_id={sid}")

    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(503, "Payments are not configured yet")

    price_id = cfg.STRIPE_PRICE_ID_PROTOCOL if kind == "protocol" else cfg.STRIPE_PRICE_ID_MONTHLY
    if not price_id:
        raise HTTPException(503, "Payment price not configured")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    # Create or reuse the Stripe customer.
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
        raise HTTPException(502, "Could not connect to payment provider")

    base_url = _frontend_base_url(request)
    success_url = f"{base_url}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base_url}/?payment=cancel"

    meta = {"rewire_user_id": str(user.id), "kind": kind}
    if kind == "protocol":
        meta["protocol_id"] = str(req.protocol_id)

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode=("payment" if kind == "protocol" else "subscription"),
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
        )
    except Exception as e:
        print(f"[stripe] checkout session error: {e}")
        raise HTTPException(502, "Could not create checkout session")

    return CheckoutResp(status="checkout", checkout_url=session.url)


@r.post("/verify", response_model=VerifyResp)
def verify_session(req: VerifyReq, user: User = Depends(get_current_user_required),
                   db: Session = Depends(get_db)):
    """Verify a Checkout session after the success redirect and grant the
    entitlement immediately, without waiting for the webhook. Idempotent."""
    session_id = (req.session_id or "").strip()
    if not session_id:
        raise HTTPException(400, "session_id is required")

    # DEV_MODE grants happen at /checkout; the dev session is already applied.
    if cfg.DEV_MODE or session_id.startswith("dev_"):
        return VerifyResp(status="ok", unlocked=True)

    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(503, "Payments are not configured yet")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        print(f"[stripe] verify: could not retrieve session {session_id}: {e}")
        raise HTTPException(404, "Checkout session not found")

    meta = getattr(session, "metadata", None)
    meta_uid = getattr(meta, "rewire_user_id", None) if meta else None
    if not meta_uid or int(meta_uid) != user.id:
        raise HTTPException(403, "This payment does not belong to your account")

    kind = getattr(meta, "kind", "monthly") if meta else "monthly"

    if kind == "protocol":
        if (getattr(session, "payment_status", "") or "") != "paid":
            return VerifyResp(status="pending", unlocked=False)
        pid = getattr(meta, "protocol_id", None)
        _grant_protocol(db, user.id, int(pid) if pid else None, session_id)
        return VerifyResp(status="ok", unlocked=True)

    # monthly
    sub_id = getattr(session, "subscription", None)
    if not sub_id:
        return VerifyResp(status="pending", unlocked=False)
    _grant_monthly(db, user.id, session_id, str(sub_id), _sub_period_end(str(sub_id)))
    return VerifyResp(status="ok", unlocked=True)


@r.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook: grant on checkout completion, keep monthly expiry in sync
    on renewal, and expire it on cancellation."""
    if not cfg.STRIPE_SECRET_KEY or not cfg.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Stripe not configured")

    import stripe
    stripe.api_key = cfg.STRIPE_SECRET_KEY

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, cfg.STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")
    except Exception as e:
        print(f"[stripe] webhook parse error: {e}")
        raise HTTPException(400, "Webhook error")

    etype = event["type"]

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.id or ""
        meta = getattr(session, "metadata", None)
        uid = getattr(meta, "rewire_user_id", None) if meta else None
        kind = getattr(meta, "kind", "monthly") if meta else "monthly"
        if not uid:
            print("[stripe] webhook: no rewire_user_id in metadata")
            return {"status": "ignored"}
        user_id = int(uid)

        db = SessionLocal()
        try:
            if kind == "protocol":
                pid = getattr(meta, "protocol_id", None)
                _grant_protocol(db, user_id, int(pid) if pid else None, session_id)
            else:
                sub_id = getattr(session, "subscription", None)
                if sub_id:
                    _grant_monthly(db, user_id, session_id, str(sub_id), _sub_period_end(str(sub_id)))
        except Exception as e:
            print(f"[stripe] webhook grant error: {e}")
            db.rollback()
        finally:
            db.close()

    elif etype == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.id or ""
        cpe = getattr(sub, "current_period_end", None)
        db = SessionLocal()
        try:
            e = (db.query(Entitlement)
                 .filter(Entitlement.stripe_subscription_id == sub_id,
                         Entitlement.kind == "monthly")
                 .order_by(Entitlement.created_at.desc()).first())
            if e and cpe:
                e.expires_at = datetime.fromtimestamp(int(cpe), tz=timezone.utc)
                if e.status != "active":
                    e.status = "active"
                db.commit()
        except Exception as ex:
            print(f"[stripe] webhook sub-update error: {ex}")
            db.rollback()
        finally:
            db.close()

    elif etype == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.id or ""
        db = SessionLocal()
        try:
            es = (db.query(Entitlement)
                  .filter(Entitlement.stripe_subscription_id == sub_id,
                          Entitlement.kind == "monthly").all())
            for e in es:
                e.status = "expired"
            db.commit()
        except Exception as ex:
            print(f"[stripe] webhook sub-delete error: {ex}")
            db.rollback()
        finally:
            db.close()

    return {"status": "ok"}


@r.post("/cancel", response_model=Ok)
def cancel(user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    """Cancel the monthly membership. Access continues until the current period
    ends (Stripe cancel_at_period_end); the deleted webhook expires it then."""
    m = _monthly_entitlement(user.id, db)
    if not m:
        raise HTTPException(404, "No active membership found")

    # DEV or no real Stripe subscription -> expire now so the state can be re-tested.
    if cfg.DEV_MODE or not m.stripe_subscription_id or not cfg.STRIPE_SECRET_KEY:
        m.status = "expired"
        m.expires_at = datetime.now(timezone.utc)
        db.commit()
        return Ok(status="ok", message="Membership canceled")

    try:
        import stripe
        stripe.api_key = cfg.STRIPE_SECRET_KEY
        stripe.Subscription.modify(m.stripe_subscription_id, cancel_at_period_end=True)
    except Exception as e:
        print(f"[stripe] cancel error: {e}")
        raise HTTPException(502, "Could not cancel with payment provider")

    m.canceling = True
    db.commit()
    return Ok(status="ok", message="Your membership will not renew and stays active until the period ends")


@r.post("/resume", response_model=Ok)
def resume(user: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    """Undo a scheduled cancellation so the membership keeps renewing."""
    m = _monthly_entitlement(user.id, db)
    if not m:
        raise HTTPException(404, "No active membership found")
    if not m.canceling:
        return Ok(status="ok", message="Your membership is already active")

    # DEV or no real Stripe subscription -> just clear the flag.
    if cfg.DEV_MODE or not m.stripe_subscription_id or not cfg.STRIPE_SECRET_KEY:
        m.canceling = False
        db.commit()
        return Ok(status="ok", message="Membership resumed")

    try:
        import stripe
        stripe.api_key = cfg.STRIPE_SECRET_KEY
        stripe.Subscription.modify(m.stripe_subscription_id, cancel_at_period_end=False)
    except Exception as e:
        print(f"[stripe] resume error: {e}")
        raise HTTPException(502, "Could not resume with payment provider")

    m.canceling = False
    db.commit()
    return Ok(status="ok", message="Your membership will keep renewing")