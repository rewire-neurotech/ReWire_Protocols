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
  pregenerate_next_day(pid, day)      -> generate day+1 ahead (reflect route)
  recover_orphaned_protocol_jolts()   -> startup cleanup (call from app.main)
"""

import os
import re
import time
import shutil
import subprocess
import tempfile
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
from app.services.tts import synth, synth_meditation
from app.services.mix import mix as mix_audio
from app.services.mix_v45 import mix as mix_meditation_audio
from app.services.transition import attach_primer
from app.services.safety_log import log_safety_event_bg, compose_said
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

# Day 1 meditation music gain (Felix, Aug 2026): "voice could be less loud, or
# music louder, in jolt 1". mix_v45's default profile puts the music at
# DEFAULT_MUSIC_GAIN_DB (-10.0 dB). Passing this value on day 1 only makes the
# music 3 dB louder there; days 2-5 and journal jolts keep the default. Tune
# by ear: less negative = louder music.
DAY1_MUSIC_GAIN_DB = -7.0


def _count_spoken_words(text: str) -> int:
    """Count only the words a voice will speak, excluding audio tags and breaks."""
    stripped = _TAG_RE.sub("", text)
    stripped = stripped.replace("---", " ")
    return len(stripped.split())


# ADMIN CONSOLE: map the stage on a ProtocolUnsafe onto the console's layer.
#
#   llm.generate_protocol_speech / generate_journal_speech raise with
#     stage="speech"        -> the SPEECH prompt itself returned REWIRE_UNSAFE
#     stage="output_screen" -> the generated speech failed output screening
#                              after its retry cap
#
# These are the two catches that happen AFTER the input screen already said the
# goal was safe, which makes them the most informative rows in the review queue:
# each one is a case where L1 was too lenient and a later layer had to save it.
# Until now they only ever became a "blocked" stage on a jolt row -- the words
# and the reason were never recorded anywhere reviewable.
_STAGE_TO_LAYER = {
    "plan": "L2",
    "speech": "L2",
    "output_screen": "L3",
}


def _layer_for_stage(stage: str) -> str:
    return _STAGE_TO_LAYER.get((stage or "").strip().lower(), "L2")


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


def pregenerate_next_day(protocol_id: int, completed_day: int):
    """Generate the next day's meditation ahead of the user asking for it.

    Called by the reflect route the moment day N's reflection is saved: the
    recursive prompt needs that reflection, so this is the earliest point at
    which day N+1 can be written. Meditation protocols only (integrate and
    expand). Runs for every user regardless of entitlement (Ashwin, Aug
    2026): the paywall is enforced by the start route at listen time, never
    at generation time, so a pre generated file just waits encrypted on disk
    until the user unlocks it. Idempotent: if the next day already has a
    finished or in-flight jolt, nothing happens. Day 5's reflection triggers
    nothing, the protocol is complete. Never raises: pre generation failing
    must never break the reflection save, and a missing pre generated jolt
    just means the start route generates on demand exactly as before.
    """
    try:
        if completed_day >= int(cfg.PROTOCOL_DAYS):
            return
        next_day = completed_day + 1
        db = SessionLocal()
        try:
            p = db.query(Protocol).filter(Protocol.id == protocol_id).first()
            if not p or (p.type or "") not in ("integrate", "expand"):
                return
            existing = (db.query(ProtocolJolt)
                        .filter(ProtocolJolt.protocol_id == protocol_id,
                                ProtocolJolt.day == next_day,
                                ~ProtocolJolt.stage.in_(("error", "blocked")))
                        .first())
            if existing:
                return
            j = ProtocolJolt(protocol_id=protocol_id, day=next_day,
                             user_id=p.user_id, stage="queued", progress=10)
            db.add(j)
            db.commit()
            db.refresh(j)
            jid = j.id
        finally:
            db.close()
        print(f"[protojolt] pregenerating day {next_day} for protocol {protocol_id} (jolt {jid})")
        _pool.submit(_run_protocol_gen, jid)
    except Exception as e:
        print(f"[protojolt] pregenerate failed for protocol {protocol_id}: {e}")


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

        # Meditation history for days 2-5: every previous day's script with its
        # reflection, in day order, so the recursive prompt can read everything
        # that came before and never repeat it.
        history = []
        if (p.type or "") in ("integrate", "expand") and j.day > 1:
            prev_jolts = (db.query(ProtocolJolt)
                          .filter(ProtocolJolt.protocol_id == p.id,
                                  ProtocolJolt.day < j.day,
                                  ProtocolJolt.stage == "done")
                          .order_by(ProtocolJolt.day).all())
            for pj in prev_jolts:
                script = (decrypt_field(pj.speech_text) or "") if pj.speech_text else ""
                refl_row = (db.query(JournalEntry)
                            .filter(JournalEntry.protocol_id == p.id,
                                    JournalEntry.day == pj.day)
                            .order_by(JournalEntry.created_at.desc()).first())
                reflection = ""
                chills = None
                if refl_row:
                    if refl_row.answer:
                        reflection = decrypt_field(refl_row.answer) or ""
                    if refl_row.chills == "yes":
                        chills = True
                    elif refl_row.chills == "no":
                        chills = False
                history.append({
                    "day": pj.day,
                    "script": script,
                    "chills": chills,
                    "reflection": reflection,
                })

        # Generation guard input (Ashwin, Aug 2026): a meditation day above 1
        # must never be written without every prior day's questionnaire
        # answered (day 3 was once generated with an empty day 2 reflection
        # after the questionnaire was skipped). List the prior days that have
        # no reflection row at all; the worker refuses to generate if any.
        missing_reflections = []
        if (p.type or "") in ("integrate", "expand") and j.day > 1:
            refl_days = {row.day for row in
                         (db.query(JournalEntry.day)
                          .filter(JournalEntry.protocol_id == p.id,
                                  JournalEntry.day.isnot(None))
                          .all())}
            missing_reflections = [n for n in range(1, j.day) if n not in refl_days]

        return {
            "user_id": j.user_id,
            "protocol_id": p.id,
            "protocol_type": p.type or "activate",
            "place": p.place or "",
            "day": j.day,
            "target": p.target or "",
            "charge": (decrypt_field(p.charge) or "") if p.charge else "",
            "plan": plan,
            "completed_actions": completed,
            "yesterday_reflection": yref,
            "history": history,
            "missing_reflections": missing_reflections,
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

    # Meditation build (Aug 2026): integrate = regular meditation, expand =
    # place meditations. Both take the meditation pipeline. Activate keeps
    # the original path below, untouched.
    if ptype in ("integrate", "expand"):
        _run_meditation_gen(jolt_id, ctx)
        return

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
        _update(jolt_id, audio_filename=af,
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
        # ADMIN CONSOLE: record the flag. The jolt row already carries a
        # "blocked" stage, but that is a state for the frontend to poll, not a
        # reviewable record -- it holds no verbatim text and no reason, and it
        # is scoped to one day of one protocol.
        #
        # log_safety_event_bg opens its own short session on purpose: this
        # worker deliberately holds no DB session while it does the slow
        # speech / TTS / mix work, and it is running on a pool thread.
        log_safety_event_bg(
            user_id=ctx.get("user_id"),
            layer=_layer_for_stage(e.stage),
            source="protocol_jolt",
            verdict=(e.verdict or "unsafe"),
            category=(e.category or "other"),
            said=compose_said(ctx.get("target", ""), ctx.get("charge", "")),
            rationale=(e.detail or ""),
            protocol_id=ctx.get("protocol_id"),
            jolt_id=jolt_id,
        )

    except Exception as e:
        print(f"[protojolt] {jolt_id} error: {e}")
        _update(jolt_id, stage="error", gen_error=str(e))


# ===========================================================================
# MEDITATION PIPELINE (integrate + expand protocols, Aug 2026)
# ---------------------------------------------------------------------------
# Day 1: the theme's own prompt (regular / forest / ocean / fire) on the day 1
# model. Days 2-5: the recursive prompt fed with every previous day's script
# and reflection. No word-count drift correction: the prompts fix their own
# word counts. TTS inserts real silence for [pause] / [long pause]. The mix is
# mix_v45 with no retiming: the voice is padded with silence to the music
# length first, so the voice plays at natural pace, ends, and the music
# continues to the end of the track. The theme primer is attached in front on
# day 1 only; days 2-5 ship the bare mix (Ashwin, Aug 2026) and are normally
# pre generated at the prior day's reflect (pregenerate_next_day).
# ===========================================================================

def _music_duration_ms(music_path: str) -> int:
    """Music length in ms via ffprobe (cheap, no full decode). Falls back to
    a pydub decode if ffprobe is missing or fails."""
    try:
        ffprobe = "ffprobe"
        if "ffmpeg" in (cfg.FFMPEG_BIN or ""):
            ffprobe = cfg.FFMPEG_BIN.replace("ffmpeg", "ffprobe")
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", music_path],
            text=True, timeout=30,
        )
        return int(float(out.strip()) * 1000)
    except Exception:
        from pydub import AudioSegment
        return len(AudioSegment.from_file(music_path))


def _pad_voice_to_music(wav_path: str, music_path: str) -> str:
    """Append silence to the voice WAV so it matches the music length.

    Rewrites wav_path in place and returns it. When the voice is already the
    longer of the two, nothing changes and the mixer trims the music instead.

    WHY THIS MUST NOT FAIL SILENTLY (Felix's ocean cutoff, Aug 2026): the
    meditation mix runs mix_v45 in no_retime_trim_pad mode, where the final
    mix is exactly as long as the voice. If this pad fails, the mix is
    trimmed to the bare speech length and the meditation stops dead the
    moment the speech ends (ocean stopped at 1:47 instead of running the
    full 4:00 track). So: two independent pad routes, and a verification
    that logs the final voice length against the music length every run.
    """
    try:
        music_ms = _music_duration_ms(music_path)
    except Exception as e:
        print(f"[meditation] cannot read music duration, mixing unpadded: {e}")
        return wav_path

    # Route 1: pydub. Decodes the voice, appends silence, re-exports.
    try:
        from pydub import AudioSegment
        voice = AudioSegment.from_file(wav_path)
        gap = music_ms - len(voice)
        if gap > 0:
            padded = voice + AudioSegment.silent(duration=gap, frame_rate=voice.frame_rate)
            padded.export(wav_path, format="wav")
            print(f"[meditation] voice padded by {gap}ms to match music")
    except Exception as e:
        print(f"[meditation] pydub pad failed, trying raw wave pad: {e}")

    # Route 2 + verification: read the WAV with the stdlib wave module. If it
    # is still shorter than the music (route 1 failed or fell short), append
    # raw zero frames directly. No decode of the music, no pydub.
    try:
        with wave.open(wav_path, "rb") as r:
            p = r.getparams()
            voice_ms = int(1000 * p.nframes / p.framerate)
            frames = r.readframes(p.nframes) if voice_ms + 50 < music_ms else None
        if frames is not None:
            need_ms = music_ms - voice_ms
            extra = bytes(int(p.framerate * need_ms / 1000) * p.nchannels * p.sampwidth)
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(p.nchannels)
                w.setsampwidth(p.sampwidth)
                w.setframerate(p.framerate)
                w.writeframes(frames + extra)
            print(f"[meditation] raw wave pad added {need_ms}ms")
        with wave.open(wav_path, "rb") as r2:
            final_ms = int(1000 * r2.getnframes() / r2.getframerate())
        print(f"[meditation] pad check: voice {final_ms}ms, music {music_ms}ms")
        if final_ms + 50 < music_ms:
            print("[meditation] WARNING voice still shorter than music, mix will end early")
    except Exception as e:
        print(f"[meditation] raw wave pad failed: {e}")
    return wav_path


def _run_meditation_gen(jolt_id, ctx):
    theme = cfg.meditation_theme(ctx.get("place", ""))
    day = ctx["day"]

    # Generation guard (Ashwin, Aug 2026): defense in depth behind the
    # reflection based sequence gate in the start route. Whatever path
    # created this jolt row, a day above 1 refuses to generate unless every
    # prior day's questionnaire is answered, so a script can never again be
    # written from incomplete history. Pre generation always passes this by
    # construction, it only fires from a saved reflection.
    missing = ctx.get("missing_reflections") or []
    if missing:
        _update(jolt_id, stage="error",
                gen_error=f"previous day's reflection missing (day {missing[0]})")
        print(f"[meditation] {jolt_id} refused: day {day} without reflection for day(s) {missing}")
        return

    track = cfg.get_meditation_track(theme, day)
    track_name = track["file"].name

    t0 = time.time()
    try:
        # ---- DEV_MODE: silent 10s WAV, skip all API + mixing ----
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
            print(f"[meditation] {jolt_id} DEV_MODE done (silent audio)")
            return

        # ---- 1. Script (validated + output screened inside llm) ----
        _update(jolt_id, stage="generating", progress=20,
                track_name=track_name, voice_id=track["voice_id"])

        if day == 1:
            speech = llm.generate_meditation_day1(theme, ctx["target"], ctx["charge"])
        else:
            speech = llm.generate_meditation_later(
                ctx["target"], ctx["charge"], ctx.get("history") or []
            )

        print(f"[meditation] {jolt_id} script done: {len(speech.split())} words, theme {theme}, day {day}")
        _update(jolt_id, speech_text=encrypt_field(speech), screen_verdict="pass",
                stage="synthesizing", progress=40)

        # ---- 2. TTS with real inserted pauses ----
        wav = synth_meditation(
            speech, track["voice_id"], cfg.ELEVENLABS_API_KEY,
            voice_settings=track["voice_settings"],
            pause_ms=cfg.MEDITATION_PAUSE_MS,
            long_pause_ms=cfg.MEDITATION_LONG_PAUSE_MS,
        )
        _update(jolt_id, stage="mixing", progress=70)

        # ---- 3. Pad voice to music, mix with mix_v45. Day 1 gets the theme
        # primer attached in front (Felix's transition) into one complete
        # session; days 2-5 deliver the bare mix alone, no second induction
        # (Ashwin, Aug 2026) ----
        af = f"{_PREFIX}{jolt_id}.mp3"
        final_path = str(cfg.out_dir_path / af)
        bare_mix = None
        if day == 1:
            bare_fd, bare_mix = tempfile.mkstemp(prefix="rewire_bare_", suffix=".mp3")
            os.close(bare_fd)
        try:
            _pad_voice_to_music(wav, str(track["file"]))
            mix_meditation_audio(
                voice_path=wav, music_path=str(track["file"]),
                out_path=(bare_mix if day == 1 else final_path),
                ffmpeg_bin=cfg.FFMPEG_BIN,
                sync_mode="no_retime_trim_pad",
                # Day 1 only: louder music under the voice (Felix). None on
                # days 2-5 lets mix_v45 resolve its own profile default.
                music_premix_gain_db=(DAY1_MUSIC_GAIN_DB if day == 1 else None),
            )
            if day == 1:
                # Join primer + meditation the way Felix's 2_transition.py
                # does. Never raises: on any failure the bare mix is
                # delivered as is.
                attach_primer(theme, bare_mix, final_path)
            encrypt_file(final_path)
        finally:
            try:
                if bare_mix and os.path.exists(bare_mix):
                    os.remove(bare_mix)
            except Exception as e:
                print(f"[meditation] {jolt_id} bare mix cleanup failed: {e}")
            try:
                if wav and os.path.exists(wav):
                    os.remove(wav)
            except Exception as e:
                print(f"[meditation] {jolt_id} voice temp cleanup failed: {e}")

        elapsed = round(time.time() - t0, 1)
        _update(jolt_id, audio_filename=af,
                gen_time_sec=elapsed, stage="done", progress=100)
        print(f"[meditation] {jolt_id} done in {elapsed}s")
        # Push wording (Ashwin, Aug 2026): pre generated days complete while
        # the user is away, so the notification names the day it announces.
        _notify_user(ctx["user_id"], f"Jolt {day} is ready", "Put on headphones and press play.")

    except llm.ProtocolUnsafe as e:
        print(f"[meditation] {jolt_id} unsafe at {e.stage}: {e.verdict} {e.category}")
        _update(
            jolt_id,
            stage="blocked",
            gen_error=f"unsafe:{e.stage}:{e.verdict}",
            screen_verdict=(e.verdict if e.stage == "output_screen" else None),
            screen_category=(e.category or None),
        )
        log_safety_event_bg(
            user_id=ctx.get("user_id"),
            layer=_layer_for_stage(e.stage),
            source="protocol_jolt",
            verdict=(e.verdict or "unsafe"),
            category=(e.category or "other"),
            said=compose_said(ctx.get("target", ""), ctx.get("charge", "")),
            rationale=(e.detail or ""),
            protocol_id=ctx.get("protocol_id"),
            jolt_id=jolt_id,
        )

    except Exception as e:
        print(f"[meditation] {jolt_id} error: {e}")
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
        return {"user_id": j.user_id, "journal_entry_id": e.id, "entry_text": text}
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

    # Meditation build (Aug 2026): journal jolts run the recursive meditation
    # prompt (a dedicated journal prompt comes later) over a random track from
    # the regular meditation set, so the music varies between entries.
    track = cfg.get_meditation_track("regular", random.randint(1, cfg.PROTOCOL_DAYS))
    track_name = track["file"].name

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

        # ---- 1. Script (validated + output screened inside llm) ----
        _update_journal(jj_id, stage="generating", progress=20,
                        track_name=track_name, voice_id=track["voice_id"])

        speech = llm.generate_meditation_later(entry_text, "", [])

        print(f"[journaljolt] {jj_id} script done: {len(speech.split())} words")
        _update_journal(jj_id, speech_text=encrypt_field(speech), screen_verdict="pass",
                        stage="synthesizing", progress=40)

        # ---- 2. TTS with real inserted pauses ----
        wav = synth_meditation(
            speech, track["voice_id"], cfg.ELEVENLABS_API_KEY,
            voice_settings=track["voice_settings"],
            pause_ms=cfg.MEDITATION_PAUSE_MS,
            long_pause_ms=cfg.MEDITATION_LONG_PAUSE_MS,
        )
        _update_journal(jj_id, stage="mixing", progress=70)

        # ---- 3. Pad voice to music, mix with mix_v45, then attach the
        # regular primer (journal jolts always use the regular theme) ----
        bare_fd, bare_mix = tempfile.mkstemp(prefix="rewire_bare_", suffix=".mp3")
        os.close(bare_fd)
        try:
            _pad_voice_to_music(wav, str(track["file"]))
            af = f"{_PREFIX_JOURNAL}{jj_id}.mp3"
            mix_meditation_audio(
                voice_path=wav, music_path=str(track["file"]),
                out_path=bare_mix,
                ffmpeg_bin=cfg.FFMPEG_BIN,
                sync_mode="no_retime_trim_pad",
            )
            # Join primer + meditation the way Felix's 2_transition.py does.
            # Never raises: on any failure the bare mix is delivered as is.
            attach_primer("regular", bare_mix, str(cfg.out_dir_path / af))
            encrypt_file(str(cfg.out_dir_path / af))
        finally:
            try:
                if os.path.exists(bare_mix):
                    os.remove(bare_mix)
            except Exception as e:
                print(f"[journaljolt] {jj_id} bare mix cleanup failed: {e}")
            try:
                if wav and os.path.exists(wav):
                    os.remove(wav)
            except Exception as e:
                print(f"[journaljolt] {jj_id} voice temp cleanup failed: {e}")

        elapsed = round(time.time() - t0, 1)
        _update_journal(jj_id, audio_filename=af,
                        gen_time_sec=elapsed, stage="done", progress=100)
        print(f"[journaljolt] {jj_id} done in {elapsed}s")
        _notify_user(ctx["user_id"], "Your meditation is ready", "Put on headphones and press play.")

    except llm.ProtocolUnsafe as e:
        print(f"[journaljolt] {jj_id} unsafe at {e.stage}: {e.verdict} {e.category}")
        _update_journal(
            jj_id,
            stage="blocked",
            gen_error=f"unsafe:{e.stage}:{e.verdict}",
            screen_verdict=(e.verdict if e.stage == "output_screen" else None),
            screen_category=(e.category or None),
        )
        # ADMIN CONSOLE: same record as the protocol path above. Note this fires
        # on an entry the input screen had ALREADY cleared in
        # routes/protocol_jolt.py -- so the speech prompt or the output screen
        # caught something L1 let past.
        log_safety_event_bg(
            user_id=ctx.get("user_id"),
            layer=_layer_for_stage(e.stage),
            source="journal_jolt",
            verdict=(e.verdict or "unsafe"),
            category=(e.category or "other"),
            said=compose_said(entry_text, ""),
            rationale=(e.detail or ""),
            journal_entry_id=ctx.get("journal_entry_id"),
            jolt_id=jj_id,
        )

    except Exception as e:
        print(f"[journaljolt] {jj_id} error: {e}")
        _update_journal(jj_id, stage="error", gen_error=str(e))
