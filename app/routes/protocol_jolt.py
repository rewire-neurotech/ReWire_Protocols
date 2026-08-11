from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db
from app.models import Protocol, ProtocolDay, ProtocolJolt, JournalEntry, JournalJolt, User
from app.routes.auth import get_current_user_required
from app.routes.protocols import _audio_url_for, _protocol_unlocked, _own, _as_aware_utc, day_time_unlocked
from app.utils.encryption import encrypt_field, decrypt_field, decrypt_file_to_bytes
from app import tasks
from app.services import llm
from app.services.safety_log import log_safety_event, compose_said

r = APIRouter(prefix="/api/protocol-jolt", tags=["protocol-jolt"])

# Stages that never change again on their own.
_TERMINAL_STAGES = ("done", "error", "blocked")
# A jolt still non-terminal after this long was orphaned (generation is 1-4 min).
_STALE_AFTER_MIN = 15


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class StartReq(BaseModel):
    day: int


class StartResp(BaseModel):
    status: str                       # generating | replay
    jolt_id: Optional[int] = None
    day: Optional[int] = None
    audio_url: Optional[str] = None   # set when status == "replay"
    message: Optional[str] = None


class StatusResp(BaseModel):
    jolt_id: int
    stage: str
    progress: int
    audio_url: Optional[str] = None
    error: Optional[str] = None


class ReflectReq(BaseModel):
    question: str = ""
    answer: str = ""
    chills: str = ""


class Ok(BaseModel):
    status: str


class JournalJoltStartResp(BaseModel):
    status: str                       # generating | replay | crisis | block
    jolt_id: Optional[int] = None
    audio_url: Optional[str] = None   # set when status == "replay"
    category: Optional[str] = None    # set when status == "crisis" | "block"
    message: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _latest_done(pid, day, db):
    """Most recent finished jolt for a protocol day, or None."""
    return (db.query(ProtocolJolt)
            .filter(ProtocolJolt.protocol_id == pid,
                    ProtocolJolt.day == day,
                    ProtocolJolt.stage == "done",
                    ProtocolJolt.audio_filename.isnot(None))
            .order_by(ProtocolJolt.id.desc())
            .first())


def _in_progress(pid, day, db):
    """A jolt for this day still generating (non-terminal), or None."""
    return (db.query(ProtocolJolt)
            .filter(ProtocolJolt.protocol_id == pid,
                    ProtocolJolt.day == day,
                    ~ProtocolJolt.stage.in_(_TERMINAL_STAGES))
            .order_by(ProtocolJolt.id.desc())
            .first())


def _done_days(pid, db):
    """Set of day numbers that already have a finished jolt."""
    rows = (db.query(ProtocolJolt)
            .filter(ProtocolJolt.protocol_id == pid, ProtocolJolt.stage == "done")
            .all())
    return {j.day for j in rows}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@r.post("/{pid}/start", response_model=StartResp)
def start_jolt(pid: int, req: StartReq,
               u: User = Depends(get_current_user_required),
               db: Session = Depends(get_db)):
    p = _own(pid, u, db)
    day = req.day
    if day < 1 or day > cfg.PROTOCOL_DAYS:
        raise HTTPException(400, "invalid day")

    d = (db.query(ProtocolDay)
         .filter(ProtocolDay.protocol_id == pid, ProtocolDay.day == day).first())
    if not d:
        raise HTTPException(404, "day not found")

    # 1) Already generated -> replay for free (no charge, no regeneration).
    existing = _latest_done(pid, day, db)
    if existing:
        return StartResp(
            status="replay",
            jolt_id=existing.id,
            day=day,
            audio_url=_audio_url_for(existing.audio_filename),
        )

    # 2) Already generating -> return the in-flight jolt (avoid double work).
    inprog = _in_progress(pid, day, db)
    if inprog:
        return StartResp(status="generating", jolt_id=inprog.id, day=day)

    # 3) Sequence gating: only the current frontier day can be started (no
    #    skipping ahead to a later day).
    done = _done_days(pid, db)
    current_day = next((n for n in range(1, cfg.PROTOCOL_DAYS + 1) if n not in done), None)
    if current_day is not None and day > current_day:
        raise HTTPException(400, "complete earlier days first")

    # 4) Access gating (paywall): day 1 is free; days beyond the free window
    #    require an unlock (this protocol purchased, or an active monthly
    #    membership). Checked BEFORE the prior-day / daily-cadence gates below,
    #    so an unpaid user always reaches the paywall and can purchase, instead
    #    of being told to finish yesterday or come back tomorrow first.
    if day > cfg.PROTOCOL_FREE_DAYS and not _protocol_unlocked(p, u.id, db):
        raise HTTPException(402, "unlock required for this protocol")

    # 5) Prior-day + daily-cadence gating (only reached once the day is
    #    unlocked): yesterday's action must be marked done, and the calendar
    #    must have moved past the day it was completed (judged in UTC, matching
    #    the client's own check). The None guard fails open on any legacy row
    #    missing the timestamp.
    if day > 1:
        prev = (db.query(ProtocolDay)
                .filter(ProtocolDay.protocol_id == pid, ProtocolDay.day == day - 1).first())
        if not (prev and prev.done):
            raise HTTPException(403, "mark yesterday's step done to unlock today's jolt")
        if prev.done_at is not None and not day_time_unlocked(prev.done_at):
            raise HTTPException(403, "come back tomorrow for the next day's jolt")

    # 6) Create the jolt row and hand it to the background pool.
    j = ProtocolJolt(protocol_id=pid, day=day, user_id=u.id, stage="queued", progress=10)
    db.add(j)
    db.commit()
    db.refresh(j)

    tasks.submit_generation(j.id)

    return StartResp(status="generating", jolt_id=j.id, day=day)


