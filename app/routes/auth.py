from datetime import datetime, timedelta, timezone, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import cfg
from app.db import get_db
from app.models import (
    User, Subscription, Goal, Challenge, Tip, Jolt, Reflection,
    PushSubscription, AuditLog,
)

r = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
def hash_pw(pw): return pwd_ctx.hash(pw)
def check_pw(plain, hashed): return pwd_ctx.verify(plain, hashed)

def make_token(uid, email):
    exp = datetime.now(timezone.utc) + timedelta(hours=cfg.JWT_EXPIRE_HRS)
    return jwt.encode({"sub": str(uid), "email": email, "exp": exp},
                      cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)

def decode_token(token):
    try:
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.JWT_ALGORITHM])
    except JWTError:
        return None

_bearer = HTTPBearer(auto_error=False)


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


def get_current_user_optional(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not creds: return None
    p = decode_token(creds.credentials)
    if not p: return None
    uid = p.get("sub")
    if not uid: return None
    return db.query(User).filter(User.id == int(uid)).first()


def get_current_user_required(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(401, "not authenticated")
    p = decode_token(creds.credentials)
    if not p:
        raise HTTPException(401, "invalid or expired token")
    uid = p.get("sub")
    if not uid:
        raise HTTPException(401, "invalid token")
    u = db.query(User).filter(User.id == int(uid)).first()
    if not u:
        raise HTTPException(401, "user not found")
    return u


def _has_active_sub(uid, db):
    s = (db.query(Subscription)
         .filter(Subscription.user_id == uid, Subscription.status == "active")
         .first())
    if not s: return False
    exp = _as_aware_utc(s.expires_at)
    if exp and exp < datetime.now(timezone.utc):
        s.status = "expired"
        db.commit()
        return False
    return True


class RegisterReq(BaseModel):
    email: str
    password: str
    first_name: str = ""
    last_name: str = ""
    dob: str = ""
    gender: str = ""

class LoginReq(BaseModel):
    email: str
    password: str

class GoogleReq(BaseModel):
    credential: str

class ProfileReq(BaseModel):
    first_name: str = ""
    last_name: str = ""
    dob: str = ""
    gender: str = ""

class DisclaimerReq(BaseModel):
    version: str = "1.0"

class AuthResp(BaseModel):
    token: str
    user_id: int
    email: str
    first_name: str = ""
    last_name: str = ""
    onboarding_complete: bool = False
    disclaimer_accepted: bool = False
    has_subscription: bool = False

class UserResp(BaseModel):
    user_id: int
    email: str
    first_name: str = ""
    last_name: str = ""
    auth_provider: str = "local"
    onboarding_complete: bool = False
    disclaimer_accepted: bool = False
    has_subscription: bool = False

class StatusResp(BaseModel):
    status: str


def _auth_resp(u, token, db):
    return AuthResp(
        token=token, user_id=u.id, email=u.email,
        first_name=u.first_name or "", last_name=u.last_name or "",
        onboarding_complete=u.onboarding_complete,
        disclaimer_accepted=u.disclaimer_accepted_at is not None,
        has_subscription=_has_active_sub(u.id, db),
    )


@r.post("/register", response_model=AuthResp)
def register(req: RegisterReq, db: Session = Depends(get_db)):
    if not req.email or not req.email.strip():
        raise HTTPException(400, "email required")
    if not req.password or len(req.password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")

    email = req.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "email already registered")

    dob_val = None
    if req.dob:
        try:
            dob_val = date.fromisoformat(req.dob)
        except ValueError:
            raise HTTPException(400, "invalid date, use YYYY-MM-DD")

    u = User(
        email=email,
        password_hash=hash_pw(req.password),
        first_name=req.first_name.strip() or None,
        last_name=req.last_name.strip() or None,
        dob=dob_val,
        gender=req.gender.strip() or None,
        auth_provider="local",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _auth_resp(u, make_token(u.id, u.email), db)


@r.post("/login", response_model=AuthResp)
def login(req: LoginReq, db: Session = Depends(get_db)):
    email = (req.email or "").strip().lower()
    u = db.query(User).filter(User.email == email).first()
    if not u or not u.password_hash:
        raise HTTPException(401, "invalid email or password")
    if not check_pw(req.password, u.password_hash):
        raise HTTPException(401, "invalid email or password")
    return _auth_resp(u, make_token(u.id, u.email), db)


@r.post("/google", response_model=AuthResp)
def google_login(req: GoogleReq, db: Session = Depends(get_db)):
    """
    Verify a Google Identity Services ID token (credential) and sign the
    user in, creating the account on first login (sign-up included).

    Flow:
      1. Verify the credential with Google's tokeninfo endpoint.
      2. Check the token audience matches our GOOGLE_CLIENT_ID.
      3. Require a verified email.
      4. Find user by google_id, else by email (links existing local
         accounts to Google), else create a new user.
    """
    import requests as http_req

    if not req.credential or not req.credential.strip():
        raise HTTPException(400, "missing Google credential")

    if not cfg.GOOGLE_CLIENT_ID:
        print("[google] GOOGLE_CLIENT_ID is not set on the server")
        raise HTTPException(503, "Google login is not configured on the server")

    try:
        resp = http_req.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": req.credential.strip()},
            timeout=10,
        )
    except http_req.exceptions.Timeout:
        print("[google] tokeninfo request timed out")
        raise HTTPException(502, "could not reach Google (timeout)")
    except Exception as e:
        print(f"[google] tokeninfo request failed: {e}")
        raise HTTPException(502, "could not reach Google")

    if resp.status_code != 200:
        print(f"[google] tokeninfo rejected credential: HTTP {resp.status_code} {resp.text[:200]}")
        raise HTTPException(401, "invalid Google token")

    try:
        info = resp.json()
    except Exception as e:
        print(f"[google] tokeninfo returned non-JSON: {e}")
        raise HTTPException(502, "unexpected response from Google")

    # Audience must match our OAuth client ID
    if info.get("aud") != cfg.GOOGLE_CLIENT_ID:
        print(f"[google] audience mismatch: token aud={info.get('aud')} "
              f"expected={cfg.GOOGLE_CLIENT_ID}")
        raise HTTPException(401, "Google token audience mismatch")

    # Require a verified email (tokeninfo returns this as the string "true")
    email_verified = str(info.get("email_verified", "")).lower() == "true"
    if not email_verified:
        print(f"[google] unverified email rejected: {info.get('email')}")
        raise HTTPException(401, "Google account email is not verified")

    g_email = (info.get("email") or "").strip().lower()
    g_id = info.get("sub") or ""
    g_name = info.get("name") or ""
    g_given = (info.get("given_name") or "").strip()
    g_family = (info.get("family_name") or "").strip()

    if not g_email or not g_id:
        print("[google] token missing email or sub claim")
        raise HTTPException(401, "could not extract email from Google token")

    # 1) Match by google_id, 2) link by email, 3) create new user
    u = db.query(User).filter(User.google_id == g_id).first()
    if not u:
        u = db.query(User).filter(User.email == g_email).first()

    if u:
        changed = False
        if not u.google_id:
            u.google_id = g_id
            u.auth_provider = "google"
            changed = True
            print(f"[google] linked existing account {u.email} to Google")
        # Fill in missing names from Google profile without overwriting
        if not u.first_name and (g_given or g_name):
            u.first_name = g_given or g_name.strip().rsplit(" ", 1)[0]
            changed = True
        if not u.last_name and g_family:
            u.last_name = g_family
            changed = True
        if changed:
            db.commit()
    else:
        # Prefer Google's structured name fields; fall back to splitting name
        if g_given or g_family:
            first_name = g_given or None
            last_name = g_family or None
        else:
            parts = g_name.strip().rsplit(" ", 1) if g_name.strip() else [""]
            first_name = parts[0] or None
            last_name = parts[1] if len(parts) > 1 else None

        u = User(
            email=g_email,
            first_name=first_name,
            last_name=last_name,
            auth_provider="google",
            google_id=g_id,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        print(f"[google] created new user via Google sign-up: {g_email}")

    return _auth_resp(u, make_token(u.id, u.email), db)


@r.put("/profile", response_model=StatusResp)
def update_profile(req: ProfileReq, u: User = Depends(get_current_user_required),
                   db: Session = Depends(get_db)):
    if req.first_name: u.first_name = req.first_name.strip()
    if req.last_name: u.last_name = req.last_name.strip()
    if req.gender: u.gender = req.gender.strip()
    if req.dob:
        try:
            u.dob = date.fromisoformat(req.dob)
        except ValueError:
            raise HTTPException(400, "invalid date, use YYYY-MM-DD")
    db.commit()
    return StatusResp(status="ok")


@r.post("/accept-disclaimer", response_model=StatusResp)
def accept_disclaimer(req: DisclaimerReq, u: User = Depends(get_current_user_required),
                      db: Session = Depends(get_db)):
    u.disclaimer_accepted_at = datetime.now(timezone.utc)
    u.disclaimer_version = req.version
    db.commit()
    return StatusResp(status="ok")


@r.post("/complete-onboarding", response_model=StatusResp)
def complete_onboarding(u: User = Depends(get_current_user_required),
                        db: Session = Depends(get_db)):
    u.onboarding_complete = True
    db.commit()
    return StatusResp(status="ok")


@r.get("/me", response_model=UserResp)
def get_me(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    return UserResp(
        user_id=u.id, email=u.email,
        first_name=u.first_name or "", last_name=u.last_name or "",
        auth_provider=u.auth_provider,
        onboarding_complete=u.onboarding_complete,
        disclaimer_accepted=u.disclaimer_accepted_at is not None,
        has_subscription=_has_active_sub(u.id, db),
    )


@r.get("/config")
def get_auth_config():
    return {"google_client_id": cfg.GOOGLE_CLIENT_ID or ""}


@r.delete("/account", response_model=StatusResp)
def delete_account(u: User = Depends(get_current_user_required),
                   db: Session = Depends(get_db)):
    """Permanently delete the user's account and all associated data."""
    uid = u.id

    # Gather all goal IDs for this user
    goal_ids = [g.id for g in db.query(Goal.id).filter(Goal.user_id == uid).all()]

    # Delete in dependency order
    if goal_ids:
        db.query(Reflection).filter(Reflection.goal_id.in_(goal_ids)).delete(synchronize_session=False)
        db.query(Jolt).filter(Jolt.goal_id.in_(goal_ids)).delete(synchronize_session=False)
        db.query(Tip).filter(Tip.goal_id.in_(goal_ids)).delete(synchronize_session=False)
        db.query(Challenge).filter(Challenge.goal_id.in_(goal_ids)).delete(synchronize_session=False)
        db.query(Goal).filter(Goal.user_id == uid).delete(synchronize_session=False)

    # Also catch any reflections/jolts linked directly to user but not via goals
    db.query(Reflection).filter(Reflection.user_id == uid).delete(synchronize_session=False)
    db.query(Jolt).filter(Jolt.user_id == uid).delete(synchronize_session=False)

    # Delete subscription, push, and audit data
    db.query(Subscription).filter(Subscription.user_id == uid).delete(synchronize_session=False)
    db.query(PushSubscription).filter(PushSubscription.user_id == uid).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.user_id == uid).delete(synchronize_session=False)

    # Delete the user
    db.delete(u)
    db.commit()

    return StatusResp(status="ok")
