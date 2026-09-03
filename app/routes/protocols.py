from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db
from app.models import Protocol, ProtocolDay, ProtocolJolt, JournalEntry, Entitlement, User
from app.routes.auth import get_current_user_required
from app.utils.encryption import encrypt_field, decrypt_field
from app.services import llm
from app.services.safety_log import log_safety_event, compose_said

r = APIRouter(prefix="/api/protocols", tags=["protocols"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class DayOut(BaseModel):
    day: int
    stage: Optional[str] = None
    action: Optional[str] = None      # revealed only once the day's jolt is done
    done: bool = False
    jolted: bool = False
    jolt_id: Optional[int] = None
    audio_url: Optional[str] = None   # for replay, when a finished jolt exists
    done_at: Optional[str] = None     # when this day's action was marked done (ISO, UTC)


class ProtocolOut(BaseModel):
    id: int
    type: str
    place: Optional[str] = None       # forest | ocean | fire (expand meditations)
    target: str
    title: Optional[str] = None
    # LLM home-card summary, generated at create time. Third person, never
    # quotes the user's words back.
    summary: Optional[str] = None
    unlocked: bool
    status: str
    is_active: bool
    current_day: Optional[int] = None   # next un-jolted day (1-5), None if complete
    created_at: str
    updated_at: str
    days: List[DayOut]


class ProtocolsList(BaseModel):
    protocols: List[ProtocolOut]
    total: int


class CreateProtocolReq(BaseModel):
    type: str = "activate"
    place: Optional[str] = None       # forest | ocean | fire, required for expand
    target: str
    charge: str = ""
    title: Optional[str] = None


class CreateProtocolResp(BaseModel):
    status: str                       # created | clarify | block | crisis
    category: Optional[str] = None
    message: Optional[str] = None
    protocol: Optional[ProtocolOut] = None


class UpdateProtocolReq(BaseModel):
    title: Optional[str] = None
    charge: Optional[str] = None


class MarkDoneReq(BaseModel):
    done: bool = True


class JournalReq(BaseModel):
    text: str
    protocol_id: Optional[int] = None
    day: Optional[int] = None
    question: Optional[str] = None


class JournalOut(BaseModel):
    id: int
    protocol_id: Optional[int] = None
    day: Optional[int] = None
    question: Optional[str] = None
    answer: str
    created_at: str
    jolt_audio_url: Optional[str] = None   # replay URL for a reflection's jolt (None for free entries)
    chills: Optional[str] = None           # "yes" | "no" | null


class JournalList(BaseModel):
    entries: List[JournalOut]
    total: int


class Ok(BaseModel):
    status: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ts(dt):
    # Normalize to aware UTC before formatting. SQLite reads timestamps back as
    # naive datetimes (tzinfo dropped), so a bare isoformat() would emit no zone
    # and the browser's new Date() would read the UTC time as local -- shifting
    # every date/time by the client's offset (and by a whole day near midnight).
    # Emitting an explicit +00:00 makes every client parse the correct instant.
    if not dt:
        return ""
    return _as_aware_utc(dt).isoformat()


def _as_aware_utc(dt):
    """Normalize a (possibly naive Postgres) datetime to aware UTC for comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_time_unlocked(prev_done_at, now=None) -> bool:
    """Daily-cadence gate for a protocol's day-to-day loop.

    Returns True once the calendar has moved PAST the day on which the
    previous day's action was marked done, so the next day's jolt unlocks
    on the following calendar day rather than the same day. Judged in UTC
    so the server and the client (which computes the same thing from the
    day's done_at) always agree; per-user local-time handling can layer on
    later. Returns False when the previous day is not done yet.

        prev_done_at : previous ProtocolDay.done_at (naive-UTC or aware) or None
        now          : override for 'now' (testing); defaults to current UTC
    """
    if prev_done_at is None:
        return False
    done = _as_aware_utc(prev_done_at)
    ref = now or datetime.now(timezone.utc)
    return ref.date() > done.date()


def _has_monthly(uid, db) -> bool:
    """True if the user has an active monthly membership entitlement."""
    e = (db.query(Entitlement)
         .filter(Entitlement.user_id == uid,
                 Entitlement.kind == "monthly",
                 Entitlement.status == "active")
         .first())
    if not e:
        return False
    exp = _as_aware_utc(e.expires_at)
    if exp and exp < datetime.now(timezone.utc):
        e.status = "expired"
        db.commit()
        return False
    return True


def _protocol_unlocked(p, uid, db) -> bool:
    """Days 2-5 are unlocked if this protocol was purchased, or the user has a
    monthly membership (which unlocks all protocols)."""
    return bool(p.unlocked) or _has_monthly(uid, db)


def _audio_url_for(filename: str) -> str:
    # Served by the protocol-jolt route (see routes/protocol_jolt.py).
    base = cfg.PUBLIC_BASE_URL.rstrip("/") if cfg.PUBLIC_BASE_URL else ""
    return f"{base}/api/protocol-jolt/audio/{filename}"


def _own(pid, u, db) -> Protocol:
    p = db.query(Protocol).filter(Protocol.id == pid, Protocol.user_id == u.id).first()
    if not p:
        raise HTTPException(404, "protocol not found")
    return p


def _protocol_out(p, db, uid) -> ProtocolOut:
    days = (db.query(ProtocolDay)
            .filter(ProtocolDay.protocol_id == p.id)
            .order_by(ProtocolDay.day).all())
    done_jolts = (db.query(ProtocolJolt)
                  .filter(ProtocolJolt.protocol_id == p.id, ProtocolJolt.stage == "done")
                  .all())
    # latest finished jolt per day
    by_day = {}
    for j in done_jolts:
        cur = by_day.get(j.day)
        if cur is None or j.id > cur.id:
            by_day[j.day] = j

    # Pre generation fix (Ashwin, Aug 2026): a finished jolt row no longer
    # means the day was experienced, because days 2-5 are generated ahead at
    # the prior day's reflect and sit ready on disk before anyone hears
    # them. For meditation protocols a day therefore counts as completed
    # only when its questionnaire was saved: jolted and current_day are
    # derived from the reflection rows, so a pre generated day never shows
    # as a replay row and never advances the frontier, and a day whose
    # questionnaire was skipped repeats until it is answered. Activate
    # protocols keep the original jolt-row meaning.
    is_meditation = (p.type or "") in ("integrate", "expand")
    reflected_days = set()
    if is_meditation:
        refl_rows = (db.query(JournalEntry.day)
                     .filter(JournalEntry.protocol_id == p.id,
                             JournalEntry.day.isnot(None))
                     .all())
        reflected_days = {row.day for row in refl_rows}

    day_outs = []
    current_day = None
    for d in days:
        jj = by_day.get(d.day)
        if is_meditation:
            jolted = jj is not None and d.day in reflected_days
        else:
            jolted = jj is not None
        if not jolted and current_day is None:
            current_day = d.day
        day_outs.append(DayOut(
            day=d.day,
            stage=d.stage,
            action=(d.action if jolted else None),
            done=bool(d.done),
            jolted=jolted,
            jolt_id=(jj.id if jolted and jj else None),
            audio_url=(_audio_url_for(jj.audio_filename) if jolted and jj and jj.audio_filename else None),
            done_at=(_ts(d.done_at) or None),
        ))

    return ProtocolOut(
        id=p.id,
        type=p.type or "activate",
        place=(p.place or None),
        target=p.target or "",
        title=p.title,
        summary=(p.summary or None),
        unlocked=_protocol_unlocked(p, uid, db),
        status=p.status or "active",
        is_active=bool(p.is_active),
        current_day=current_day,
        created_at=_ts(p.created_at),
        updated_at=_ts(p.updated_at),
        days=day_outs,
    )


def _journal_out(e, jolt_audio_url=None) -> JournalOut:
    return JournalOut(
        id=e.id,
        protocol_id=e.protocol_id,
        day=e.day,
        question=e.question,
        answer=decrypt_field(e.answer) or "" if e.answer else "",
        created_at=_ts(e.created_at),
        jolt_audio_url=jolt_audio_url,
        chills=e.chills or None,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@r.get("", response_model=ProtocolsList)
def list_protocols(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    ps = (db.query(Protocol)
          .filter(Protocol.user_id == u.id)
          .order_by(Protocol.is_active.desc(), Protocol.created_at.desc()).all())
    return ProtocolsList(protocols=[_protocol_out(p, db, u.id) for p in ps], total=len(ps))


# NOTE: static sub-paths are declared before the dynamic "/{pid}" route so they
# are matched first (FastAPI resolves in declaration order).

@r.get("/journal", response_model=JournalList)
def list_journal(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    es = (db.query(JournalEntry)
          .filter(JournalEntry.user_id == u.id)
          .order_by(JournalEntry.created_at.desc()).all())
    # A reflection is tied to a protocol + day; attach that day's finished jolt
    # audio so the client can replay the original jolt instead of generating a
    # new one. Free-form entries (no protocol/day) get None and will generate.
    out = []
    for e in es:
        audio = None
        if e.protocol_id and e.day:
            jj = (db.query(ProtocolJolt)
                  .filter(ProtocolJolt.protocol_id == e.protocol_id,
                          ProtocolJolt.day == e.day,
                          ProtocolJolt.stage == "done",
                          ProtocolJolt.audio_filename.isnot(None))
                  .order_by(ProtocolJolt.id.desc()).first())
            if jj:
                audio = _audio_url_for(jj.audio_filename)
        out.append(_journal_out(e, audio))
    return JournalList(entries=out, total=len(out))


@r.post("/journal", response_model=JournalOut)
def add_journal(req: JournalReq, u: User = Depends(get_current_user_required),
                db: Session = Depends(get_db)):
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text required")
    # if a protocol is referenced, it must belong to the user
    if req.protocol_id is not None:
        _own(req.protocol_id, u, db)
    e = JournalEntry(
        user_id=u.id,
        protocol_id=req.protocol_id,
        day=req.day,
        question=(req.question.strip() if req.question else None),
        answer=encrypt_field(req.text.strip()),
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return _journal_out(e)


@r.get("/export")
def export_data(u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    ps = db.query(Protocol).filter(Protocol.user_id == u.id).order_by(Protocol.created_at.desc()).all()
    out = []
    for p in ps:
        days = db.query(ProtocolDay).filter(ProtocolDay.protocol_id == p.id).order_by(ProtocolDay.day).all()
        out.append({
            "type": p.type or "activate",
            "target": p.target or "",
            "title": p.title or "",
            "summary": p.summary or "",
            "status": p.status or "active",
            "created_at": _ts(p.created_at),
            "days": [
                {"day": d.day, "stage": d.stage, "action": d.action or "",
                 "done": bool(d.done), "done_at": _ts(d.done_at) or None}
                for d in days
            ],
        })
    js = db.query(JournalEntry).filter(JournalEntry.user_id == u.id).order_by(JournalEntry.created_at).all()
    journal = [
        {"protocol_id": e.protocol_id, "day": e.day, "question": e.question or "",
         "answer": decrypt_field(e.answer) or "" if e.answer else "", "chills": e.chills or "",
         "created_at": _ts(e.created_at)}
        for e in js
    ]
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "protocols": out, "journal": journal}


@r.post("", response_model=CreateProtocolResp)
def create_protocol(req: CreateProtocolReq, u: User = Depends(get_current_user_required),
                    db: Session = Depends(get_db)):
    ptype = (req.type or "activate").strip().lower()
    if ptype not in cfg.PROTOCOL_TYPES:
        raise HTTPException(400, "unknown protocol type")
    # Meditation build (Aug 2026): integrate (regular meditation) and expand
    # (place meditations) are live and use the meditation track registry.
    # Activate still requires its own tracks.
    place = (req.place or "").strip().lower() or None
    if ptype == "expand":
        if place not in ("forest", "ocean", "fire"):
            raise HTTPException(400, "place must be forest, ocean or fire")
    else:
        place = None
    if ptype == "activate" and not cfg.PROTOCOL_TRACKS.get(ptype):
        raise HTTPException(400, "protocol type not available yet")
    if not req.target or not req.target.strip():
        raise HTTPException(400, "target required")

    target = req.target.strip()
    charge = req.charge.strip() if req.charge else ""

    # 1) Upstream input safety screen (safe | clarify | block | crisis)
    verdict = llm.screen_input(target, charge)
    v = verdict.get("verdict", "clarify")
    if v != "safe":
        # ADMIN CONSOLE: record the flag BEFORE returning. Previously this
        # verdict -- and the words that produced it -- were discarded here, so
        # every crisis and block the screen has ever caught left no trace. The
        # person's experience is unchanged: they still get the same response,
        # immediately, whether or not this write succeeds (log_safety_event
        # never raises).
        log_safety_event(
            db,
            user_id=u.id,
            layer="L1",
            source="protocol_create",
            verdict=v,
            category=verdict.get("category", "other"),
            said=compose_said(target, charge),
            rationale=verdict.get("rationale", ""),
        )
        # Clarify feedback (Felix, Sep 2026): give the user an actual question
        # to answer instead of the generic screen, so they know what to fix.
        # Empty on any failure; the frontend falls back to its generic text.
        message = None
        if v == "clarify":
            message = llm.generate_clarify_question(target, charge) or None
        return CreateProtocolResp(status=v, category=verdict.get("category", "other"), message=message, protocol=None)

    # 2) Plan the 5-day arc. Meditation protocols carry no per-day actions:
    # each day's script is generated fresh from the growing history, so the
    # plan step is skipped and the day rows are bare placeholders.
    plan = None
    if ptype == "activate":
        try:
            plan = llm.generate_plan(target, charge)
        except llm.ProtocolUnsafe as e:
            # Plan judged the goal unsafe -> treat like a soft block.
            # ADMIN CONSOLE: this is a second-layer catch -- the input screen
            # passed it and the plan prompt refused it. Those are the most
            # interesting rows in the whole queue, because they are exactly
            # where L1 was too lenient.
            log_safety_event(
                db,
                user_id=u.id,
                layer="L2",
                source="protocol_plan",
                verdict=(e.verdict or "unsafe"),
                category=(e.category or "other"),
                said=compose_said(target, charge),
                rationale=(e.detail or ""),
            )
            return CreateProtocolResp(status="block", category="other", protocol=None)

    # 3) Home card title and summary (Aug 2026). Felix: the title must fit
    # between the two arrows, so 5 words max, made NOW instead of after the
    # first reflect. The summary is third person and never quotes the user's
    # words back. Both are cheap Haiku calls with safe fallbacks inside, so
    # this cannot fail the create.
    title = req.title.strip()[:200] if req.title and req.title.strip() else None
    if not title:
        title = llm.generate_protocol_title(target)
    summary = llm.generate_protocol_summary(target, charge)

    # 4) Store: deactivate previous active protocols, then create this one active.
    db.query(Protocol).filter(
        Protocol.user_id == u.id, Protocol.is_active == True  # noqa: E712
    ).update({"is_active": False})

    p = Protocol(
        user_id=u.id,
        type=ptype,
        place=place,
        target=target[:2000],
        charge=encrypt_field(charge) if charge else None,
        title=title,
        summary=summary,
        input_verdict="safe",
        input_category="none",
        unlocked=False,
        status="active",
        is_active=True,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    if plan:
        for d in plan["days"]:
            db.add(ProtocolDay(
                protocol_id=p.id,
                day=d["day"],
                stage=d.get("stage"),
                action=d.get("action"),
                brief=d.get("brief"),
            ))
    else:
        for n in range(1, cfg.PROTOCOL_DAYS + 1):
            db.add(ProtocolDay(protocol_id=p.id, day=n))
    db.commit()

    return CreateProtocolResp(status="created", protocol=_protocol_out(p, db, u.id))


@r.get("/{pid}", response_model=ProtocolOut)
def get_protocol(pid: int, u: User = Depends(get_current_user_required),
                 db: Session = Depends(get_db)):
    p = _own(pid, u, db)
    return _protocol_out(p, db, u.id)


@r.put("/{pid}", response_model=ProtocolOut)
def update_protocol(pid: int, req: UpdateProtocolReq,
                    u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    p = _own(pid, u, db)
    if req.title is not None:
        p.title = req.title.strip()[:200] or None
    if req.charge is not None:
        p.charge = encrypt_field(req.charge.strip()) if req.charge.strip() else None
    db.commit()
    db.refresh(p)
    return _protocol_out(p, db, u.id)


@r.put("/{pid}/activate", response_model=ProtocolOut)
def activate_protocol(pid: int, u: User = Depends(get_current_user_required),
                      db: Session = Depends(get_db)):
    p = _own(pid, u, db)
    db.query(Protocol).filter(
        Protocol.user_id == u.id, Protocol.is_active == True  # noqa: E712
    ).update({"is_active": False})
    p.is_active = True
    db.commit()
    db.refresh(p)
    return _protocol_out(p, db, u.id)


@r.put("/{pid}/days/{day}/done", response_model=ProtocolOut)
def mark_day_done(pid: int, day: int, req: MarkDoneReq = MarkDoneReq(),
                  u: User = Depends(get_current_user_required), db: Session = Depends(get_db)):
    p = _own(pid, u, db)
    d = (db.query(ProtocolDay)
         .filter(ProtocolDay.protocol_id == p.id, ProtocolDay.day == day).first())
    if not d:
        raise HTTPException(404, "day not found")
    d.done = bool(req.done)
    d.done_at = datetime.now(timezone.utc) if req.done else None

    # If all five days are done, mark the protocol complete.
    all_days = db.query(ProtocolDay).filter(ProtocolDay.protocol_id == p.id).all()
    if all_days and all(x.done for x in all_days):
        p.status = "complete"
    else:
        p.status = "active"
    db.commit()
    db.refresh(p)
    return _protocol_out(p, db, u.id)
