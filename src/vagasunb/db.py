import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    origin: Mapped[str] = mapped_column(String(16), default="demo", server_default="demo")
    cipher: Mapped[str] = mapped_column(Text)
    lookup: Mapped[str] = mapped_column(String(64), unique=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Classroom(Base):
    __tablename__ = "classrooms"
    __table_args__ = (
        UniqueConstraint("code", "number", "period"),
        Index("ix_classroom_period_code", "period", "code"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(250))
    search: Mapped[str] = mapped_column(Text, index=True)
    number: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(20))
    units: Mapped[list] = mapped_column(JSON)
    campus: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provenance: Mapped[str] = mapped_column(String(200), default="Não identificado")
    teachers: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    offered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occupied: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    candidate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("recipient_id", "code", "period"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(20))
    numbers: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Callback(Base):
    __tablename__ = "callbacks"
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(40))
    args: Mapped[dict] = mapped_column(JSON)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("recipient_id", "classroom_id", "cycle"),
        Index("ix_outbox_due", "status", "due"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    classroom_id: Mapped[str] = mapped_column(ForeignKey("classrooms.id", ondelete="CASCADE"))
    cycle: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Cooldown(Base):
    __tablename__ = "cooldowns"
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), primary_key=True
    )
    classroom_id: Mapped[str] = mapped_column(
        ForeignKey("classrooms.id", ondelete="CASCADE"), primary_key=True
    )
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    category: Mapped[str] = mapped_column(String(40))
    code: Mapped[str] = mapped_column(String(60))
    values: Mapped[dict] = mapped_column(JSON, default=dict)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int] = mapped_column(Integer, default=0)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password: Mapped[str] = mapped_column(Text)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, url):
        self.engine = create_async_engine(url, echo=False, pool_pre_ping=True, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def transaction(self):
        return self.sessions.begin()

    async def close(self):
        await self.engine.dispose()


async def lock(session, namespace: str, key: str, wait=True):
    # Transaction-scoped locks automatically release on crash/rollback; consistent ordering.
    function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    return await session.scalar(
        text(f"SELECT {function}(hashtextextended(:key, 0))"), {"key": namespace + ":" + key}
    )


async def release_session_lock(connection, key):
    """Never return a session-locked or aborted connection to the pool."""
    try:
        await connection.rollback()
        await connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {"key": key}
        )
        await connection.commit()
    except BaseException:
        await connection.invalidate()
        raise
