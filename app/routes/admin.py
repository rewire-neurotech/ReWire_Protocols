"""
Admin console read API (v5).

Every endpoint here is READ-ONLY over the app's own data, with one exception:
POST/DELETE on a safety review, which records a human decision about a flag.
Nothing in this module can change a user's protocol, jolt, entitlement or
account -- the console is a window, not a control panel.

ACCESS
  Every route depends on get_current_admin_required, which is the normal user
  dependency plus an is_admin check. The console signs in through the same
  /api/auth/login the app uses, with the same JWT. No second credential system.

PRIVACY
  Two deliberate decisions are encoded here:
    1. Emails are MASKED server-side (j***23@gmail.com). The browser never
       receives a whole address. Controlled by cfg.ADMIN_MASK_EMAILS.
    2. Protocol.charge and SafetyEvent.said -- the rawest things a person
       writes -- are decrypted for display, and EVERY read is written to
       audit_logs. That is the accountability half of showing them at all.
       Controlled by cfg.ADMIN_SHOW_CHARGE / cfg.ADMIN_AUDIT_READS.
  This is why person detail is a separate endpoint rather than being folded
  into the list: an audit trail that says "an admin opened the People tab" is
  worthless, one that says "this admin read this person's charges at this time"
  is not.

SCALE
  Retention and the daily series are computed in Python from two narrow
  queries rather than in dialect-specific SQL, because the date arithmetic
  differs between SQLite (dev) and Postgres (prod) and correctness matters more
  than speed at the current row counts. Every list endpoint is capped by
  cfg.ADMIN_MAX_ROWS. If the user table reaches six figures, the retention
  computation is the first thing to push down into SQL.
"""

from datetime import datetime, timezone, timedelta, date
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db
from app.models import (
    User, Protocol, ProtocolDay, ProtocolJolt, JournalEntry,
    Entitlement, Payment, UserActivity, SafetyEvent, SafetyReview, AuditLog,
)
from app.routes.auth import get_current_admin_required, check_pw, make_token
from app.utils.encryption import decrypt_field

r = APIRouter(prefix="/api/admin", tags=["admin"])


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


def _ts(dt) -> Optional[str]:
    """ISO-8601 with an explicit +00:00, so the browser parses the right
    instant. SQLite hands datetimes back naive; without the zone every
    timestamp would shift by the client's offset."""
    d = _as_aware_utc(dt)
    return d.isoformat() if d else None


def _clamp_days(days) -> int:
    """Only the windows the console's 7d/30d/90d control can ask for.

    A hand-edited query string must not be able to request an unbounded scan.
    """
    try:
        d = int(days)
    except (TypeError, ValueError):
        return cfg.ADMIN_DEFAULT_RANGE_DAYS
    return d if d in cfg.ADMIN_RANGE_DAYS_ALLOWED else cfg.ADMIN_DEFAULT_RANGE_DAYS


def _window(days: int):
    """Calendar-aligned window: `days` whole UTC days, ending with today.

    Aligned to midnight rather than to "now minus N*24h" so that the KPI total
    and the chart's final cumulative point are computed over exactly the same
    set of days. A rolling timestamp window silently excludes part of today from
    the per-day buckets while still including it in the headline sum, and the
    two numbers drift apart -- which is precisely the kind of quiet
    inconsistency that makes a dashboard untrustworthy.

    Returns (start, end, prev_start, prev_end); prev_* is the equal-length
    window immediately before, used for the trend chips.
    """
    now = datetime.now(timezone.utc)
    start_date = now.date() - timedelta(days=days - 1)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=days)
    prev_start = start - timedelta(days=days)
    return start, end, prev_start, start


def mask_email(email: str) -> str:
    """j***23@gmail.com -- enough to recognise a returning account in the list,
    not enough to be a mailing list."""
    e = (email or "").strip()
    if not cfg.ADMIN_MASK_EMAILS or not e or "@" not in e:
        return e
    local, _, domain = e.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}***{local[-2:]}@{domain}"


def _audit(db: Session, admin: User, request: Request, action: str, target: str) -> None:
    """Record an admin read of personal data. Best-effort, never raises."""
    if not cfg.ADMIN_AUDIT_READS:
        return
    try:
        ip = None
        if request is not None and request.client:
            ip = request.client.host
        fwd = request.headers.get("x-forwarded-for") if request is not None else None
        if fwd:
            ip = fwd.split(",")[0].strip()
        db.add(AuditLog(
            user_id=admin.id,
            user_email=admin.email,
            action=action[:100],
            target=(target or "")[:300],
            ip_address=(ip or "")[:50] or None,
        ))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[admin] audit write failed ({action}): {e}")


