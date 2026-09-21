"""Bridge to ChillsTV's database.

Accounts live in ChillsTV (rewire.bio). Edge recognizes them three ways:
the /go/{code} grant link, the cv_session cookie ChillsTV sets on
.rewire.bio, and plain email+password checked against ChillsTV's pbkdf2
hashes. All three resolve here, plus the research readers (questionnaire
answers, chills score) fed into speech generation.

Since Edge signup (Sept 2026) the bridge also writes: create_account
puts the new user straight into ChillsTV's users table with Edge access
granted, and record_consent stamps the notice acceptance. Same formats
as ChillsTV's own auth.py and db.py, so both apps read one identity.

Set CHILLSTV_DB_URL to ChillsTV's postgres connection string. A
sqlite:///path url also works for local dev against a chillstv.db file.
Everything returns None (or {}) when the bridge is not configured or the
DB is unreachable, so Edge still boots without it.
"""
import os
import hmac
import json
import time
import hashlib
import secrets
import sqlite3
import threading

from app.core.config import cfg

_lock = threading.Lock()
_pg = None


def enabled() -> bool:
    return bool(cfg.CHILLSTV_DB_URL)


def _is_sqlite() -> bool:
    return cfg.CHILLSTV_DB_URL.startswith("sqlite")


def _sqlite_path() -> str:
    return cfg.CHILLSTV_DB_URL.split("///", 1)[-1]


def _query(sql: str, params=()):
    """Run one SELECT, return list of dict rows. [] on any failure."""
    global _pg
    if not enabled():
        return []
    try:
        if _is_sqlite():
            conn = sqlite3.connect(_sqlite_path())
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        import psycopg2
        import psycopg2.extras
        with _lock:
            if _pg is None or _pg.closed:
                _pg = psycopg2.connect(cfg.CHILLSTV_DB_URL)
                _pg.autocommit = True
            try:
                cur = _pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(sql.replace("?", "%s"), params)
                rows = cur.fetchall()
                cur.close()
                return [dict(r) for r in rows]
            except psycopg2.Error:
                # one reconnect and retry, then give up
                try:
                    _pg.close()
                except Exception:
                    pass
                _pg = psycopg2.connect(cfg.CHILLSTV_DB_URL)
                _pg.autocommit = True
                cur = _pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(sql.replace("?", "%s"), params)
                rows = cur.fetchall()
                cur.close()
                return [dict(r) for r in rows]
    except Exception as e:
        print(f"[chillstv bridge] query failed: {type(e).__name__}: {e}")
        return []


def _one(sql: str, params=()):
    rows = _query(sql, params)
    return rows[0] if rows else None


# password check, same scheme as ChillsTV auth.py (pbkdf2$iters$salt$hex)
def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, hexhash = (stored or "").split("$", 3)
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), hexhash)
    except (ValueError, TypeError):
        return False


def user_by_session(token: str):
    """The users row for a live cv_session cookie, or None."""
    if not token:
        return None
    return _one(
        "SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, time.time()),
    )


def user_by_edge_code(code: str):
    """The users row for a /go/ code, or None. Revoked users have code ''."""
    if not code:
        return None
    return _one("SELECT * FROM users WHERE edge_code = ? AND edge_code != ''", (code,))


def user_by_id(user_id: int):
    if not user_id:
        return None
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def user_by_email(email: str):
    e = (email or "").strip().lower()
    if not e:
        return None
    return _one("SELECT * FROM users WHERE lower(email) = ? AND email != ''", (e,))


def verify_login(email: str, password: str):
    """email+password against ChillsTV credentials. users row or None."""
    u = user_by_email(email)
    if not u:
        return None
    if not verify_password(password, u.get("password_hash") or ""):
        return None
    return u


def has_edge_access(user: dict) -> bool:
    return bool(user) and (user.get("beta_status") or "none") == "granted"


def _json(s, fallback):
    try:
        v = json.loads(s or "")
        return v if v else fallback
    except (ValueError, TypeError):
        return fallback


def research_context(chillstv_user_id: int) -> dict:
    """The curated slice fed into speech generation prompts.

    Questionnaire answers, chills profile, and what they wrote after
    watching stimuli. Returns {} when unavailable so callers can just
    merge it in.
    """
    if not chillstv_user_id:
        return {}
    u = _one(
        "SELECT answers_json, score, percentile, top5_json, display_name "
        "FROM users WHERE id = ?",
        (chillstv_user_id,),
    )
    if not u:
        return {}
    out = {}
    answers = _json(u.get("answers_json"), {})
    if answers:
        out["answers"] = answers
    top5 = _json(u.get("top5_json"), [])
    if top5:
        out["top5"] = top5
    if u.get("score"):
        out["chills_score"] = u["score"]
    if u.get("percentile"):
        out["chills_percentile"] = u["percentile"]
    if u.get("display_name"):
        out["name"] = u["display_name"]
    reports = _query(
        "SELECT stimulus_id, chills, what_text, why_text FROM after_answers "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (chillstv_user_id,),
    )
    if reports:
        out["chills_reports"] = reports
    return out


