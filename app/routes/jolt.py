import json
import re
import time
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import cfg
from app.db import get_db, SessionLocal
from app.models import Goal, Challenge, Tip, Jolt, Reflection, User, Subscription, PushSubscription
from app.routes.auth import get_current_user_required
from app.services.prompt import build_user_prompt
from app.services.llm import generate_speech, SafetyHalt
from app.services.tts import synth
from app.services.mix import mix as mix_audio
from app.services.music_selector import select_track
from app.services import healthcheck
from app.utils.encryption import encrypt_field, decrypt_field, encrypt_file, decrypt_file_to_bytes

r = APIRouter(prefix="/api/jolt", tags=["jolt"])
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jolt")

# Regex to strip ElevenLabs audio tags like [whispers], [pause], [dramatic tone], etc.
_TAG_RE = re.compile(r"\[[^\]]*\]")


def _count_spoken_words(text: str) -> int:
    """Count only the words a voice will speak, excluding audio tags and section breaks."""
    stripped = _TAG_RE.sub("", text)
    stripped = stripped.replace("---", " ")
    return len(stripped.split())


class StartResp(BaseModel):
    status: str
    jolt_id: Optional[int] = None
    hc_status: Optional[str] = None
    hc_category: Optional[str] = None
    user_message: Optional[str] = None

class StatusResp(BaseModel):
    jolt_id: int
    stage: str
    progress: int
    audio_url: Optional[str] = None
    error: Optional[str] = None

class ReflectReq(BaseModel):
    question: str = ""
    answer: str = ""

class Ok(BaseModel):
    status: str


def _update(jolt_id, **kw):
    db = SessionLocal()
    try:
        j = db.query(Jolt).filter(Jolt.id == jolt_id).first()
        if j:
            for k, v in kw.items():
                setattr(j, k, v)
            db.commit()
    except Exception as e:
        print(f"[jolt] db update error: {e}")
        try: db.rollback()
        except: pass
    finally:
        db.close()


def _has_sub(uid, db):
    s = (db.query(Subscription)
         .filter(Subscription.user_id == uid, Subscription.status == "active")
         .first())
    if not s: return False
    if s.expires_at and s.expires_at < datetime.now(timezone.utc):
        s.status = "expired"
        db.commit()
        return False
    return True


def _notify_user(user_id, title, body):
    """Send a push notification to all of a user's subscribed devices."""
    if not cfg.VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
        db = SessionLocal()
        try:
            subs = db.query(PushSubscription).filter(
                PushSubscription.user_id == user_id
            ).all()
            for s in subs:
                try:
                    webpush(
                        subscription_info={
                            "endpoint": s.endpoint,
                            "keys": {"p256dh": s.p256dh, "auth": s.auth},
                        },
                        data=json.dumps({"title": title, "body": body}),
                        vapid_private_key=cfg.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": cfg.VAPID_CONTACT},
                    )
                except WebPushException as e:
                    print(f"[push] failed for sub {s.id}: {e}")
                    if "410" in str(e) or "404" in str(e):
                        db.delete(s)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"[push] notification error: {e}")


def _run_gen(jolt_id, user_id, title, why, challenges, tips, reflection, track_name):
    try:
        t0 = time.time()
        t = cfg.get_track(track_name)

        _update(jolt_id, stage="generating", progress=20)
        prompt = build_user_prompt(
            goal_title=title, goal_why=why,
            challenges=challenges, tips=tips,
            reflection=reflection, target_words=t["target_words"],
        )
        speech = generate_speech(prompt)
        spoken = _count_spoken_words(speech)
        target = t["target_words"]
        drift = abs(spoken - target) / target

        # If spoken words are more than 12% off target, retry once
        if drift > 0.12:
            direction = "fewer" if spoken > target else "more"
            print(
                f"[jolt] {jolt_id} word count drift: {spoken} spoken vs "
                f"{target} target ({drift:.0%}). Retrying with correction."
            )
            retry_prompt = (
                prompt
                + f"\n\nCORRECTION: Your previous attempt had {spoken} spoken words "
                f"but the target is exactly {target}. Write {direction} words this time. "
                f"Count carefully before outputting."
            )
            speech2 = generate_speech(retry_prompt)
            spoken2 = _count_spoken_words(speech2)
            drift2 = abs(spoken2 - target) / target
            # Use whichever attempt is closer to target
            if drift2 < drift:
                speech = speech2
                spoken = spoken2
                drift = drift2
                print(f"[jolt] {jolt_id} retry improved: {spoken} spoken words ({drift:.0%} drift)")
            else:
                print(f"[jolt] {jolt_id} retry did not improve ({spoken2} words), keeping original")

        _update(jolt_id, speech_text=encrypt_field(speech),
                stage="synthesizing", progress=40)
        print(f"[jolt] {jolt_id} speech done, {spoken} spoken words (target {target}, drift {drift:.0%})")

        wav = synth(speech, t["voice_id"], cfg.ELEVENLABS_API_KEY,
                    voice_settings=t["voice_settings"])
        _update(jolt_id, stage="mixing", progress=70)

        vf = f"{jolt_id}_voice.wav"
        shutil.copy2(wav, str(cfg.out_dir_path / vf))
        encrypt_file(str(cfg.out_dir_path / vf))

        af = f"{jolt_id}.mp3"
        mix_audio(
            voice_path=wav, music_path=str(t["file"]), out_path=str(cfg.out_dir_path / af),
            ffmpeg_bin=cfg.FFMPEG_BIN,
            voice_target_dbfs=-12.5, music_target_dbfs=-24.0,
            duck_db=5.0, content_duration_sec=t["content_duration_sec"],
        )
        encrypt_file(str(cfg.out_dir_path / af))

        elapsed = round(time.time() - t0, 1)
        _update(jolt_id, audio_filename=af, voice_filename=vf,
                voice_id=t["voice_id"], gen_time_sec=elapsed,
                stage="done", progress=100)
        print(f"[jolt] {jolt_id} done in {elapsed}s")
        _notify_user(user_id, "Your Jolt is ready", "Put on headphones and press play.")

    except SafetyHalt as e:
        print(f"[jolt] {jolt_id} SAFETY_HALT: {e.reason}")
        _update(jolt_id, stage="error",
                gen_error=f"SAFETY_HALT: {e.reason}",
                hc_status="block", hc_category="safety_halt")

    except Exception as e:
        print(f"[jolt] {jolt_id} error: {e}")
        _update(jolt_id, stage="error", gen_error=str(e))