def _plan_for(uid: int, monthly_ids: set, protocol_ids: set) -> str:
    if uid in monthly_ids:
        return "monthly"
    if uid in protocol_ids:
        return "protocol"
    return "free"


def _paying_user_ids(db: Session):
    """(monthly_user_ids, protocol_user_ids) for currently-active entitlements.

    Entitlements rather than payments on purpose: this answers "what can this
    person access right now", which is what the Plan column means. Revenue is a
    separate question and comes from the payments ledger.
    """
    now = datetime.now(timezone.utc)
    monthly, protocol = set(), set()
    rows = (db.query(Entitlement.user_id, Entitlement.kind, Entitlement.expires_at)
            .filter(Entitlement.status == "active").all())
    for uid, kind, exp in rows:
        if kind == "monthly":
            e = _as_aware_utc(exp)
            if e is None or e >= now:
                monthly.add(uid)
        elif kind == "protocol":
            protocol.add(uid)
    return monthly, protocol


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class SeriesPoint(BaseModel):
    date: str
    signups: int
    revenue: int          # dollars in this day
    cum_revenue: int
    cum_users: int


class CurvePoint(BaseModel):
    day: int
    rate: float
    cohort: int           # how many accounts were old enough to be counted


class Funnel(BaseModel):
    signedUp: int
    startedOne: int
    paid: int


class Retention(BaseModel):
    d7: float
    d30: float


class OverviewResp(BaseModel):
    range_days: int
    revenue: int
    prevRevenue: int
    totalUsers: int
    newUsers: int
    prevUsers: int
    retention: Retention
    curve: List[CurvePoint]
    funnel: Funnel
    series: List[SeriesPoint]


class PersonRow(BaseModel):
    id: str
    email_masked: str
    created_at: str
    plan: str
    last_active: Optional[str] = None
    protocol_count: int
    latest_goal: str = ""


class PeopleResp(BaseModel):
    people: List[PersonRow]
    total: int
    truncated: bool = False


class ProtocolOut(BaseModel):
    goal: str
    charge: str = ""
    day: int
    status: str
    created_at: str


class PersonDetail(BaseModel):
    id: str
    email_masked: str
    created_at: str
    plan: str
    last_active: Optional[str] = None
    protocols: List[ProtocolOut]
    journal_count: int = 0


class SafetyCase(BaseModel):
    id: str
    ts: str
    sev: str
    category: str
    said: str
    layer: str
    source: str
    auto: str
    user: str
    decision: Optional[str] = None      # confirmed | false | None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None


class SafetyResp(BaseModel):
    cases: List[SafetyCase]
    reviewed: int
    confirmed: int
    open_count: int


class ReviewReq(BaseModel):
    decision: str                        # confirmed | false
    note: str = ""


class Ok(BaseModel):
    status: str


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

class LoginReq(BaseModel):
    identifier: str = ""      # email, or the dev username
    password: str = ""


class LoginResp(BaseModel):
    token: str
    email: str
    mode: str                 # "account" | "dev"