def full_record(chillstv_user_id: int) -> dict:
    """Every piece of ChillsTV data linked to this user, read live.

    For research and export. Nothing is copied into Edge's DB; this is
    the complete picture on demand. {} when unavailable.
    """
    if not chillstv_user_id:
        return {}
    u = _one("SELECT * FROM users WHERE id = ?", (chillstv_user_id,))
    if not u:
        return {}
    u.pop("password_hash", None)
    u["answers_json"] = _json(u.get("answers_json"), {})
    u["top5_json"] = _json(u.get("top5_json"), [])
    u["vector_json"] = _json(u.get("vector_json"), [])
    uid = (chillstv_user_id,)
    return {
        "user": u,
        "after_answers": _query("SELECT * FROM after_answers WHERE user_id = ? ORDER BY created_at", uid),
        "video_watches": _query("SELECT * FROM video_watches WHERE user_id = ? ORDER BY watched_at", uid),
        "sends": _query("SELECT * FROM sends WHERE sender_user_id = ? ORDER BY created_at", uid),
        "send_responses": _query("SELECT * FROM send_responses WHERE respondent_user_id = ? ORDER BY created_at", uid),
        "duo_pairs": _query("SELECT * FROM duo_pairs WHERE user_id = ? OR partner_user_id = ? ORDER BY created_at", uid + uid),
        "video_comments": _query("SELECT * FROM video_comments WHERE user_id = ? ORDER BY created_at", uid),
        "contributions": _query("SELECT * FROM contributions WHERE submitted_by = ? ORDER BY created_at", uid),
        "events": _query("SELECT * FROM events WHERE user_id = ? ORDER BY created_at", uid),
        "tags": _query("SELECT tag, created_at FROM user_tags WHERE user_id = ? ORDER BY created_at", uid),
    }


# ── writes: Edge signup lands the account in ChillsTV ──────────────

PBKDF2_ITERATIONS = 260000  # same as ChillsTV auth.py


def _execute(sql: str, params=()) -> bool:
    """Run one INSERT/UPDATE. True on success, False on any failure."""
    global _pg
    if not enabled():
        return False
    try:
        if _is_sqlite():
            conn = sqlite3.connect(_sqlite_path())
            try:
                conn.execute(sql, params)
                conn.commit()
                return True
            finally:
                conn.close()
        import psycopg2
        with _lock:
            if _pg is None or _pg.closed:
                _pg = psycopg2.connect(cfg.CHILLSTV_DB_URL)
                _pg.autocommit = True
            try:
                cur = _pg.cursor()
                cur.execute(sql.replace("?", "%s"), params)
                cur.close()
                return True
            except psycopg2.Error:
                try:
                    _pg.close()
                except Exception:
                    pass
                _pg = psycopg2.connect(cfg.CHILLSTV_DB_URL)
                _pg.autocommit = True
                cur = _pg.cursor()
                cur.execute(sql.replace("?", "%s"), params)
                cur.close()
                return True
    except Exception as e:
        print(f"[chillstv bridge] write failed: {type(e).__name__}: {e}")
        return False


def hash_password(password: str) -> str:
    """Same scheme ChillsTV mints: pbkdf2$iters$salt$hex."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def log_event(event: str, user_id=None, detail: dict = None):
    """One row in ChillsTV's events table, best effort, never raises."""
    try:
        _execute(
            "INSERT INTO events (user_id, pid, event, detail, created_at) VALUES (?,?,?,?,?)",
            (user_id, "", event, json.dumps(detail or {}), time.time()),
        )
    except Exception:
        pass


def create_account(email: str, password: str):
    """A fresh ChillsTV users row born in Edge, access granted.

    Mirrors ChillsTV's create_account_user plus grant_edge in one insert:
    token so every legacy flow works on the row, pbkdf2 password, beta
    granted with an edge code minted. Returns the users row, or None if
    the email is taken or the write failed.
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    if user_by_email(e):
        return None
    token = secrets.token_urlsafe(16)
    ok = _execute(
        "INSERT INTO users (token, email, password_hash, created_at, "
        "beta_status, edge_code, edge_granted_at) VALUES (?,?,?,?,?,?,?)",
        (token, e, hash_password(password), time.time(),
         "granted", secrets.token_urlsafe(6), time.time()),
    )
    if not ok:
        return None
    u = _one("SELECT * FROM users WHERE token = ?", (token,))
    if u:
        log_event("account_created", user_id=u.get("id"), detail={"method": "password", "via": "edge"})
    return u


def record_consent(user_id: int, terms_version: str, privacy_version: str, consent_boxes: dict) -> bool:
    """Stamp the notice acceptance, same fields ChillsTV's consent writes.

    Versions update to the latest accepted, the timestamp only stamps once.
    """
    if not user_id:
        return False
    ok = _execute(
        "UPDATE users SET terms_version=?, privacy_version=?, consent_boxes=? WHERE id=?",
        (terms_version or "", privacy_version or "", json.dumps(consent_boxes or {}), user_id),
    )
    if ok:
        _execute(
            "UPDATE users SET consented_at=? WHERE id=? AND consented_at IS NULL",
            (time.time(), user_id),
        )
        log_event("consented", user_id=user_id, detail={"via": "edge"})
    return ok
