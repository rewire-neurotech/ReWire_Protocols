import io
import re
import time
import tempfile
from typing import List, Dict, Optional

import requests
import numpy as np
import noisereduce as nr
from pydub import AudioSegment
from requests.exceptions import ConnectionError, Timeout, RequestException

# Split on sentence boundaries so each chunk stays under ElevenLabs' input cap
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[\.\!\?])\s+')
_PAUSE_TOKEN = "[pause]"
_PAUSE_SENTINEL = "<<<PAUSE>>>"

# Meditation pause markers. [long pause] must be handled before [pause].
_LONG_PAUSE_TOKEN = "[long pause]"
_LONG_PAUSE_SENTINEL = "<<<LONGPAUSE>>>"
# ElevenLabs break tags like <break time="3.0s"/>. v3 drops them, so we
# convert them to real inserted silence instead.
_BREAK_TAG_RE = re.compile(r'<break\s+time="([0-9]*\.?[0-9]+)s?"\s*/?>', re.IGNORECASE)
# Any other bracket tag left in a meditation script (audio tags are not
# used in meditation, style is 0.0) gets removed so it is never spoken.
_BRACKET_TAG_RE = re.compile(r"\[[^\[\]]{1,40}\]")

# ElevenLabs sometimes gets unstable with huge chunks.
# 4600 is near the upper limit - we can be a bit more conservative.
DEFAULT_MAX_CHARS = 3200

# Default voice settings (used if none provided)
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.35,
    "similarity_boost": 0.7,
    "style": 0.85,
    "use_speaker_boost": True,
}