@r.post("/login", response_model=LoginResp)
def admin_login(req: LoginReq, db: Session = Depends(get_db)):
    """Sign in to the console.

    Two ways in, both ending at the same place: a normal JWT for an account
    with is_admin set. Every /api/admin/* route checks that flag independently,
    so neither path is a bypass of the actual authorisation.

    1) ACCOUNT  -- the email and password the person already uses for the app.
       The account must be listed in ADMIN_EMAILS (or have is_admin set
       directly). This is the path that should exist in production.

    2) DEV      -- a fixed username/password, no account needed. Exists ONLY
       when ADMIN_DEV_LOGIN is explicitly true; otherwise this branch is never
       reached and the credential is meaningless. On first use it creates a
       marked, password-less account (ADMIN_DEV_EMAIL) so that reviews and
       audit rows still have a real user to attribute to, rather than the
       console having a second identity system of its own.

       Retiring it is removing one environment variable. The account it made
       can then be deleted, and every safety review it recorded survives with
       its reviewer_email intact.
    """
    ident = (req.identifier or "").strip()
    pw = req.password or ""
    if not ident or not pw:
        raise HTTPException(400, "Enter a username and password")

    # ---- 2) dev login ------------------------------------------------- #
    if cfg.ADMIN_DEV_LOGIN and ident.lower() == (cfg.ADMIN_DEV_USER or "").strip().lower():
        if pw != cfg.ADMIN_DEV_PASS:
            raise HTTPException(401, "Wrong username or password")
        u = db.query(User).filter(User.email == cfg.ADMIN_DEV_EMAIL).first()
        if not u:
            u = User(
                email=cfg.ADMIN_DEV_EMAIL,
                auth_provider="local",
                first_name="Console",
                last_name="(dev login)",
                is_admin=True,
                onboarding_complete=True,
            )
            db.add(u)
            db.commit()
            db.refresh(u)
            print(f"[admin] created dev-login account {cfg.ADMIN_DEV_EMAIL}")
        elif not u.is_admin:
            u.is_admin = True
            db.commit()
        print("[admin] dev login used (ADMIN_DEV_LOGIN is on)")
        return LoginResp(token=make_token(u.id, u.email), email=u.email, mode="dev")

    # ---- 1) normal account -------------------------------------------- #
    u = db.query(User).filter(User.email == ident.lower()).first()
    if not u or not u.password_hash or not check_pw(pw, u.password_hash):
        raise HTTPException(401, "Wrong username or password")
    if not u.is_admin and cfg.is_admin_email(u.email):
        u.is_admin = True
        db.commit()
    if not u.is_admin:
        raise HTTPException(403, "This account does not have admin access")
    return LoginResp(token=make_token(u.id, u.email), email=u.email, mode="account")


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

@r.get("/overview", response_model=OverviewResp)
def overview(days: Optional[str] = Query(default=None),
             admin: User = Depends(get_current_admin_required),
             db: Session = Depends(get_db)):
    days = _clamp_days(days)
    start, end, prev_start, prev_end = _window(days)

    # ---- users -------------------------------------------------------- #
    total_users = db.query(func.count(User.id)).scalar() or 0
    new_users = (db.query(func.count(User.id))
                 .filter(User.created_at >= start.replace(tzinfo=None),
                         User.created_at < end.replace(tzinfo=None)).scalar() or 0)
    prev_users = (db.query(func.count(User.id))
                  .filter(User.created_at >= prev_start.replace(tzinfo=None),
                          User.created_at < prev_end.replace(tzinfo=None)).scalar() or 0)
    users_before = max(0, total_users - new_users)

    # ---- revenue ------------------------------------------------------ #
    # Straight from the payments ledger, which counts renewals. Anything
    # inferred from entitlements would count each subscriber once and go flat.
    # Both bounds always applied. An unbounded upper edge lets anything dated
    # after the window (a clock skew, a backfill, a test row) inflate the KPI
    # while never appearing in the per-day buckets, so the headline number and
    # the chart's final point silently disagree.
    def _sum_payments(a, b):
        return int(db.query(func.coalesce(func.sum(Payment.amount_cents), 0)).filter(
            Payment.created_at >= a.replace(tzinfo=None),
            Payment.created_at < b.replace(tzinfo=None)).scalar() or 0)

    revenue_cents = _sum_payments(start, end)
    prev_revenue_cents = _sum_payments(prev_start, prev_end)

    # ---- daily series ------------------------------------------------- #
    day_keys = [(start + timedelta(days=i)).date() for i in range(days)]
    signups_by_day: Dict[date, int] = {d: 0 for d in day_keys}
    revenue_by_day: Dict[date, int] = {d: 0 for d in day_keys}

    for (created,) in db.query(User.created_at).filter(
            User.created_at >= start.replace(tzinfo=None),
            User.created_at < end.replace(tzinfo=None)).all():
        d = _as_aware_utc(created)
        if d and d.date() in signups_by_day:
            signups_by_day[d.date()] += 1

    for created, cents in db.query(Payment.created_at, Payment.amount_cents).filter(
            Payment.created_at >= start.replace(tzinfo=None),
            Payment.created_at < end.replace(tzinfo=None)).all():
        d = _as_aware_utc(created)
        if d and d.date() in revenue_by_day:
            revenue_by_day[d.date()] += int(cents or 0)

    series: List[SeriesPoint] = []
    cum_rev = 0
    cum_users = users_before
    for d in day_keys:
        cum_rev += revenue_by_day[d]
        cum_users += signups_by_day[d]
        series.append(SeriesPoint(
            date=d.isoformat(),
            signups=signups_by_day[d],
            revenue=revenue_by_day[d] // 100,
            cum_revenue=cum_rev // 100,
            cum_users=cum_users,
        ))

    # ---- funnel ------------------------------------------------------- #
    cohort_ids = [uid for (uid,) in db.query(User.id).filter(
        User.created_at >= start.replace(tzinfo=None),
        User.created_at < end.replace(tzinfo=None)).all()]
    signed_up = len(cohort_ids)
    started_one = 0
    paid = 0
    if cohort_ids:
        started_one = (db.query(func.count(func.distinct(Protocol.user_id)))
                       .filter(Protocol.user_id.in_(cohort_ids)).scalar() or 0)
        paid = (db.query(func.count(func.distinct(Entitlement.user_id)))
                .filter(Entitlement.user_id.in_(cohort_ids)).scalar() or 0)

    # ---- retention curve ---------------------------------------------- #
    curve = _retention_curve(db)
    by_day = {c.day: c.rate for c in curve}

    return OverviewResp(
        range_days=days,
        revenue=revenue_cents // 100,
        prevRevenue=prev_revenue_cents // 100,
        totalUsers=total_users,
        newUsers=new_users,
        prevUsers=prev_users,
        retention=Retention(d7=by_day.get(7, 0.0), d30=by_day.get(30, 0.0)),
        curve=curve,
        funnel=Funnel(signedUp=signed_up, startedOne=started_one, paid=paid),
        series=series,
    )


