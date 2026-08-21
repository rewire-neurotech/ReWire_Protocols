"""Join a theme primer to a finished meditation mix (Felix's 2_transition.py).

One complete session file comes out: the primer followed by the meditation,
joined the way Felix's scripts do it. Two modes, driven by cfg.PRIMER_TRACKS:

  concat  - the meditation is loudness mastered and glued directly after the
            primer. Ocean and fire. The primer carries its own ending space,
            so nothing is faded or crossfaded.
  overlay - the meditation is loudness mastered, delayed to the offset, and
            mixed on top of the primer with a limiter on the sum. Regular
            starts at a fixed 189.5s over the bird bridge. Forest starts at
            primer duration minus 5s under the fading ambience.

The mastering pass and every filter string are verbatim from Felix's scripts.
attach_primer never raises: on any failure the bare mix is copied to the
output path so a missing primer asset or an ffmpeg hiccup can never block a
jolt from being delivered.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.config import cfg

TARGET_LUFS = -16.0
TARGET_TRUE_PEAK_DB = -1.0
TARGET_LRA = 11.0
SAMPLE_RATE = 44100
BITRATE = "256k"


def _ffmpeg_bin(override=None):
    return override or cfg.FFMPEG_BIN or "ffmpeg"


def _ffprobe_bin():
    if cfg.FFPROBE_BIN:
        return cfg.FFPROBE_BIN
    ff = cfg.FFMPEG_BIN or "ffmpeg"
    if "ffmpeg" in ff:
        return ff.replace("ffmpeg", "ffprobe")
    return "ffprobe"


def _run(command, capture=False):
    return subprocess.run(command, check=True, text=True, capture_output=capture)


def _measure_loudness(ffmpeg, source):
    result = _run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-i", str(source),
            "-af",
            (
                f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:"
                f"LRA={TARGET_LRA}:print_format=json"
            ),
            "-f", "null", "-",
        ],
        capture=True,
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg did not return loudness measurements")
    return json.loads(matches[-1])


def _master_mix(ffmpeg, source, destination):
    measured = _measure_loudness(ffmpeg, source)
    loudnorm = (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:LRA={TARGET_LRA}:"
        f"measured_I={measured['input_i']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=summary"
    )
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-af", loudnorm,
            "-ar", str(SAMPLE_RATE), "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", BITRATE,
            str(destination),
        ]
    )


def _probe_duration(source):
    result = _run(
        [
            _ffprobe_bin(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(source),
        ],
        capture=True,
    )
    return float(result.stdout.strip())


def _concat(ffmpeg, primer, mix, destination):
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(primer), "-i", str(mix),
            "-filter_complex",
            (
                f"[0:a]aresample={SAMPLE_RATE},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[first];"
                f"[1:a]aresample={SAMPLE_RATE},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[second];"
                "[first][second]concat=n=2:v=0:a=1[out]"
            ),
            "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", BITRATE,
            str(destination),
        ]
    )


def _overlay(ffmpeg, primer, mix, destination, start_sec):
    delay_ms = round(start_sec * 1000)
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(primer), "-i", str(mix),
            "-filter_complex",
            (
                f"[0:a]aresample={SAMPLE_RATE},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[primer];"
                f"[1:a]aresample={SAMPLE_RATE},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={delay_ms}:all=1[mix];"
                "[primer][mix]amix=inputs=2:duration=longest:normalize=0,"
                "alimiter=limit=0.891251:attack=5:release=50:"
                "level=false:latency=true[out]"
            ),
            "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", BITRATE,
            str(destination),
        ]
    )


def attach_primer(theme: str, mix_path, out_path, ffmpeg_bin=None) -> float:
    """Join the theme's primer to the mix and write the complete session file.

    Returns the meditation start offset in seconds inside the final file
    (useful for logging). Never raises: on any failure the bare mix is
    copied to out_path and 0.0 is returned, so the jolt still delivers.
    """
    mix_path = Path(mix_path)
    out_path = Path(out_path)
    try:
        primer = cfg.get_primer(theme)
        primer_file = Path(primer["file"])
        if not primer_file.is_file():
            raise FileNotFoundError(f"primer asset missing: {primer_file}")

        ffmpeg = _ffmpeg_bin(ffmpeg_bin)

        if primer["mode"] == "overlay":
            if primer["offset_sec"] is not None:
                start_sec = float(primer["offset_sec"])
            else:
                start_sec = _probe_duration(primer_file) - float(primer["tail_sec"])
        else:
            start_sec = _probe_duration(primer_file)

        with tempfile.TemporaryDirectory(prefix="rewire_transition_") as temp_dir:
            mastered = Path(temp_dir) / "mix_mastered.mp3"
            _master_mix(ffmpeg, mix_path, mastered)
            if primer["mode"] == "overlay":
                _overlay(ffmpeg, primer_file, mastered, out_path, start_sec)
            else:
                _concat(ffmpeg, primer_file, mastered, out_path)

        print(
            f"[transition] {primer['theme']} {primer['mode']}: "
            f"meditation starts at {start_sec:.1f}s in {out_path.name}"
        )
        return start_sec

    except Exception as e:
        print(f"[transition] failed for theme {theme}, delivering bare mix: {e}")
        try:
            if mix_path.resolve() != out_path.resolve():
                shutil.copy2(str(mix_path), str(out_path))
        except Exception as copy_err:
            print(f"[transition] fallback copy failed: {copy_err}")
        return 0.0
