import os
from pathlib import Path


class Config:

    DEV_MODE: bool = os.getenv("DEV_MODE", "").lower() in ("true", "1", "yes")

    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    HAIKU_MODEL: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")

    FFMPEG_BIN: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    FFPROBE_BIN: str = os.getenv("FFPROBE_BIN", "ffprobe")

    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    ASSETS_DIR: Path = BASE_DIR / "assets"
    OUT_DIR: str = os.getenv("OUT_DIR", "/data/audio")

    @property
    def out_dir_path(self) -> Path:
        p = Path(self.OUT_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Track registry
    # total_duration_sec: full file length (including trailing silence/fade)
    # content_duration_sec: actual music content (before trailing silence/fade)
    # target_words: SPOKEN word count for speech generation.
    #   Calibrated per voice against actual ElevenLabs v3 delivery rates
    #   (~1.8 words/sec for expressive voices). Claude's prompt now enforces
    #   precise word counts, so these should match the true duration needs.
    #   Tags like [whispers], [pause], [sighs] do NOT count.
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

    DB_URL: str = os.getenv("DB_URL", "sqlite:///./rewire.db")

    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HRS: int = int(os.getenv("JWT_EXPIRE_HRS", "24"))

    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    ALLOWED_ORIGINS: list = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_CONTACT: str = os.getenv("VAPID_CONTACT", "mailto:hello@rewire.bio")
    VAPID_CLAIMS_EMAIL: str = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:hello@rewire.bio")

    # Stripe
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_PRICE_ID_MONTHLY: str = os.getenv("STRIPE_PRICE_ID_MONTHLY", "")
    STRIPE_PRICE_ID_YEARLY: str = os.getenv("STRIPE_PRICE_ID_YEARLY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")


cfg = Config()