def _retention_curve(db: Session) -> List[CurvePoint]:
    """Cohort retention: of everyone who signed up, the share still opening the
    app N days later.

    Computed from user_activity, which routes/auth.py writes once per user per
    day. Two rules keep it honest:

      * A user only counts in day N's denominator if they signed up at least N
        days ago -- otherwise a brand new account would drag d30 toward zero
        simply for not having existed for a month yet.
      * Day 0 is anchored at 1.0 by definition (everyone was present on the day
        they signed up), matching the chart's fixed origin.

    Reads two narrow columns and joins in Python rather than in SQL, because
    date arithmetic differs between SQLite and Postgres and correctness matters
    more than speed here. Push this down into SQL when users reach six figures.
    """
    signup: Dict[int, date] = {}
    for uid, created in db.query(User.id, User.created_at).all():
        d = _as_aware_utc(created)
        if d:
            signup[uid] = d.date()
    if not signup:
        return [CurvePoint(day=n, rate=0.0, cohort=0) for n in cfg.ADMIN_RETENTION_DAYS]

    offsets: Dict[int, set] = {uid: set() for uid in signup}
    for uid, adate in db.query(UserActivity.user_id, UserActivity.activity_date).all():
        s = signup.get(uid)
        if s and adate:
            offsets[uid].add((adate - s).days)

    today = datetime.now(timezone.utc).date()
    out: List[CurvePoint] = []
    for n in cfg.ADMIN_RETENTION_DAYS:
        eligible = [uid for uid, s in signup.items() if (today - s).days >= n]
        if n == 0:
            out.append(CurvePoint(day=0, rate=1.0, cohort=len(eligible)))
            continue
        if not eligible:
            out.append(CurvePoint(day=n, rate=0.0, cohort=0))
            continue
        hits = sum(1 for uid in eligible if n in offsets.get(uid, ()))
        out.append(CurvePoint(day=n, rate=round(hits / len(eligible), 4),
                              cohort=len(eligible)))
    return out


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #

