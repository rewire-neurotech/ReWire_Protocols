"""
Protocol jolt generation (in-process background worker).

Same mechanism as v4's jolt.py: a module-level ThreadPoolExecutor runs
generation off the request thread, writing encrypted audio to the local disk
(OUT_DIR). No Redis, no separate worker process, no object storage - identical
infrastructure to v4.

What it adds on top of v4's _run_gen is the v5 shape: a protocol is generated
one day at a time (lazily), so each job is a single ProtocolJolt row. The audio
pipeline (tts -> mix -> encrypt) and the mix tuning constants are unchanged from
v4.

Public entrypoints:
  submit_generation(jolt_id)          -> queue a ProtocolJolt for generation
  recover_orphaned_protocol_jolts()   -> startup cleanup (call from app.main)
"""

import os
import re
import time
import shutil
import wave
import struct
import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.core.config import cfg
from app.db import SessionLocal
from app.models import Protocol, ProtocolDay, ProtocolJolt, JournalEntry, JournalJolt, PushSubscription
from app.services import llm
from app.services.tts import synth
from app.services.mix import mix as mix_audio
from app.utils.encryption import encrypt_field, decrypt_field, encrypt_file


# In-process pool. Worker count is the concurrency cap (each job runs one heavy
# ffmpeg mix). Tunable via MAX_CONCURRENT_MIXES; v4 used 8. Sized conservatively
# because v5 generates lazily per day, so fewer full runs overlap.
_pool = ThreadPoolExecutor(
    max_workers=max(1, int(getattr(cfg, "MAX_CONCURRENT_MIXES", 2))),
    thread_name_prefix="protojolt",
)

# Strip ElevenLabs audio tags like [whispers], [pause], [dramatic tone].
_TAG_RE = re.compile(r"\[[^\]]*\]")

# Stages that never change again on their own.
_TERMINAL_STAGES = ("done", "error", "blocked")

# Audio filename prefix so v5 files never collide with v4 jolt files ("{id}.mp3")
# sharing the same OUT_DIR. ProtocolJolt id 5 -> "pj5.mp3".
_PREFIX = "pj"
_PREFIX_JOURNAL = "jj"   # standalone journal jolt: JournalJolt id 5 -> "jj5.mp3"


def _count_spoken_words(text: str) -> int:
    """Count only the words a voice will speak, excluding audio tags and breaks."""
    stripped = _TAG_RE.sub("", text)
    stripped = stripped.replace("---", " ")
    return len(stripped.split())