@r.get("/audio/{fname}")
def serve_audio(fname: str, request: Request):
    fp = cfg.out_dir_path / fname
    if not fp.exists():
        raise HTTPException(404, "audio not found")
    data = decrypt_file_to_bytes(str(fp))
    total = len(data)
    mt = "audio/mpeg" if fname.endswith(".mp3") else "audio/wav"

    # Parse Range header for byte-range requests (Safari, mobile browsers).
    range_header = request.headers.get("range")
    if range_header:
        try:
            range_spec = range_header.strip()
            if range_spec.lower().startswith("bytes="):
                range_spec = range_spec[6:]
            parts = range_spec.split("-", 1)
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else total - 1

            end = min(end, total - 1)

            if start > end or start >= total:
                return Response(
                    content=b"",
                    status_code=416,
                    headers={"Content-Range": f"bytes */{total}"},
                )

            chunk = data[start:end + 1]
            return Response(
                content=chunk,
                status_code=206,
                media_type=mt,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": f"inline; filename={fname}",
                },
            )
        except (ValueError, IndexError):
            # Malformed Range header, fall through to full response
            pass

    # No Range header or malformed -- return full file
    return Response(
        content=data,
        media_type=mt,
        headers={
            "Content-Length": str(total),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"inline; filename={fname}",
        },
    )


@r.get("/{jid}/status", response_model=StatusResp)
def get_status(jid: int, u: User = Depends(get_current_user_required),
               db: Session = Depends(get_db)):
    j = db.query(ProtocolJolt).filter(ProtocolJolt.id == jid, ProtocolJolt.user_id == u.id).first()
    if not j:
        raise HTTPException(404, "jolt not found")

    # Safety net: a jolt still non-terminal after _STALE_AFTER_MIN minutes was
    # orphaned (e.g. by a restart the startup sweep didn't cover). Flip it to
    # error so the client fails fast instead of polling forever.
    if j.stage not in _TERMINAL_STAGES:
        created = _as_aware_utc(j.created_at)
        if created and (datetime.now(timezone.utc) - created) > timedelta(minutes=_STALE_AFTER_MIN):
            j.stage = "error"
            j.gen_error = "generation interrupted, please try again"
            db.commit()
            print(f"[protojolt] {j.id} marked stale after {_STALE_AFTER_MIN}min in stage limbo")

    url = None
    if j.stage == "done" and j.audio_filename:
        url = _audio_url_for(j.audio_filename)

    return StatusResp(jolt_id=j.id, stage=j.stage, progress=j.progress,
                      audio_url=url, error=j.gen_error)


@r.post("/{jid}/reflect", response_model=Ok)
def save_reflection(jid: int, req: ReflectReq,
                    u: User = Depends(get_current_user_required),
                    db: Session = Depends(get_db)):
    j = db.query(ProtocolJolt).filter(ProtocolJolt.id == jid, ProtocolJolt.user_id == u.id).first()
    if not j:
        raise HTTPException(404, "jolt not found")
    # Saved into the journal, tagged to this protocol + day.
    db.add(JournalEntry(
        user_id=u.id,
        protocol_id=j.protocol_id,
        day=j.day,
        question=req.question or None,
        answer=encrypt_field(req.answer) if req.answer else None,
        chills=req.chills.strip() or None,
    ))
    db.commit()
    return Ok(status="ok")


# --------------------------------------------------------------------------- #
# Journal jolts: a standalone jolt generated from a single journal entry. Free,
# not tied to a protocol day. Generated audio is served by /audio/{fname} above.
# --------------------------------------------------------------------------- #

