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

    # ------------------------------------------------------------------ #
    # ADMIN CONSOLE
    # ------------------------------------------------------------------ #
    # Everything below exists only to serve the internal ops console at
    # /admin. None of it changes app behaviour for a normal user.

    # Bootstrap: comma-separated emails promoted to is_admin=True at startup
    # (see main.py). This is how the first admin exists at all, since there is
    # no shell on Render and no UI for granting the flag. Matching is
    # case-insensitive and whitespace-tolerant. Accounts must already exist;
    # this never creates one.
    #   ADMIN_EMAILS="felix@rewire.bio,ashwin@rewire.bio"
    ADMIN_EMAILS_RAW: str = os.getenv("ADMIN_EMAILS", "")

    @property
    def admin_emails(self) -> list:
        return [
            e.strip().lower()
            for e in (self.ADMIN_EMAILS_RAW or "").split(",")
            if e.strip()
        ]

    def is_admin_email(self, email: str) -> bool:
        return bool(email) and email.strip().lower() in self.admin_emails

    # How long a single process waits before writing another activity row for
    # the same user. UserActivity is unique per (user, UTC date), so a duplicate
    # write is harmless -- this is purely to avoid a pointless DB round trip on
    # every single request. In-memory per worker, so with 2 uvicorn workers the
    # real ceiling is 2 writes per user per window. Still nothing.
    ACTIVITY_TOUCH_TTL_SEC: int = int(os.getenv("ACTIVITY_TOUCH_TTL_SEC", "900"))

    # A protocol that is neither complete nor touched within this many days is
    # shown as "stalled" in the console. Purely a display concept: the backend
    # itself only knows active | complete, and this never writes to the DB.
    PROTOCOL_STALLED_DAYS: int = int(os.getenv("PROTOCOL_STALLED_DAYS", "5"))

    # Time windows the console's 7d / 30d / 90d segment may request. Anything
    # else is clamped to ADMIN_DEFAULT_RANGE_DAYS, so a hand-edited query string
    # cannot ask the database for an unbounded scan.
    ADMIN_RANGE_DAYS_ALLOWED: tuple = (7, 30, 90)
    ADMIN_DEFAULT_RANGE_DAYS: int = 30

    # Hard ceiling on rows returned by the People table and the safety queue.
    ADMIN_MAX_ROWS: int = int(os.getenv("ADMIN_MAX_ROWS", "500"))

    # The retention curve's x-axis, in days since signup. Must stay <= 30 to
    # match the chart Felix drew (its x scale is fixed at 0..30).
    ADMIN_RETENTION_DAYS: tuple = (0, 1, 3, 7, 14, 21, 30)

    # --- dev login (INTERNAL TESTING ONLY) --------------------------------- #
    # A username/password that signs straight into the console without an
    # account, for internal testing before there are real team accounts.
    #
    # OFF unless ADMIN_DEV_LOGIN is explicitly set to true. Unset, the code path
    # does not exist -- the endpoint refuses before looking at any credential.
    # This is deliberate: /admin is a public URL, and what sits behind it is
    # every user's private charge text plus verbatim writing from people in
    # crisis. A weak credential is a reasonable trade for a few days of internal
    # testing; one that survives into production by accident is not.
    #
    # To retire it: delete ADMIN_DEV_LOGIN from the environment. Nothing else
    # changes -- normal email/password sign-in for accounts in ADMIN_EMAILS
    # keeps working either way, and both paths run through the same JWT.
    ADMIN_DEV_LOGIN: bool = os.getenv("ADMIN_DEV_LOGIN", "").lower() in ("true", "1", "yes")
    ADMIN_DEV_USER: str = os.getenv("ADMIN_DEV_USER", "admin")
    ADMIN_DEV_PASS: str = os.getenv("ADMIN_DEV_PASS", "admin")
    # The account rows created by the dev login are tagged with this address so
    # they are obvious in the users table and in audit_logs.
    ADMIN_DEV_EMAIL: str = os.getenv("ADMIN_DEV_EMAIL", "admin@console.local")

    # --- privacy switches -------------------------------------------------- #
    # Emails are masked server-side (j***23@gmail.com), never sent whole to the
    # browser. Set false only with a deliberate reason.
    ADMIN_MASK_EMAILS: bool = os.getenv("ADMIN_MASK_EMAILS", "true").lower() not in ("false", "0", "no")

    # Whether the People detail panel may decrypt and show Protocol.charge --
    # the rawest thing a user writes. Agreed policy is: show it, and log every
    # single read to the audit_logs table (Option A). Flipping this to false
    # hides charges without any other change.
    ADMIN_SHOW_CHARGE: bool = os.getenv("ADMIN_SHOW_CHARGE", "true").lower() not in ("false", "0", "no")

    # Write an AuditLog row every time an admin opens a person's detail panel.
    # This is the accountability half of the decision above; it should not be
    # turned off while ADMIN_SHOW_CHARGE is on.
    ADMIN_AUDIT_READS: bool = os.getenv("ADMIN_AUDIT_READS", "true").lower() not in ("false", "0", "no")

    # ------------------------------------------------------------------ #
    # SAFETY SEVERITY (derived, not model-reported)
    # ------------------------------------------------------------------ #
    # The console shows an S2 / S3 / S4 pill on each flagged case. The input
    # screen does NOT emit a severity, and we are deliberately not changing its
    # prompt to add one: the ST1 benchmark (3,248 rows) is frozen against the
    # prompt currently running, and editing it would mean those numbers no
    # longer describe production. The same reasoning is why no confidence score
    # is shown -- rather than invent one.
    #
    # So severity is a lookup on what the screen DOES tell us: verdict first,
    # then category. When ST3 lands it produces a calibrated score by design,
    # and severity_for() gets replaced by a real model output without the
    # console changing at all.
    #
    #   S4  the person themselves may be in danger        -> every crisis verdict
    #   S3  serious harm, not a personal crisis           -> self-harm-adjacent
    #                                                        blocks, and anything
    #                                                        the generation layers
    #                                                        (L2/L3) had to stop
    #   S2  everything else worth a second pair of eyes
    #
    # Note S4 covers all crisis verdicts rather than only "explicit intent":
    # the screen cannot distinguish stated-plan from no-plan, and quietly
    # downgrading half of them on a guess is the wrong direction to be wrong in.
    SEVERITY_S3_BLOCK_CATEGORIES: tuple = (
        "self_harm",
        "harm_to_others",
        "disordered_eating",
        "self_punishment",
    )

    # Ordering used for the "Most serious" KPI. Higher wins.
    SEVERITY_ORDER: dict = {"S2": 2, "S3": 3, "S4": 4}

    def severity_for(self, verdict: str, category: str = "") -> str:
        """Map a screen verdict + category onto the console's S2/S3/S4 pill.

        verdict  : clarify | block | crisis        (L1, input screen)
                   unsafe                          (L2, REWIRE_UNSAFE token)
                   fail                            (L3, output screen)
        category : the screen's own category, or "" when it did not give one.

        Never raises and never returns None -- an unrecognised verdict falls to
        S2 so an unexpected value still shows up in the queue rather than
        vanishing from it.
        """
        v = (verdict or "").strip().lower()
        c = (category or "").strip().lower()

        if v == "crisis":
            return "S4"
        if v == "block":
            return "S3" if c in self.SEVERITY_S3_BLOCK_CATEGORIES else "S2"
        if v in ("unsafe", "fail"):
            # The input screen let this through and a later layer caught it.
            # Worth a look regardless of category.
            return "S3"
        if v == "clarify":
            return "S2"
        return "S2"

    # Plain-English description of what the app already did, shown on each
    # queue card under "App already did:". Keyed by verdict. These must stay
    # factually true against the routes -- see routes/protocols.py and
    # routes/protocol_jolt.py for the behaviour each one describes.
    AUTO_ACTION_TEXT: dict = {
        "crisis":  "Generation blocked. Crisis resources shown instead.",
        "block":   "Generation blocked. Asked them to choose a different goal.",
        "clarify": "Generation paused. Asked them to restate the goal.",
        "unsafe":  "Speech generation refused by the safety prompt. Nothing was voiced.",
        "fail":    "Generated speech failed the output screen after retries. Nothing was voiced.",
    }

    def auto_action_for(self, verdict: str) -> str:
        return self.AUTO_ACTION_TEXT.get(
            (verdict or "").strip().lower(),
            "Generation stopped.",
        )


cfg = Config()