def _gather(g, db):
    cs = db.query(Challenge).filter(Challenge.goal_id == g.id).all()
    ts = db.query(Tip).filter(Tip.goal_id == g.id).all()
    last_ref = (db.query(Reflection).filter(Reflection.goal_id == g.id)
                .order_by(Reflection.created_at.desc()).first())
    return {
        "title": g.title,
        "why": decrypt_field(g.description) or "",
        "challenges": [decrypt_field(c.text) or "" for c in cs],
        "tips": [decrypt_field(t.text) or "" for t in ts],
        "reflection": decrypt_field(last_ref.answer) if last_ref and last_ref.answer else "",
    }


@r.post("/{gid}/start", response_model=StartResp)
def start_jolt(gid: int, skip_hc: bool = False,
               u: User = Depends(get_current_user_required),
               db: Session = Depends(get_db)):
    g = db.query(Goal).filter(Goal.id == gid, Goal.user_id == u.id).first()
    if not g:
        raise HTTPException(404, "goal not found")

    # first 3 jolts are free, subsequent need subscription
    total_done = db.query(Jolt).filter(
        Jolt.user_id == u.id, Jolt.stage == "done"
    ).count()
    if total_done >= 3 and not _has_sub(u.id, db):
        raise HTTPException(402, "subscription required for additional jolts")

    data = _gather(g, db)

    # health check (skip after reframe acceptance)
    if not skip_hc:
        v = healthcheck.run(data["title"], data["why"],
                            data["challenges"], data["tips"], data["reflection"])
        st = v.get("status", "block")
        if st != "allow":
            j = Jolt(goal_id=gid, user_id=u.id,
                     hc_status=st, hc_category=v.get("category"),
                     stage="blocked", progress=0)
            db.add(j)
            db.commit()
            return StartResp(
                status=st, jolt_id=j.id,
                hc_status=st, hc_category=v.get("category"),
                user_message=v.get("user_message", ""),
            )

    # assign track based on user's total jolt count
    track_name = select_track(u.total_jolt_count)

    j = Jolt(goal_id=gid, user_id=u.id, track_name=track_name,
             hc_status="allow", stage="queued", progress=10)
    db.add(j)

    today = date.today()
    if u.last_jolt_date != today:
        u.jolt_count_today = 0
    u.total_jolt_count += 1
    u.jolt_count_today += 1
    u.last_jolt_date = today

    db.commit()
    db.refresh(j)

    _pool.submit(_run_gen, j.id, u.id,
                 data["title"], data["why"], data["challenges"],
                 data["tips"], data["reflection"], track_name)

    return StartResp(status="generating", jolt_id=j.id)


@r.get("/audio/{fname}")
def serve_audio(fname: str):
    fp = cfg.out_dir_path / fname
    if not fp.exists():
        raise HTTPException(404, "audio not found")
    data = decrypt_file_to_bytes(str(fp))
    mt = "audio/mpeg" if fname.endswith(".mp3") else "audio/wav"
    return Response(content=data, media_type=mt,
                    headers={"Content-Disposition": f"inline; filename={fname}",
                             "Accept-Ranges": "bytes"})


@r.get("/{jid}/status", response_model=StatusResp)
def get_status(jid: int, u: User = Depends(get_current_user_required),
               db: Session = Depends(get_db)):
    j = db.query(Jolt).filter(Jolt.id == jid, Jolt.user_id == u.id).first()
    if not j:
        raise HTTPException(404, "jolt not found")

    url = None
    if j.stage == "done" and j.audio_filename:
        base = cfg.PUBLIC_BASE_URL.rstrip("/") if cfg.PUBLIC_BASE_URL else ""
        url = f"{base}/api/jolt/audio/{j.audio_filename}"

    return StatusResp(jolt_id=j.id, stage=j.stage, progress=j.progress,
                      audio_url=url, error=j.gen_error)


@r.post("/{jid}/reflect", response_model=Ok)
def save_reflection(jid: int, req: ReflectReq,
                    u: User = Depends(get_current_user_required),
                    db: Session = Depends(get_db)):
    j = db.query(Jolt).filter(Jolt.id == jid, Jolt.user_id == u.id).first()
    if not j:
        raise HTTPException(404, "jolt not found")
    db.add(Reflection(
        jolt_id=j.id, goal_id=j.goal_id, user_id=u.id,
        question=req.question,
        answer=encrypt_field(req.answer) if req.answer else None,
    ))
    db.commit()
    return Ok(status="ok")