@r.get("/people", response_model=PeopleResp)
def people(q: str = Query(default=""),
           admin: User = Depends(get_current_admin_required),
           db: Session = Depends(get_db)):
    """List rows only -- no charges here.

    Search covers the masked email and protocol goals (both plaintext).
    It deliberately does NOT cover charges: they are encrypted at rest, so
    matching them would mean decrypting every charge of every user on every
    keystroke. Goals are the searchable surface; charges are readable one
    person at a time, through the audited detail endpoint below.
    """
    term = (q or "").strip().lower()
    monthly_ids, protocol_ids = _paying_user_ids(db)

    users = db.query(User).order_by(User.created_at.desc()).limit(cfg.ADMIN_MAX_ROWS + 1).all()
    truncated = len(users) > cfg.ADMIN_MAX_ROWS
    users = users[:cfg.ADMIN_MAX_ROWS]
    ids = [u.id for u in users]

    counts: Dict[int, int] = {}
    latest: Dict[int, str] = {}
    if ids:
        for uid, cnt in (db.query(Protocol.user_id, func.count(Protocol.id))
                         .filter(Protocol.user_id.in_(ids))
                         .group_by(Protocol.user_id).all()):
            counts[uid] = cnt
        # Newest protocol per user, for the "Most recent goal" column.
        for p in (db.query(Protocol.user_id, Protocol.target, Protocol.created_at)
                  .filter(Protocol.user_id.in_(ids))
                  .order_by(Protocol.created_at.asc()).all()):
            latest[p.user_id] = p.target or ""

    rows: List[PersonRow] = []
    for u in users:
        masked = mask_email(u.email)
        goal = latest.get(u.id, "")
        if term and term not in masked.lower() and term not in (goal or "").lower():
            continue
        rows.append(PersonRow(
            id=str(u.id),
            email_masked=masked,
            created_at=_ts(u.created_at) or "",
            plan=_plan_for(u.id, monthly_ids, protocol_ids),
            last_active=_ts(u.last_active_at),
            protocol_count=counts.get(u.id, 0),
            latest_goal=goal,
        ))

    return PeopleResp(people=rows, total=len(rows), truncated=truncated)


@r.get("/people/{user_id}", response_model=PersonDetail)
def person_detail(user_id: int, request: Request,
                  admin: User = Depends(get_current_admin_required),
                  db: Session = Depends(get_db)):
    """Everything one person wrote, in their own words.

    This is the only endpoint that decrypts Protocol.charge, and it writes an
    AuditLog row on every call. Keeping it separate from the list is what makes
    that audit trail meaningful -- it records that a specific admin read a
    specific person's private text at a specific time, rather than merely that
    somebody opened a tab.
    """
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "person not found")

    _audit(db, admin, request, "admin.read_person", f"user:{u.id}")

    monthly_ids, protocol_ids = _paying_user_ids(db)
    ps = (db.query(Protocol).filter(Protocol.user_id == u.id)
          .order_by(Protocol.created_at.desc()).all())

    pids = [p.id for p in ps]
    jolted: Dict[int, set] = {pid: set() for pid in pids}
    if pids:
        for pid, dayno in (db.query(ProtocolJolt.protocol_id, ProtocolJolt.day)
                           .filter(ProtocolJolt.protocol_id.in_(pids),
                                   ProtocolJolt.stage == "done").all()):
            jolted.setdefault(pid, set()).add(dayno)

    now = datetime.now(timezone.utc)
    out: List[ProtocolOut] = []
    for p in ps:
        done_days = jolted.get(p.id, set())
        day = len(done_days)
        if (p.status or "") == "complete" or day >= cfg.PROTOCOL_DAYS:
            status = "complete"
        else:
            # "stalled" is a console-only reading of an active protocol that has
            # gone quiet. The backend itself only knows active | complete, and
            # nothing is written back to the database here.
            updated = _as_aware_utc(p.updated_at) or _as_aware_utc(p.created_at)
            stale = updated and (now - updated).days >= cfg.PROTOCOL_STALLED_DAYS
            status = "stalled" if stale else "active"

        charge = ""
        if cfg.ADMIN_SHOW_CHARGE and p.charge:
            charge = decrypt_field(p.charge) or ""

        out.append(ProtocolOut(
            goal=p.target or "",
            charge=charge,
            day=day,
            status=status,
            created_at=_ts(p.created_at) or "",
        ))

    journal_count = (db.query(func.count(JournalEntry.id))
                     .filter(JournalEntry.user_id == u.id).scalar() or 0)

    return PersonDetail(
        id=str(u.id),
        email_masked=mask_email(u.email),
        created_at=_ts(u.created_at) or "",
        plan=_plan_for(u.id, monthly_ids, protocol_ids),
        last_active=_ts(u.last_active_at),
        protocols=out,
        journal_count=journal_count,
    )


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #

