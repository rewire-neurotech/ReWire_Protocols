"""
Safety event logging (admin console).

Today, when a safety layer stops something, the app does the right thing for the
person -- shows crisis resources, asks for a different goal -- and then discards
the verdict AND their words. Nothing is written anywhere. That means:

  * the Safety tab in the console has nothing to show, and
  * there is no way to answer "is the screen tuned correctly?", because the
    cases it fired on no longer exist.

This module is the single place that turns a screen result into a durable row.
It is called from three layers:

  L1  routes/protocols.py        input screen said clarify | block | crisis
      routes/protocol_jolt.py    same, on a journal jolt request
  L2  routes/protocols.py        the PLAN prompt returned unsafe_goal
      tasks.py                   the SPEECH prompt returned REWIRE_UNSAFE
  L3  tasks.py                   the output screen failed the generated speech

DESIGN RULE, ABSOLUTE: logging must never change what the user experiences.
Every function here swallows its own exceptions and returns None on failure. A
person in crisis gets their resources whether or not the insert succeeded; a
telemetry problem must never become a user-facing one.

PRIVACY: `said` holds the verbatim text the person wrote and is stored ENCRYPTED
via utils.encryption.encrypt_field, exactly like Protocol.charge. It is the most
sensitive column in the schema and must be decrypted for display.
"""

from typing import Optional

from app.core.config import cfg
from app.models import SafetyEvent
from app.utils.encryption import encrypt_field


# The generation layers raise ProtocolUnsafe with their own vocabulary
# ("unsafe_goal" from the plan prompt, "fail" from the output screen). Normalise
# to the small set core/config.py knows how to map onto a severity, so a new
# verdict string appearing upstream can never silently land as S2-by-accident.
_VERDICT_ALIASES = {
    "unsafe_goal": "unsafe",
    "rewire_unsafe": "unsafe",
}


def normalize_verdict(verdict: str) -> str:
    v = (verdict or "").strip().lower()
    return _VERDICT_ALIASES.get(v, v)


def compose_said(target: str = "", charge: str = "") -> str:
    """Build the verbatim block shown on a queue card.

    The console card shows one quoted passage. A protocol create has two fields
    and the charge is usually where the real signal lives, so both are kept,
    labelled, in the order the person filled them in.
    """
    target = (target or "").strip()
    charge = (charge or "").strip()
    if target and charge:
        return f"Goal: {target}\n\nWhy it matters: {charge}"
    return target or charge or ""


def log_safety_event(
    db,
    *,
    user_id: Optional[int],
    layer: str,
    source: str,
    verdict: str,
    category: str = "",
    said: str = "",
    rationale: str = "",
    protocol_id: Optional[int] = None,
    journal_entry_id: Optional[int] = None,
    jolt_id: Optional[int] = None,
) -> Optional[int]:
    """Record one safety flag. Returns the new event id, or None on any failure.

    db      : an open SQLAlchemy session. Committed here, so callers should not
              have uncommitted work pending that they did not intend to flush.
    layer   : "L1" (input screen) | "L2" (generation prompt) | "L3" (output screen)
    source  : where it happened -- protocol_create | protocol_plan |
              protocol_jolt | journal_jolt
    verdict : clarify | block | crisis | unsafe | fail  (aliases normalised)
    said    : the verbatim user text; encrypted before it is stored
    """
    try:
        v = normalize_verdict(verdict)
        ev = SafetyEvent(
            user_id=user_id,
            layer=(layer or "").strip().upper()[:4] or "L1",
            source=(source or "unknown")[:30],
            verdict=v[:20] or "unknown",
            category=((category or "").strip() or None),
            severity=cfg.severity_for(v, category),
            said=(encrypt_field(said) if said else None),
            auto_action=cfg.auto_action_for(v),
            rationale=((rationale or "").strip() or None),
            protocol_id=protocol_id,
            journal_entry_id=journal_entry_id,
            jolt_id=jolt_id,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        print(f"[safety] logged {ev.layer} {ev.verdict}/{ev.category} "
              f"{ev.severity} user={user_id} source={source} id={ev.id}")
        return ev.id
    except Exception as e:
        # Never let telemetry break the user path -- see the module docstring.
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[safety] FAILED to log {layer}/{verdict} for user {user_id}: {e}")
        return None


def log_safety_event_bg(**kwargs) -> Optional[int]:
    """Same as log_safety_event, but opens and closes its own session.

    For callers with no request session of their own -- specifically the
    background generation worker in tasks.py, which deliberately holds no DB
    session while it does the slow speech / TTS / mix work.
    """
    from app.db import SessionLocal

    if SessionLocal is None:
        return None
    db = SessionLocal()
    try:
        return log_safety_event(db, **kwargs)
    except Exception as e:
        print(f"[safety] background log failed: {e}")
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass
