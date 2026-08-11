from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey, Date,
    UniqueConstraint,
)
from app.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(300), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    dob = Column(Date, nullable=True)
    gender = Column(String(30), nullable=True)
    auth_provider = Column(String(20), nullable=False, default="local")
    google_id = Column(String(200), nullable=True, unique=True)
    is_admin = Column(Boolean, default=False, nullable=False, server_default="false")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    disclaimer_accepted_at = Column(DateTime, nullable=True)
    disclaimer_version = Column(String(20), nullable=True)
    onboarding_complete = Column(Boolean, default=False, nullable=False, server_default="false")

    # count % 3 -> 0 suuvi, 1 hallelujah, 2 ww2
    total_jolt_count = Column(Integer, default=0, nullable=False, server_default="0")
    jolt_count_today = Column(Integer, default=0, nullable=False, server_default="0")
    last_jolt_date = Column(Date, nullable=True)

    stripe_customer_id = Column(String(200), nullable=True)

    # ADMIN CONSOLE: last time this account made an authenticated request.
    # Written by the activity touch in routes/auth.py (throttled to once per
    # window per user). Powers the "Last seen" column in the People table.
    # NOTE: this is a NEW column on an EXISTING table, so create_all will not
    # add it to a database that already exists -- the ALTER TABLE migration in
    # main.py does that. Nothing reads or writes it until that migration ships.
    last_active_at = Column(DateTime, nullable=True)


class Goal(Base):
    __tablename__ = "goals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    display_title = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=True)
    place = Column(String(200), nullable=True)
    reflection_question = Column(
        Text, nullable=False,
        default="What is one small step you could take today?",
        server_default="What is one small step you could take today?",
    )
    done_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Challenge(Base):
    __tablename__ = "challenges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Tip(Base):
    __tablename__ = "tips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    from_name = Column(String(100), nullable=True, default="You")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Jolt(Base):
    __tablename__ = "jolts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    track_name = Column(String(50), nullable=True)
    voice_id = Column(String(60), nullable=True)
    speech_text = Column(Text, nullable=True)
    speech_format = Column(String(40), nullable=True)
    audio_filename = Column(String(200), nullable=True)
    voice_filename = Column(String(200), nullable=True)
    stage = Column(String(40), nullable=False, default="queued")
    progress = Column(Integer, nullable=False, default=0)
    gen_error = Column(Text, nullable=True)
    gen_time_sec = Column(Float, nullable=True)
    hc_status = Column(String(20), nullable=True)
    hc_category = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Reflection(Base):
    __tablename__ = "reflections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    jolt_id = Column(Integer, ForeignKey("jolts.id", ondelete="SET NULL"), nullable=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question = Column(Text, nullable=True)
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="active")
    code_used = Column(String(50), nullable=True)
    stripe_session_id = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    max_uses = Column(Integer, default=0, nullable=False)
    uses_count = Column(Integer, default=0, nullable=False, server_default="0")
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    p256dh = Column(String(200), nullable=False)
    auth = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    user_email = Column(String(200), nullable=True)
    action = Column(String(100), nullable=False)
    target = Column(String(300), nullable=True)
    ip_address = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# =========================================================================== #
# v5 PROTOCOL MODELS
# --------------------------------------------------------------------------- #
# All NEW tables. Under Option A (Base.metadata.create_all on startup) these
# are created automatically on the next boot, on SQLite locally and on the live
# Postgres, WITHOUT touching any existing v4 table above. No v4 table's columns
# are modified here, because create_all only creates missing tables; it does
# not alter existing ones. v5 state therefore lives entirely in these new
# tables, and the v4 Goal / Challenge / Tip / Jolt / Reflection / Subscription
# tables remain untouched and are retired only at the end of the migration.
# =========================================================================== #


class Protocol(Base):
    __tablename__ = "protocols"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # activate | integrate | expand
    type = Column(String(20), nullable=False, default="activate")
    target = Column(Text, nullable=False)        # the goal they typed
    charge = Column(Text, nullable=True)         # why it matters (private fuel; never surfaced raw)
    title = Column(String(200), nullable=True)   # optional display title

    # input safety screen result (run on target + charge, before the plan)
    input_verdict = Column(String(20), nullable=True)    # safe | clarify | block | crisis
    input_category = Column(String(30), nullable=True)

    # Unlock state for days 2-5. True once this single protocol is purchased
    # (one-time). A user with an active monthly Entitlement unlocks ALL
    # protocols regardless of this flag; the route computes effective access.
    unlocked = Column(Boolean, default=False, nullable=False, server_default="false")

    status = Column(String(20), nullable=False, default="active")   # active | complete
    # Which protocol is currently shown on the home path. The route keeps one
    # active per user; others are set false when a new one is created/selected.
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProtocolDay(Base):
    __tablename__ = "protocol_days"

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False, index=True)
    day = Column(Integer, nullable=False)        # 1..5

    # From the PLAN step (the protocol architect prompt):
    stage = Column(String(30), nullable=True)    # initiation | proof | middle | momentum | consolidation
    action = Column(Text, nullable=True)         # the day's concrete action (revealed once the jolt runs)
    brief = Column(Text, nullable=True)          # one-line emotional brief for the speechwriter

    # User state (the daily loop): today's jolt unlocks only when yesterday's
    # action is marked done.
    done = Column(Boolean, default=False, nullable=False, server_default="false")
    done_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ProtocolJolt(Base):
    __tablename__ = "protocol_jolts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False, index=True)
    day = Column(Integer, nullable=False)        # 1..5 (which day this jolt belongs to)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    track_name = Column(String(50), nullable=True)
    voice_id = Column(String(60), nullable=True)
    speech_text = Column(Text, nullable=True)
    speech_format = Column(String(40), nullable=True)
    audio_filename = Column(String(200), nullable=True)
    voice_filename = Column(String(200), nullable=True)

    stage = Column(String(40), nullable=False, default="queued")   # queued|generating|synthesizing|mixing|done|error|blocked
    progress = Column(Integer, nullable=False, default=0)
    gen_error = Column(Text, nullable=True)
    gen_time_sec = Column(Float, nullable=True)

    # output safety screen result (run on the generated speech)
    screen_verdict = Column(String(20), nullable=True)    # pass | fail
    screen_category = Column(String(30), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="SET NULL"), nullable=True, index=True)
    day = Column(Integer, nullable=True)         # which day's reflection, if any
    question = Column(Text, nullable=True)       # the reflection prompt (null for a free-form note)
    answer = Column(Text, nullable=True)         # the entry text
    chills = Column(String(10), nullable=True)   # "yes" | "no" | null (not asked, e.g. free-form entry)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class JournalJolt(Base):
    """A standalone jolt generated from a journal entry's text (not part of a
    protocol's 5-day arc). Mirrors the ProtocolJolt generation fields so the
    same speech -> TTS -> mix pipeline can produce it."""
    __tablename__ = "journal_jolts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    journal_entry_id = Column(
        Integer, ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    track_name = Column(String(50), nullable=True)
    voice_id = Column(String(60), nullable=True)
    speech_text = Column(Text, nullable=True)
    speech_format = Column(String(40), nullable=True)
    audio_filename = Column(String(200), nullable=True)
    voice_filename = Column(String(200), nullable=True)

    stage = Column(String(40), nullable=False, default="queued")   # queued|generating|synthesizing|mixing|done|error|blocked
    progress = Column(Integer, nullable=False, default=0)
    gen_error = Column(Text, nullable=True)
    gen_time_sec = Column(Float, nullable=True)

    # output safety screen result (run on the generated speech)
    screen_verdict = Column(String(20), nullable=True)    # pass | fail
    screen_category = Column(String(30), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Entitlement(Base):
    __tablename__ = "entitlements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # "protocol" -> one-time unlock of a single protocol (protocol_id set)
    # "monthly"  -> membership unlocking all protocols (protocol_id null)
    kind = Column(String(20), nullable=False)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"), nullable=True, index=True)

    status = Column(String(20), nullable=False, default="active")   # active | canceled | expired
    stripe_session_id = Column(String(200), nullable=True)
    stripe_subscription_id = Column(String(200), nullable=True)     # for monthly memberships
    canceling = Column(Boolean, default=False)                      # monthly set to not renew (stays active until expires_at)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)                    # for monthly memberships


# =========================================================================== #
# ADMIN CONSOLE MODELS
# --------------------------------------------------------------------------- #
# Four NEW tables that record things the app currently acts on and then throws
# away. Like the v5 block above, create_all makes them on next boot and touches
# nothing that already exists.
#
# They exist because the console can only ever show what the app wrote down:
#
#   UserActivity  -> retention curve + "Last seen". The app records what people
#                    CREATE (a protocol, a journal entry, a tick) but nothing
#                    about them simply OPENING the app, so neither number is
#                    currently answerable.
#   SafetyEvent   -> the Safety tab. Today a flagged input is screened, the
#                    person is shown crisis resources (that part works), and
#                    then the verdict AND their words are discarded -- no row is
#                    written anywhere. This table is the record.
#   SafetyReview  -> the "Right call" / "Wrong call" decisions made in the
#                    console against those events.
#   Payment       -> the Revenue chart. Entitlements record THAT someone paid,
#                    never HOW MUCH, and a monthly renewal creates no
#                    entitlement row at all (the webhook only pushes expires_at
#                    forward), so months 2, 3, 4... are invisible. This is a
#                    ledger: one row per payment event, initial and renewal
#                    alike, so revenue is a single SUM over one table.
# =========================================================================== #


class UserActivity(Base):
    """One row per user per calendar day (UTC) on which they made an
    authenticated request.

    Written by the throttled activity touch in routes/auth.py. Deliberately a
    table rather than a single column on User: a lone "last_active_at" answers
    "who is still around", but the retention curve asks "of everyone who signed
    up, what share came back on day N", which needs the set of days a person was
    present, not just the most recent one.

    Cheap by construction: at most one insert per user per day, and the unique
    constraint makes a repeat write a no-op the caller can swallow.
    """
    __tablename__ = "user_activity"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    activity_date = Column(Date, nullable=False, index=True)   # UTC calendar date
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "activity_date", name="uq_user_activity_day"),
    )


class SafetyEvent(Base):
    """A record of every time a safety layer stopped or diverted something.

    Written at three points, distinguished by `layer`:
      L1 - the input screen (safety_screen.py) returned clarify | block | crisis
           on a protocol create or a journal jolt request
      L2 - the generation prompt returned the REWIRE_UNSAFE token
      L3 - the output screen (output_screen.py) failed the generated speech
           after its retry cap

    `said` is the verbatim text the person wrote. It is stored ENCRYPTED via
    utils.encryption.encrypt_field, exactly like Protocol.charge, and must be
    decrypted for display. It is the most sensitive column in the schema.

    `severity` is DERIVED at write time from verdict + category (the mapping
    lives in core/config.py). The safety screen does not emit a severity or a
    confidence score today, and we are deliberately not changing its prompt to
    add them, because the ST1 benchmark is frozen against the current prompt.
    When ST3 lands it produces a calibrated score by design and this column can
    be filled from the model instead of a lookup table.

    The reference columns are plain nullable Integers, NOT foreign keys, on
    purpose: the commonest case (a blocked protocol create) is precisely the
    case where no Protocol row was ever created, and an L1 block on a journal
    entry must survive that entry being deleted. The event is the record; it
    should never be cascaded away by the thing it refers to.
    """
    __tablename__ = "safety_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    layer = Column(String(4), nullable=False, index=True)    # L1 | L2 | L3
    source = Column(String(30), nullable=False)              # protocol_create | journal_jolt | protocol_jolt
    verdict = Column(String(20), nullable=False, index=True) # clarify | block | crisis | unsafe | fail
    category = Column(String(40), nullable=True)             # self_harm | disordered_eating | ...
    severity = Column(String(4), nullable=True, index=True)  # derived: S2 | S3 | S4

    said = Column(Text, nullable=True)          # ENCRYPTED verbatim user text
    auto_action = Column(Text, nullable=True)   # what the app did, in plain words
    rationale = Column(Text, nullable=True)     # the screen's own one-line reason (internal)

    # Loose references, no FKs -- see the class docstring.
    protocol_id = Column(Integer, nullable=True)
    journal_entry_id = Column(Integer, nullable=True)
    jolt_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class SafetyReview(Base):
    """A human decision on one SafetyEvent, made in the admin console.

    `decision` uses the same two values the console's buttons already send:
      "confirmed" -> Right call (the screen was correct to flag this)
      "false"     -> Wrong call (the screen stopped someone who was fine)

    One decision per event, enforced by the unique constraint. Undo is a delete
    of this row; changing your mind is a delete followed by an insert. Keeping
    it as a separate table rather than columns on SafetyEvent means the flag
    itself is never edited after the fact.
    """
    __tablename__ = "safety_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    safety_event_id = Column(
        Integer, ForeignKey("safety_events.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewer_email = Column(String(200), nullable=True)

    decision = Column(String(20), nullable=False)   # confirmed | false
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("safety_event_id", name="uq_safety_review_event"),
    )


class Payment(Base):
    """Money ledger: one row per payment event.

    Entitlements answer "what does this person have access to". This answers
    "what did they actually pay, and when" -- which entitlements cannot, because
    a monthly renewal produces no new entitlement row, only a bumped expires_at.
    Revenue for any window is therefore a single SUM over created_at here.

    `kind` distinguishes the three money events:
      protocol         - one-time $9 unlock of a single protocol
      monthly          - first payment on a new membership
      monthly_renewal  - a subsequent membership payment (Stripe invoice.paid)

    Amounts are integer cents straight from Stripe, never inferred from the
    current config prices, so the history stays correct if prices ever change.

    Idempotency is by stripe_session_id (checkout) or stripe_invoice_id
    (renewal), checked in code before insert -- matching how routes/subscription.py
    already guards its grants. Both are indexed but not UNIQUE, deliberately: a
    unique violation raised inside a webhook handler would return a 500 to
    Stripe and trigger an ever-growing retry storm, which is a worse failure
    than the duplicate row it prevents.
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    kind = Column(String(20), nullable=False, index=True)   # protocol | monthly | monthly_renewal
    amount_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(10), nullable=False, default="usd")

    entitlement_id = Column(Integer, nullable=True)         # loose ref, no FK
    stripe_session_id = Column(String(200), nullable=True, index=True)
    stripe_invoice_id = Column(String(200), nullable=True, index=True)
    stripe_subscription_id = Column(String(200), nullable=True, index=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