def _split_text_into_chunks(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """
    Split the script into <= max_chars chunks on sentence boundaries.
    Long single sentences are hard-split if needed.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    sentences = _SENTENCE_SPLIT_RE.split(text)
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        add_len = len(s) + (1 if cur_len > 0 else 0)
        if cur_len + add_len <= max_chars:
            cur.append(s)
            cur_len += add_len
        else:
            if cur:
                chunks.append(" ".join(cur))
            if len(s) > max_chars:
                # Hard-split extremely long sentences
                for i in range(0, len(s), max_chars):
                    chunks.append(s[i:i + max_chars])
                cur, cur_len = [], 0
            else:
                cur, cur_len = [s], len(s)

    if cur:
        chunks.append(" ".join(cur))
    return chunks


def _synth_chunk(
    text: str,
    voice_id: str,
    key: str,
    *,
    voice_settings: Optional[Dict] = None,
    timeout: int = 120,
    max_retries: int = 3,
    backoff_base: float = 1.5,
) -> AudioSegment:
    """
    Synthesize a single chunk and return it as a pydub AudioSegment.

    Adds retry logic around transient network / server issues:
    - ConnectionError
    - Timeout
    - HTTP 5xx
    - HTTP 429 (rate limiting)
    """
    settings = voice_settings if voice_settings is not None else DEFAULT_VOICE_SETTINGS

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": key,
        "accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        # [pause]/[breath] are stripped or pre-processed BEFORE this stage.
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": {
            "stability": settings.get("stability", 0.35),
            "similarity_boost": settings.get("similarity_boost", 0.7),
            "style": settings.get("style", 0.85),
            "use_speaker_boost": settings.get("use_speaker_boost", True),
        },
    }

    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            # You *can* experiment with stream=False if streaming is flaky:
            # r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            r = requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=timeout,
            )

            # Handle HTTP-level issues explicitly
            if r.status_code in (429, 500, 502, 503, 504):
                # These are typically transient / rate-limit / server errors.
                last_exc = RequestException(
                    f"ElevenLabs HTTP {r.status_code} while synthesising chunk."
                )
                # Only retry if we have attempts left
                if attempt < max_retries:
                    sleep_s = backoff_base ** attempt
                    print(
                        f"[TTS] Transient HTTP error {r.status_code}. "
                        f"Retrying attempt {attempt}/{max_retries} after {sleep_s:.1f}s..."
                    )
                    time.sleep(sleep_s)
                    continue
                else:
                    r.raise_for_status()

            # If non-200 and not in the retry list, this will raise HTTPError
            r.raise_for_status()

            # If we get here, we have a good response; stream into buffer
            buf = io.BytesIO()
            for c in r.iter_content(16384):
                if c:
                    buf.write(c)
            buf.seek(0)
            return AudioSegment.from_file(buf, format="mp3")

        except (ConnectionError, Timeout) as e:
            # This is where your original "RemoteDisconnected" lives
            last_exc = e
            if attempt < max_retries:
                sleep_s = backoff_base ** attempt
                print(
                    f"[TTS] Network issue ({type(e).__name__}: {e}). "
                    f"Retrying attempt {attempt}/{max_retries} after {sleep_s:.1f}s..."
                )
                time.sleep(sleep_s)
                continue
            else:
                print(
                    f"[TTS] Network issue after {max_retries} attempts. "
                    f"Giving up on this chunk."
                )
                raise

        except RequestException as e:
            # Any other requests-related errors (including HTTPError from raise_for_status)
            last_exc = e
            if attempt < max_retries:
                sleep_s = backoff_base ** attempt
                print(
                    f"[TTS] RequestException ({type(e).__name__}: {e}). "
                    f"Retrying attempt {attempt}/{max_retries} after {sleep_s:.1f}s..."
                )
                time.sleep(sleep_s)
                continue
            else:
                print(
                    f"[TTS] RequestException after {max_retries} attempts. "
                    f"Giving up on this chunk."
                )
                raise

    # In theory we should never get here because we either return or raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown TTS error; no exception captured but chunk failed.")


def synth(
    text: str,
    voice_id: str,
    key: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    voice_settings: Optional[Dict] = None,
) -> str:
    """
    Chunk long scripts, synth each chunk, stitch, and return a temp WAV path.

    We treat "[pause]" as a request for a slightly longer-than-normal silence
    by inserting explicit silent gaps between blocks.

    Additionally, we insert short gaps between synthesized chunks so the flow
    feels less like continuous talking and more like natural phrasing.

    voice_settings: per-voice ElevenLabs settings dict with keys
        stability, similarity_boost, style, use_speaker_boost.
        If None, uses DEFAULT_VOICE_SETTINGS.
    """
    raw = (text or "").strip()
    if not raw:
        # Return a 1s silent WAV if something odd happens
        silent = AudioSegment.silent(duration=1000, frame_rate=44100)
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        f.close()
        silent.export(f.name, format="wav")
        return f.name

    # Convert [breath] to nothing (small prosody change is fine)
    raw = raw.replace("[breath]", " ")

    # Mark pauses with a sentinel so we can split and inject silence
    raw = raw.replace(_PAUSE_TOKEN, f" {_PAUSE_SENTINEL} ")

    blocks = [b.strip() for b in raw.split(_PAUSE_SENTINEL) if b.strip()]
    if not blocks:
        silent = AudioSegment.silent(duration=1000, frame_rate=44100)
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        f.close()
        silent.export(f.name, format="wav")
        return f.name

    segs: List[AudioSegment] = []
    PAUSE_MS = 900          # ~0.9s of silence for [pause]
    CHUNK_GAP_MS = 350      # ~0.35s between chunks for more natural pacing

    for block_idx, block in enumerate(blocks):
        parts = _split_text_into_chunks(block, max_chars=max_chars)
        for j, p in enumerate(parts):
            print(
                f"[TTS] Synthesising block {block_idx + 1}/{len(blocks)}, "
                f"chunk {j + 1}/{len(parts)}, len={len(p)} chars"
            )
            segs.append(_synth_chunk(p, voice_id, key, voice_settings=voice_settings))
            # Short gap between chunks inside the same block
            if j < len(parts) - 1:
                segs.append(
                    AudioSegment.silent(
                        duration=CHUNK_GAP_MS,
                        frame_rate=44100,
                    )
                )

        # Longer pause when the script explicitly used [pause]
        if block_idx < len(blocks) - 1:
            segs.append(
                AudioSegment.silent(
                    duration=PAUSE_MS,
                    frame_rate=44100,
                )
            )

    full = segs[0]
    for s in segs[1:]:
        full += s

    # Noise reduction to remove ElevenLabs synthesis artifacts
    samples = np.array(full.get_array_of_samples(), dtype=np.float32)
    reduced = nr.reduce_noise(
        y=samples,
        sr=full.frame_rate,
        stationary=True,
        prop_decrease=0.75,
    )
    reduced_int = np.int16(np.clip(reduced, -32768, 32767))
    full = AudioSegment(
        data=reduced_int.tobytes(),
        sample_width=full.sample_width,
        frame_rate=full.frame_rate,
        channels=full.channels,
    )
    print("[TTS] Noise reduction applied")

    outf = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    outf.close()
    full.export(outf.name, format="wav")
    return outf.name

def synth_meditation(
    text: str,
    voice_id: str,
    key: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    voice_settings: Optional[Dict] = None,
    pause_ms: int = 3000,
    long_pause_ms: int = 6000,
) -> str:
    """
    Meditation synthesis. Same chunking, retries and noise reduction as
    synth(), different pause handling:

    [pause]       -> pause_ms of real silence (default 3s)
    [long pause]  -> long_pause_ms of real silence (default 6s)
    <break time="X s"/> -> X seconds of real silence
    any other [tag]     -> removed, never spoken

    ElevenLabs never sees any marker. Silence is inserted in the audio
    after synthesis, so there are no dropped tags and no spoken brackets.
    Day 1 meditation scripts have no markers and fit in one chunk, so they
    go out as a single API call with no stitching.
    """
    raw = (text or "").strip()

    def _silent_wav() -> str:
        silent = AudioSegment.silent(duration=1000, frame_rate=44100)
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        f.close()
        silent.export(f.name, format="wav")
        return f.name

    if not raw:
        return _silent_wav()

    # Break tags become pause sentinels carrying their duration in ms.
    def _break_to_sentinel(m):
        try:
            ms = int(float(m.group(1)) * 1000)
        except ValueError:
            ms = pause_ms
        return f" <<<SIL:{ms}>>> "

    raw = _BREAK_TAG_RE.sub(_break_to_sentinel, raw)
    raw = raw.replace(_LONG_PAUSE_TOKEN, f" <<<SIL:{long_pause_ms}>>> ")
    raw = raw.replace(_PAUSE_TOKEN, f" <<<SIL:{pause_ms}>>> ")
    # Anything bracketed that is still left is a stray tag. Remove it.
    raw = _BRACKET_TAG_RE.sub(" ", raw)

    # Split into speech blocks and silence markers, in order.
    pieces = re.split(r"(<<<SIL:\d+>>>)", raw)
    plan = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        m = re.fullmatch(r"<<<SIL:(\d+)>>>", piece)
        if m:
            plan.append(("silence", int(m.group(1))))
        else:
            plan.append(("speech", piece))

    if not any(kind == "speech" for kind, _ in plan):
        return _silent_wav()

    segs: List[AudioSegment] = []
    CHUNK_GAP_MS = 350

    for kind, value in plan:
        if kind == "silence":
            segs.append(AudioSegment.silent(duration=value, frame_rate=44100))
            continue
        parts = _split_text_into_chunks(value, max_chars=max_chars)
        for j, p in enumerate(parts):
            print(
                f"[TTS] Meditation chunk {j + 1}/{len(parts)}, len={len(p)} chars"
            )
            segs.append(_synth_chunk(p, voice_id, key, voice_settings=voice_settings))
            if j < len(parts) - 1:
                segs.append(
                    AudioSegment.silent(duration=CHUNK_GAP_MS, frame_rate=44100)
                )

    full = segs[0]
    for s in segs[1:]:
        full += s

    samples = np.array(full.get_array_of_samples(), dtype=np.float32)
    reduced = nr.reduce_noise(
        y=samples,
        sr=full.frame_rate,
        stationary=True,
        prop_decrease=0.75,
    )
    reduced_int = np.int16(np.clip(reduced, -32768, 32767))
    full = AudioSegment(
        data=reduced_int.tobytes(),
        sample_width=full.sample_width,
        frame_rate=full.frame_rate,
        channels=full.channels,
    )
    print("[TTS] Noise reduction applied")

    outf = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    outf.close()
    full.export(outf.name, format="wav")
    return outf.name
