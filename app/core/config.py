import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env into the environment before any os.getenv() below runs.
# Safe in production: load_dotenv() does NOT overwrite variables that are
# already set in the real environment (e.g. Render's injected vars win).
load_dotenv()


class Config:

    DEV_MODE: bool = os.getenv("DEV_MODE", "").lower() in ("true", "1", "yes")

    # ------------------------------------------------------------------ #
    # AI / voice providers
    # ------------------------------------------------------------------ #
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    HAIKU_MODEL: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    FFMPEG_BIN: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    FFPROBE_BIN: str = os.getenv("FFPROBE_BIN", "ffprobe")

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    ASSETS_DIR: Path = BASE_DIR / "assets"
    OUT_DIR: str = os.getenv("OUT_DIR", "/data/audio")

    @property
    def out_dir_path(self) -> Path:
        p = Path(self.OUT_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ================================================================== #
    # LEGACY v4 TRACK REGISTRY  (kept for backward compatibility)
    # ------------------------------------------------------------------ #
    # The v4 generation path (services/llm.py, services/music_selector.py,
    # routes/jolt.py) still reads TRACKS / TRACK_ORDER / get_track().
    # DO NOT remove until those modules are migrated to the v5 protocol
    # path below, or the running app will break. These reference the old
    # v4 asset files, which are still present in app/assets/.
    #
    # total_duration_sec: full file length (including trailing silence/fade)
    # content_duration_sec: actual music content (before trailing silence/fade)
    # target_words: SPOKEN word count for speech generation.
    #   Calibrated per voice against actual ElevenLabs v3 delivery rates
    #   (~1.8 words/sec for expressive voices).
    #   Tags like [whispers], [pause], [sighs] do NOT count.
    # ================================================================== #
    TRACKS = {
        "hallelujah": {
            "file": "a_thousand_hearts.mpeg",
            "voice_id": "lMILJ9d29MrRXy9BIgcz",
            "total_duration_sec": 129,
            "content_duration_sec": 126,
            "target_words": 230,
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.7,
                "style": 0.85,
                "use_speaker_boost": True,
            },
        },
        "suuvi": {
            "file": "ad_infinitum.mpeg",
            "voice_id": "lMILJ9d29MrRXy9BIgcz",
            "total_duration_sec": 264,
            "content_duration_sec": 259,
            "target_words": 470,
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.7,
                "style": 0.85,
                "use_speaker_boost": True,
            },
        },
        "ww2": {
            "file": "heroes_wwii.mp3",
            "voice_id": "0yXkuUWXDHdmdQJugJLb",
            "total_duration_sec": 353,
            "content_duration_sec": 333,
            "target_words": 700,
            "voice_settings": {
                "stability": 0.50,
                "similarity_boost": 0.7,
                "style": 0.65,
                "use_speaker_boost": True,
            },
        },
    }

    # First jolt is Suuvi, then Hallelujah, then WW2, then cycles
    TRACK_ORDER = ["suuvi", "hallelujah", "ww2"]

    def get_track(self, name=None):
        t = self.TRACKS.get(name)
        if not t:
            t = self.TRACKS["suuvi"]
            name = "suuvi"
        return {
            "name": name,
            "file": self.ASSETS_DIR / t["file"],
            "voice_id": t["voice_id"],
            "total_duration_sec": t["total_duration_sec"],
            "content_duration_sec": t["content_duration_sec"],
            "target_words": t["target_words"],
            "voice_settings": t["voice_settings"],
        }

    # ================================================================== #
    # v5 PROTOCOL TRACK REGISTRY
    # ------------------------------------------------------------------ #
    # A protocol is a 5-day arc. Each protocol type uses one musical theme,
    # rendered at 5 lengths (one per day). The lengths follow the emotional
    # arc: they rise toward day 5 (the peak / Consolidation) and dip at
    # day 3 (the soft middle). Track N -> day N.
    #
    # total_duration_sec   : full mp3 file length (measured).
    # content_duration_sec : where the music actually ends, before the
    #                        trailing silence/fade. THIS is the number that
    #                        matters: mix.py trims the music to it and
    #                        retimes the voice to fill it exactly, so the
    #                        last word lands on the last second of music.
    # target_words         : SPOKEN word-count target for generation.
    #                        Starting estimates at ~1.8 words/sec (v4's
    #                        proven expressive-voice rate). These are a DIAL,
    #                        not a guarantee: the mix locks the ending
    #                        regardless, but a good count keeps the voice
    #                        natural instead of stretched. Tune by ear:
    #                        if the voice drags near the end, raise it; if it
    #                        rushes, lower it.
    # ================================================================== #
    PROTOCOL_TYPES: list = ["activate", "integrate", "expand"]
    PROTOCOL_DAYS: int = 5
    PROTOCOL_FREE_DAYS: int = 1  # day 1 jolt is free; days 2-5 require purchase

    # Voice per protocol type. All 5 days of a type share one voice + settings.
    # NOTE: voice_id defaults to the expressive voice used for suuvi/hallelujah
    # in v4, which matches the ~1.8 wps calibration. If the Activate tracks were
    # rendered against a different ElevenLabs voice, set ACTIVATE_VOICE_ID in
    # .env (same for INTEGRATE_VOICE_ID / EXPAND_VOICE_ID).
    PROTOCOL_VOICE = {
        "activate": {
            "voice_id": os.getenv("ACTIVATE_VOICE_ID", "lMILJ9d29MrRXy9BIgcz"),
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.7,
                "style": 0.85,
                "use_speaker_boost": True,
            },
        },
        "integrate": {
            "voice_id": os.getenv("INTEGRATE_VOICE_ID", "lMILJ9d29MrRXy9BIgcz"),
            "voice_settings": {
                "stability": 0.40,
                "similarity_boost": 0.7,
                "style": 0.70,
                "use_speaker_boost": True,
            },
        },
        "expand": {
            "voice_id": os.getenv("EXPAND_VOICE_ID", "lMILJ9d29MrRXy9BIgcz"),
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.7,
                "style": 0.60,
                "use_speaker_boost": True,
            },
        },
    }

    PROTOCOL_TRACKS = {
        "activate": [
            # Day 1-2: Felix's new Sacred Revelation tracks (Aug 2026)
            # Day 1: new_music_2.mpeg — 2:55 content -> 310 words @ 1.8 wps
            {"day": 1, "file": "new_music_2.mpeg", "total_duration_sec": 177, "content_duration_sec": 175, "target_words": 310},
            # Day 2: new_music_1.mpeg — 2:58 content -> 315 words @ 1.8 wps
            {"day": 2, "file": "new_music_1.mpeg", "total_duration_sec": 180, "content_duration_sec": 178, "target_words": 315},
            # Days 3-5: original Sacred tracks (kept until Felix sends replacements)
            {"day": 3, "file": "3_Sacred.mp3", "total_duration_sec": 140, "content_duration_sec": 139, "target_words": 250},
            {"day": 4, "file": "4_Sacred.mp3", "total_duration_sec": 150, "content_duration_sec": 149, "target_words": 270},
            {"day": 5, "file": "5_Sacred.mp3", "total_duration_sec": 160, "content_duration_sec": 158, "target_words": 285},
        ],
        # NOT BUILT YET. Activate is the only live protocol. Add 5 tracks here
        # for each once the Integrate / Expand prompts + music exist, and fill
        # in their arcs in jolt_prompts.py at the same time.
        "integrate": [],
        "expand": [],
    }

    def get_protocol_track(self, protocol_type: str, day: int) -> dict:
        """Return the merged track config for a protocol type + day.

        Shape mirrors v4's get_track() so tts.py / mix.py can consume it the
        same way (file path, voice_id, durations, target_words, voice_settings).
        """
        ptype = (protocol_type or "activate").lower()
        days = self.PROTOCOL_TRACKS.get(ptype) or []
        entry = next((d for d in days if d["day"] == day), None)
        if entry is None:
            raise ValueError(
                f"No track configured for protocol '{ptype}' day {day}"
            )
        voice = self.PROTOCOL_VOICE.get(ptype, self.PROTOCOL_VOICE["activate"])
        return {
            "protocol_type": ptype,
            "day": day,
            "file": self.ASSETS_DIR / entry["file"],
            "voice_id": voice["voice_id"],
            "total_duration_sec": entry["total_duration_sec"],
            "content_duration_sec": entry["content_duration_sec"],
            "target_words": entry["target_words"],
            "voice_settings": voice["voice_settings"],
        }

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    DB_URL: str = os.getenv("DB_URL", "sqlite:///./rewire.db")

    # ------------------------------------------------------------------ #
    # Auth (JWT + Google)
    # ------------------------------------------------------------------ #
    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HRS: int = int(os.getenv("JWT_EXPIRE_HRS", "24"))

    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    ALLOWED_ORIGINS: list = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    # ------------------------------------------------------------------ #
    # Web push (VAPID)
    # ------------------------------------------------------------------ #
    VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_CONTACT: str = os.getenv("VAPID_CONTACT", "mailto:hello@rewire.bio")
    VAPID_CLAIMS_EMAIL: str = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:hello@rewire.bio")

    # ------------------------------------------------------------------ #
    # Stripe
    # ------------------------------------------------------------------ #
    # v4 monthly/yearly kept for backward compatibility with the current
    # subscription route. v5 adds a per-protocol one-time price and reuses
    # the monthly price. Display amounts are used by the paywall copy.
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_PRICE_ID_MONTHLY: str = os.getenv("STRIPE_PRICE_ID_MONTHLY", "")
    STRIPE_PRICE_ID_YEARLY: str = os.getenv("STRIPE_PRICE_ID_YEARLY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # v5: one-time purchase that unlocks a single protocol (days 2-5) forever.
    STRIPE_PRICE_ID_PROTOCOL: str = os.getenv("STRIPE_PRICE_ID_PROTOCOL", "")
    PROTOCOL_PRICE_USD: int = int(os.getenv("PROTOCOL_PRICE_USD", "9"))
    MONTHLY_PRICE_USD: int = int(os.getenv("MONTHLY_PRICE_USD", "12"))

    # ------------------------------------------------------------------ #
    # Redis / task queue (v5 async generation)
    # ------------------------------------------------------------------ #
    # Not connected to yet; only read once tasks.py / worker.py are added.
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    RQ_QUEUE_NAME: str = os.getenv("RQ_QUEUE_NAME", "jolts")
    # Cap simultaneous heavy ffmpeg mixes so a burst of users cannot OOM the
    # worker. Size to the worker instance's RAM.
    MAX_CONCURRENT_MIXES: int = int(os.getenv("MAX_CONCURRENT_MIXES", "2"))

    # ------------------------------------------------------------------ #
    # Object storage (v5: generated audio off the local disk)
    # ------------------------------------------------------------------ #
    # STORAGE_BACKEND: "local" (default) writes encrypted audio to OUT_DIR
    # exactly like v4. "s3" stores the encrypted blob in an S3/R2 bucket,
    # which removes Render's single-disk constraint and lets the app scale.
    # The audio stays Fernet-encrypted either way.
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "local")
    STORAGE_BUCKET: str = os.getenv("STORAGE_BUCKET", "")
    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")
    S3_ACCESS_KEY_ID: str = os.getenv("S3_ACCESS_KEY_ID", "")
    S3_SECRET_ACCESS_KEY: str = os.getenv("S3_SECRET_ACCESS_KEY", "")
    S3_REGION: str = os.getenv("S3_REGION", "auto")
    SIGNED_URL_TTL_SEC: int = int(os.getenv("SIGNED_URL_TTL_SEC", "3600"))


cfg = Config()