def _update(jolt_id, **kw):
    db = SessionLocal()
    try:
        j = db.query(ProtocolJolt).filter(ProtocolJolt.id == jolt_id).first()
        if j:
            for k, v in kw.items():
                setattr(j, k, v)
            db.commit()
    except Exception as e:
        print(f"[protojolt] db update error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _notify_user(user_id, title, body):
    """Send a push notification to all of a user's subscribed devices (v4 logic)."""
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


def recover_orphaned_protocol_jolts():
    """
    Called once at startup (from app.main), mirrors v4's recover_orphaned_jolts.

    1. Remove leftover per-mix scratch dirs from /tmp (survive a hard OOM kill).
    2. Mark any ProtocolJolt stuck in a non-terminal stage as error. In-flight
       generation runs in this process's pool, so anything non-terminal at boot
       was orphaned by the previous shutdown.
    """
    try:
        import glob
        import tempfile as _tf
        swept = 0
        for d in glob.glob(os.path.join(_tf.gettempdir(), "rewire_mix_*")):
            shutil.rmtree(d, ignore_errors=True)
            swept += 1
        if swept:
            print(f"[protojolt] startup: removed {swept} leftover scratch dir(s)")
    except Exception as e:
        print(f"[protojolt] startup scratch sweep error: {e}")

    if SessionLocal is None:
        return
    db = SessionLocal()
    try:
        stuck = db.query(ProtocolJolt).filter(~ProtocolJolt.stage.in_(_TERMINAL_STAGES)).all()
        for j in stuck:
            j.stage = "error"
            j.gen_error = "interrupted by server restart"
        if stuck:
            db.commit()
            print(f"[protojolt] startup: recovered {len(stuck)} orphaned jolt(s)")
        jstuck = db.query(JournalJolt).filter(~JournalJolt.stage.in_(_TERMINAL_STAGES)).all()
        for j in jstuck:
            j.stage = "error"
            j.gen_error = "interrupted by server restart"
        if jstuck:
            db.commit()
            print(f"[journaljolt] startup: recovered {len(jstuck)} orphaned jolt(s)")
    except Exception as e:
        print(f"[protojolt] startup orphan recovery error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def submit_generation(jolt_id: int):
    """Queue a ProtocolJolt row for background generation (in-process pool)."""
    _pool.submit(_run_protocol_gen, jolt_id)


def _load_job(jolt_id):
    """Read everything the generation needs from the DB, decrypting sensitive
    fields, and return a plain dict (so the worker holds no DB session while it
    does the slow speech/tts/mix work). Returns None if the row is gone.
    """
    db = SessionLocal()
    try:
        j = db.query(ProtocolJolt).filter(ProtocolJolt.id == jolt_id).first()
        if not j:
            return None
        p = db.query(Protocol).filter(Protocol.id == j.protocol_id).first()
        if not p:
            return {"missing_protocol": True, "user_id": j.user_id}

        day_rows = (db.query(ProtocolDay)
                    .filter(ProtocolDay.protocol_id == p.id)
                    .order_by(ProtocolDay.day).all())
        plan = {"days": [
            {"day": d.day, "stage": d.stage, "action": d.action or "", "brief": d.brief or ""}
            for d in day_rows
        ]}
        completed = [d.action for d in day_rows if d.done and d.action]

        yref = ""
        if j.day > 1:
            last_ref = (db.query(JournalEntry)
                        .filter(JournalEntry.protocol_id == p.id)
                        .order_by(JournalEntry.created_at.desc()).first())
            if last_ref and last_ref.answer:
                yref = decrypt_field(last_ref.answer) or ""

        return {
            "user_id": j.user_id,
            "protocol_type": p.type or "activate",
            "day": j.day,
            "target": p.target or "",
            "charge": (decrypt_field(p.charge) or "") if p.charge else "",
            "plan": plan,
            "completed_actions": completed,
            "yesterday_reflection": yref,
        }
    finally:
        db.close()


def _run_protocol_gen(jolt_id):
    ctx = _load_job(jolt_id)
    if ctx is None:
        print(f"[protojolt] {jolt_id} row gone, skipping")
        return
    if ctx.get("missing_protocol"):
        _update(jolt_id, stage="error", gen_error="protocol not found")
        return

    ptype = ctx["protocol_type"]
    day = ctx["day"]
    track = cfg.get_protocol_track(ptype, day)
    track_name = track["file"].name

    t0 = time.time()
    try:
        # ---- DEV_MODE: silent 10s WAV, skip all API + mixing (v4 parity) ----
        if cfg.DEV_MODE:
            _update(jolt_id, stage="generating", progress=20,
                    track_name=track_name, voice_id=track["voice_id"])
            af = f"{_PREFIX}{jolt_id}.wav"
            out_path = str(cfg.out_dir_path / af)
            with wave.open(out_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(struct.pack("<" + "h" * 441000, *([0] * 441000)))
            _update(jolt_id, stage="mixing", progress=70)
            _update(jolt_id, audio_filename=af, stage="done", progress=100,
                    gen_time_sec=round(time.time() - t0, 1))
            print(f"[protojolt] {jolt_id} DEV_MODE done (silent audio)")
            return

        # ---- 1. Speech (screened inside generate_protocol_speech) ----
        _update(jolt_id, stage="generating", progress=20,
                track_name=track_name, voice_id=track["voice_id"])

        speech = llm.generate_protocol_speech(
            ptype, day, ctx["target"], ctx["charge"], ctx["plan"],
            completed_actions=ctx["completed_actions"],
            yesterday_reflection=ctx["yesterday_reflection"],
        )

        # ---- Word-count drift correction (v4 parity: retime still locks the
        # ending, this keeps the voice from sounding stretched) ----
        target_words = track["target_words"]
        spoken = _count_spoken_words(speech)
        drift = abs(spoken - target_words) / target_words
        if drift > 0.12:
            direction = "fewer" if spoken > target_words else "more"
            correction = (
                f"CORRECTION: your previous attempt had {spoken} spoken words but the "
                f"target is exactly {target_words}. Write {direction} words this time. "
                f"Count carefully before outputting."
            )
            try:
                speech2 = llm.generate_protocol_speech(
                    ptype, day, ctx["target"], ctx["charge"], ctx["plan"],
                    completed_actions=ctx["completed_actions"],
                    yesterday_reflection=ctx["yesterday_reflection"],
                    correction=correction,
                )
                spoken2 = _count_spoken_words(speech2)
                drift2 = abs(spoken2 - target_words) / target_words
                if drift2 < drift:
                    speech, spoken, drift = speech2, spoken2, drift2
                    print(f"[protojolt] {jolt_id} drift retry improved: {spoken} words ({drift:.0%})")
                else:
                    print(f"[protojolt] {jolt_id} drift retry no better ({spoken2} words), keeping first")
            except llm.ProtocolUnsafe:
                print(f"[protojolt] {jolt_id} drift retry came back unsafe, keeping first speech")

        print(f"[protojolt] {jolt_id} speech done: {spoken} spoken words (target {target_words}, drift {drift:.0%})")
        _update(jolt_id, speech_text=encrypt_field(speech), screen_verdict="pass",
                stage="synthesizing", progress=40)

        # ---- 2. TTS ----
        wav = synth(speech, track["voice_id"], cfg.ELEVENLABS_API_KEY,
                    voice_settings=track["voice_settings"])
        _update(jolt_id, stage="mixing", progress=70)

        # ---- 3. Mix + encrypt (mix tuning identical to v4) ----
        try:
            vf = f"{_PREFIX}{jolt_id}_voice.wav"
            shutil.copy2(wav, str(cfg.out_dir_path / vf))
            encrypt_file(str(cfg.out_dir_path / vf))

            af = f"{_PREFIX}{jolt_id}.mp3"
            mix_audio(
                voice_path=wav, music_path=str(track["file"]),
                out_path=str(cfg.out_dir_path / af),
                ffmpeg_bin=cfg.FFMPEG_BIN,
                voice_target_dbfs=-12.5, music_target_dbfs=-24.0,
                duck_db=5.0, content_duration_sec=track["content_duration_sec"],
            )
            encrypt_file(str(cfg.out_dir_path / af))
        finally:
            try:
                if wav and os.path.exists(wav):
                    os.remove(wav)
            except Exception as e:
                print(f"[protojolt] {jolt_id} voice temp cleanup failed: {e}")

        elapsed = round(time.time() - t0, 1)
        _update(jolt_id, audio_filename=af, voice_filename=vf,
                gen_time_sec=elapsed, stage="done", progress=100)
        print(f"[protojolt] {jolt_id} done in {elapsed}s")
        _notify_user(ctx["user_id"], "Your jolt is ready", "Put on headphones and press play.")

    except llm.ProtocolUnsafe as e:
        print(f"[protojolt] {jolt_id} unsafe at {e.stage}: {e.verdict} {e.category}")
        _update(
            jolt_id,
            stage="blocked",
            gen_error=f"unsafe:{e.stage}:{e.verdict}",
            screen_verdict=(e.verdict if e.stage == "output_screen" else None),
            screen_category=(e.category or None),
        )

    except Exception as e:
        print(f"[protojolt] {jolt_id} error: {e}")
        _update(jolt_id, stage="error", gen_error=str(e))


# ===========================================================================
# JOURNAL JOLTS: a standalone jolt generated from a single journal entry.
# Same pipeline as a protocol jolt (speech -> tts -> mix -> encrypt), but seeded
# by the entry text and rendered over one fixed 2-minute track. Rows live in the
# journal_jolts table and never touch the protocol day machinery.
# ===========================================================================

def submit_journal_generation(jj_id: int):
    """Queue a JournalJolt row for background generation (in-process pool)."""
    _pool.submit(_run_journal_gen, jj_id)


def _update_journal(jj_id, **kw):
    db = SessionLocal()
    try:
        j = db.query(JournalJolt).filter(JournalJolt.id == jj_id).first()
        if j:
            for k, v in kw.items():
                setattr(j, k, v)
            db.commit()
    except Exception as e:
        print(f"[journaljolt] db update error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _load_journal_job(jj_id):
    """Read the entry text the generation needs (decrypted) and return a plain
    dict, so the worker holds no DB session during the slow speech/tts/mix work.
    Returns None if the JournalJolt row is gone."""
    db = SessionLocal()
    try:
        j = db.query(JournalJolt).filter(JournalJolt.id == jj_id).first()
        if not j:
            return None
        e = db.query(JournalEntry).filter(JournalEntry.id == j.journal_entry_id).first()
        if not e:
            return {"missing_entry": True, "user_id": j.user_id}
        text = (decrypt_field(e.answer) or "") if e.answer else ""
        return {"user_id": j.user_id, "entry_text": text}
    finally:
        db.close()


def _run_journal_gen(jj_id):
    ctx = _load_journal_job(jj_id)
    if ctx is None:
        print(f"[journaljolt] {jj_id} row gone, skipping")
        return
    if ctx.get("missing_entry"):
        _update_journal(jj_id, stage="error", gen_error="journal entry not found")
        return
    entry_text = (ctx["entry_text"] or "").strip()
    if not entry_text:
        _update_journal(jj_id, stage="error", gen_error="empty journal entry")
        return

    # Journal jolts pick a random one of the 5 Sacred protocol tracks, so the
    # music varies between entries.
    track = cfg.get_protocol_track("activate", random.randint(1, cfg.PROTOCOL_DAYS))
    track_name = track["file"].name
    target_words = track["target_words"]

    t0 = time.time()
    try:
        # ---- DEV_MODE: silent 10s WAV, skip all API + mixing ----
        if cfg.DEV_MODE:
            _update_journal(jj_id, stage="generating", progress=20,
                            track_name=track_name, voice_id=track["voice_id"])
            af = f"{_PREFIX_JOURNAL}{jj_id}.wav"
            out_path = str(cfg.out_dir_path / af)
            with wave.open(out_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(struct.pack("<" + "h" * 441000, *([0] * 441000)))
            _update_journal(jj_id, stage="mixing", progress=70)
            _update_journal(jj_id, audio_filename=af, stage="done", progress=100,
                            gen_time_sec=round(time.time() - t0, 1))
            print(f"[journaljolt] {jj_id} DEV_MODE done (silent audio)")
            return

        # ---- 1. Speech (screened inside generate_journal_speech) ----
        _update_journal(jj_id, stage="generating", progress=20,
                        track_name=track_name, voice_id=track["voice_id"])

        speech = llm.generate_journal_speech(entry_text, target_words)

        # ---- Word-count drift correction (keeps the voice from sounding
        # stretched; the mix locks the ending regardless) ----
        spoken = _count_spoken_words(speech)
        drift = abs(spoken - target_words) / target_words
        if drift > 0.12:
            try:
                speech2 = llm.generate_journal_speech(entry_text, target_words)
                spoken2 = _count_spoken_words(speech2)
                drift2 = abs(spoken2 - target_words) / target_words
                if drift2 < drift:
                    speech, spoken, drift = speech2, spoken2, drift2
            except llm.ProtocolUnsafe:
                print(f"[journaljolt] {jj_id} drift retry came back unsafe, keeping first speech")

        print(f"[journaljolt] {jj_id} speech done: {spoken} spoken words (target {target_words}, drift {drift:.0%})")
        _update_journal(jj_id, speech_text=encrypt_field(speech), screen_verdict="pass",
                        stage="synthesizing", progress=40)

        # ---- 2. TTS ----
        wav = synth(speech, track["voice_id"], cfg.ELEVENLABS_API_KEY,
                    voice_settings=track["voice_settings"])
        _update_journal(jj_id, stage="mixing", progress=70)

        # ---- 3. Mix + encrypt (identical tuning to protocol jolts) ----
        try:
            vf = f"{_PREFIX_JOURNAL}{jj_id}_voice.wav"
            shutil.copy2(wav, str(cfg.out_dir_path / vf))
            encrypt_file(str(cfg.out_dir_path / vf))

            af = f"{_PREFIX_JOURNAL}{jj_id}.mp3"
            mix_audio(
                voice_path=wav, music_path=str(track["file"]),
                out_path=str(cfg.out_dir_path / af),
                ffmpeg_bin=cfg.FFMPEG_BIN,
                voice_target_dbfs=-12.5, music_target_dbfs=-24.0,
                duck_db=5.0, content_duration_sec=track["content_duration_sec"],
            )
            encrypt_file(str(cfg.out_dir_path / af))
        finally:
            try:
                if wav and os.path.exists(wav):
                    os.remove(wav)
            except Exception as e:
                print(f"[journaljolt] {jj_id} voice temp cleanup failed: {e}")

        elapsed = round(time.time() - t0, 1)
        _update_journal(jj_id, audio_filename=af, voice_filename=vf,
                        gen_time_sec=elapsed, stage="done", progress=100)
        print(f"[journaljolt] {jj_id} done in {elapsed}s")
        _notify_user(ctx["user_id"], "Your journal jolt is ready", "Put on headphones and press play.")

    except llm.ProtocolUnsafe as e:
        print(f"[journaljolt] {jj_id} unsafe at {e.stage}: {e.verdict} {e.category}")
        _update_journal(
            jj_id,
            stage="blocked",
            gen_error=f"unsafe:{e.stage}:{e.verdict}",
            screen_verdict=(e.verdict if e.stage == "output_screen" else None),
            screen_category=(e.category or None),
        )

    except Exception as e:
        print(f"[journaljolt] {jj_id} error: {e}")
        _update_journal(jj_id, stage="error", gen_error=str(e))