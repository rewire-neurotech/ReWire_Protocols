from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey, Date,
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