@r.get("/safety", response_model=SafetyResp)
def safety(days: Optional[str] = Query(default=None),
           scope: str = Query(default="open"),
           request: Request = None,
           admin: User = Depends(get_current_admin_required),
           db: Session = Depends(get_db)):
    """The review queue.

    Reading this decrypts SafetyEvent.said -- the verbatim text a person wrote
    when a screen fired on them -- so it is audited like person detail.

    "reviewed" and "confirmed" are counted over ALL events, not just the window,
    because "was the screen right?" is a property of the screen rather than of
    the last 30 days, and a rate computed from a handful of recent cases would
    swing wildly.
    """
    days = _clamp_days(days)
    start, end, _, _ = _window(days)
    scope = (scope or "open").strip().lower()

    events = (db.query(SafetyEvent)
              .filter(SafetyEvent.created_at >= start.replace(tzinfo=None),
                      SafetyEvent.created_at < end.replace(tzinfo=None))
              .order_by(SafetyEvent.created_at.desc())
              .limit(cfg.ADMIN_MAX_ROWS).all())

    ids = [e.id for e in events]
    reviews: Dict[int, SafetyReview] = {}
    if ids:
        for rv in db.query(SafetyReview).filter(SafetyReview.safety_event_id.in_(ids)).all():
            reviews[rv.safety_event_id] = rv

    _audit(db, admin, request, "admin.read_safety_queue",
           f"window:{days}d events:{len(events)}")

    cases: List[SafetyCase] = []
    for e in events:
        rv = reviews.get(e.id)
        if scope == "open" and rv is not None:
            continue
        cases.append(SafetyCase(
            id=str(e.id),
            ts=_ts(e.created_at) or "",
            sev=e.severity or "S2",
            category=e.category or "unspecified",
            said=(decrypt_field(e.said) or "") if e.said else "",
            layer=e.layer or "L1",
            source=e.source or "",
            auto=e.auto_action or "",
            user=(f"u_{e.user_id}" if e.user_id else "deleted account"),
            decision=(rv.decision if rv else None),
            decided_by=(rv.reviewer_email if rv else None),
            decided_at=(_ts(rv.created_at) if rv else None),
        ))

    total_reviewed = db.query(func.count(SafetyReview.id)).scalar() or 0
    total_confirmed = (db.query(func.count(SafetyReview.id))
                       .filter(SafetyReview.decision == "confirmed").scalar() or 0)
    open_count = sum(1 for e in events if e.id not in reviews)

    return SafetyResp(cases=cases, reviewed=total_reviewed,
                      confirmed=total_confirmed, open_count=open_count)


@r.post("/safety/{event_id}/review", response_model=Ok)
def set_review(event_id: int, req: ReviewReq,
               admin: User = Depends(get_current_admin_required),
               db: Session = Depends(get_db)):
    """Record 'Right call' / 'Wrong call' on one flag.

    The two values match what the console's buttons already send:
      confirmed -> the screen was right to fire
      false     -> the screen stopped someone who was fine

    Re-deciding replaces the previous answer. The SafetyEvent itself is never
    edited: the flag is what happened, the review is what we think of it.
    """
    decision = (req.decision or "").strip().lower()
    if decision not in ("confirmed", "false"):
        raise HTTPException(400, "decision must be 'confirmed' or 'false'")

    ev = db.query(SafetyEvent).filter(SafetyEvent.id == event_id).first()
    if not ev:
        raise HTTPException(404, "safety event not found")

    existing = (db.query(SafetyReview)
                .filter(SafetyReview.safety_event_id == event_id).first())
    if existing:
        existing.decision = decision
        existing.note = (req.note or "").strip() or None
        existing.reviewer_user_id = admin.id
        existing.reviewer_email = admin.email
        existing.created_at = datetime.now(timezone.utc)
    else:
        db.add(SafetyReview(
            safety_event_id=event_id,
            reviewer_user_id=admin.id,
            reviewer_email=admin.email,
            decision=decision,
            note=(req.note or "").strip() or None,
        ))
    db.commit()
    print(f"[admin] {admin.email} marked safety event {event_id} as {decision}")
    return Ok(status="ok")


@r.delete("/safety/{event_id}/review", response_model=Ok)
def clear_review(event_id: int,
                 admin: User = Depends(get_current_admin_required),
                 db: Session = Depends(get_db)):
    """Undo a decision, putting the case back in the open queue."""
    (db.query(SafetyReview)
     .filter(SafetyReview.safety_event_id == event_id)
     .delete(synchronize_session=False))
    db.commit()
    return Ok(status="ok")