@r.post("/journal/{entry_id}/start", response_model=JournalJoltStartResp)
def start_journal_jolt(entry_id: int,
                       u: User = Depends(get_current_user_required),
                       db: Session = Depends(get_db)):
    # 1) The entry must exist and belong to this user.
    entry = (db.query(JournalEntry)
             .filter(JournalEntry.id == entry_id, JournalEntry.user_id == u.id).first())
    if not entry:
        raise HTTPException(404, "journal entry not found")
    text = (decrypt_field(entry.answer) or "").strip() if entry.answer else ""
    if not text:
        raise HTTPException(400, "journal entry is empty")

    # 2) Already generated for this entry -> replay for free (no regeneration).
    existing = (db.query(JournalJolt)
                .filter(JournalJolt.journal_entry_id == entry_id,
                        JournalJolt.stage == "done",
                        JournalJolt.audio_filename.isnot(None))
                .order_by(JournalJolt.id.desc()).first())
    if existing:
        return JournalJoltStartResp(status="replay", jolt_id=existing.id,
                                    audio_url=_audio_url_for(existing.audio_filename))

    # 3) Already generating for this entry -> return the in-flight jolt.
    inprog = (db.query(JournalJolt)
              .filter(JournalJolt.journal_entry_id == entry_id,
                      ~JournalJolt.stage.in_(_TERMINAL_STAGES))
              .order_by(JournalJolt.id.desc()).first())
    if inprog:
        return JournalJoltStartResp(status="generating", jolt_id=inprog.id)

    # 4) Input safety screen on the entry text. Only stop on crisis / block; a
    #    journal entry is a reflection, not a goal, so benign-but-vague entries
    #    (safe / clarify) are allowed to proceed.
    verdict = llm.screen_input(text, "")
    v = verdict.get("verdict", "clarify")
    if v in ("crisis", "block"):
        # ADMIN CONSOLE: record the flag BEFORE returning. This path previously
        # discarded both the verdict and the entry text that produced it.
        #
        # Journal flags matter more than their volume suggests. A protocol
        # target is something a person chose to declare; a journal entry is
        # what they wrote when nobody asked them to be constructive, so it is
        # where genuine distress surfaces first. The entry itself is already
        # stored (encrypted) in journal_entries, but nothing recorded that the
        # screen fired on it, which meant the case was invisible for review.
        #
        # The user's experience is unchanged: same response, same speed,
        # whether or not this write succeeds (log_safety_event never raises).
        log_safety_event(
            db,
            user_id=u.id,
            layer="L1",
            source="journal_jolt",
            verdict=v,
            category=verdict.get("category", "other"),
            said=compose_said(text, ""),
            rationale=verdict.get("rationale", ""),
            journal_entry_id=entry_id,
        )
        return JournalJoltStartResp(status=v, category=verdict.get("category", "other"))

    # 5) Create the jolt row and hand it to the background pool.
    jj = JournalJolt(user_id=u.id, journal_entry_id=entry_id, stage="queued", progress=10)
    db.add(jj)
    db.commit()
    db.refresh(jj)

    tasks.submit_journal_generation(jj.id)

    return JournalJoltStartResp(status="generating", jolt_id=jj.id)


@r.get("/journal/{jj_id}/status", response_model=StatusResp)
def get_journal_status(jj_id: int, u: User = Depends(get_current_user_required),
                       db: Session = Depends(get_db)):
    j = db.query(JournalJolt).filter(JournalJolt.id == jj_id, JournalJolt.user_id == u.id).first()
    if not j:
        raise HTTPException(404, "jolt not found")

    # Stale-jolt safety net (same as protocol jolts): a non-terminal row older
    # than _STALE_AFTER_MIN was orphaned; flip it to error so the client fails
    # fast instead of polling forever.
    if j.stage not in _TERMINAL_STAGES:
        created = _as_aware_utc(j.created_at)
        if created and (datetime.now(timezone.utc) - created) > timedelta(minutes=_STALE_AFTER_MIN):
            j.stage = "error"
            j.gen_error = "generation interrupted, please try again"
            db.commit()

    url = None
    if j.stage == "done" and j.audio_filename:
        url = _audio_url_for(j.audio_filename)

    return StatusResp(jolt_id=j.id, stage=j.stage, progress=j.progress,
                      audio_url=url, error=j.gen_error)

@r.post("/journal/{jj_id}/reflect", response_model=Ok)
def save_journal_reflection(jj_id: int, req: ReflectReq,
                            u: User = Depends(get_current_user_required),
                            db: Session = Depends(get_db)):
    """Save chills answer on the original journal entry after a journal jolt."""
    jj = db.query(JournalJolt).filter(JournalJolt.id == jj_id, JournalJolt.user_id == u.id).first()
    if not jj:
        raise HTTPException(404, "journal jolt not found")
    entry = db.query(JournalEntry).filter(JournalEntry.id == jj.journal_entry_id).first()
    if entry:
        entry.chills = req.chills.strip() or None
    db.commit()
    return Ok(status="ok")
