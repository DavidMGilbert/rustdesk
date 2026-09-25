"""Database models (SQLAlchemy 2.0, SQLite by default)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Iterator, Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class UserTeam(Base):
    __tablename__ = "user_teams"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)


class User(Base):
    """A YLTS staff member (technician or administrator)."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str] = mapped_column(Text, default="")  # empty = 2FA not enabled
    require_2fa: Mapped[bool] = mapped_column(Boolean, default=False)
    personal_ab: Mapped[str] = mapped_column(Text, default='{"peers": [], "tags": []}')
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    teams: Mapped[list["Team"]] = relationship(secondary="user_teams", back_populates="members")

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_secret_enc)


class Team(Base):
    """A group of technicians that can be granted access together."""
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    note: Mapped[str] = mapped_column(Text, default="")
    members: Mapped[list[User]] = relationship(secondary="user_teams", back_populates="teams")


class Client(Base):
    """A YLTS customer (organisation or household). RustDesk calls this a device group."""
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    devices: Mapped[list["Device"]] = relationship(back_populates="client")


class Device(Base):
    """A client PC running the YLTS Remote Host + agent."""
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    rustdesk_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    rustdesk_uuid: Mapped[str] = mapped_column(String(128), default="")  # pinned on first sysinfo
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    alias: Mapped[str] = mapped_column(String(128), default="")
    hostname: Mapped[str] = mapped_column(String(128), default="")
    os_username: Mapped[str] = mapped_column(String(128), default="")
    os: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    tags: Mapped[str] = mapped_column(String(255), default="")  # comma separated
    note: Mapped[str] = mapped_column(Text, default="")
    info_json: Mapped[str] = mapped_column(Text, default="{}")
    agent_token_hash: Mapped[str] = mapped_column(String(128), default="", index=True)
    # Password the host is known to be using (confirmed by the agent)
    password_enc: Mapped[str] = mapped_column(Text, default="")
    password_version: Mapped[int] = mapped_column(Integer, default=0)
    # Password the agent has been asked to apply but hasn't confirmed yet
    pending_password_enc: Mapped[str] = mapped_column(Text, default="")
    pending_version: Mapped[int] = mapped_column(Integer, default=0)
    password_set_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_by_user: Mapped[bool] = mapped_column(Boolean, default=False)  # reported by tray
    active_conns: Mapped[str] = mapped_column(String(255), default="[]")
    disconnect_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    enrolled_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)  # hbbs heartbeat
    agent_last_seen: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    client: Mapped[Optional[Client]] = relationship(back_populates="devices")

    @property
    def label(self) -> str:
        return self.alias or self.hostname or self.rustdesk_id

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def info(self) -> dict[str, Any]:
        try:
            return json.loads(self.info_json or "{}")
        except ValueError:
            return {}

    @property
    def conns(self) -> list[int]:
        try:
            return [int(c) for c in json.loads(self.active_conns or "[]")]
        except (ValueError, TypeError):
            return []

    def online(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or utcnow()
        seen = max([t for t in (self.last_seen, self.agent_last_seen) if t] or [dt.datetime.min])
        return (now - seen) < dt.timedelta(minutes=3)


class Grant(Base):
    """Permission for a technician or team to reach a client or a single device."""
    __tablename__ = "grants"
    __table_args__ = (UniqueConstraint("user_id", "team_id", "client_id", "device_id", name="uq_grant"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped[Optional[User]] = relationship()
    team: Mapped[Optional[Team]] = relationship()
    client: Mapped[Optional[Client]] = relationship()
    device: Mapped[Optional[Device]] = relationship()

    def active(self, now: Optional[dt.datetime] = None) -> bool:
        return self.expires_at is None or self.expires_at > (now or utcnow())


class AccessToken(Base):
    """Login token issued to the technician RustDesk app."""
    __tablename__ = "access_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    client_rustdesk_id: Mapped[str] = mapped_column(String(32), default="")
    client_uuid: Mapped[str] = mapped_column(String(128), default="")
    device_info: Mapped[str] = mapped_column(Text, default="{}")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class PendingTfa(Base):
    """Short-lived challenge between password check and TOTP check."""
    __tablename__ = "pending_tfa"
    id: Mapped[int] = mapped_column(primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class EnrolmentToken(Base):
    """Token baked into a host installer so new PCs join the right client automatically."""
    __tablename__ = "enrolment_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(16), default="")
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    uses_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = unlimited
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    client: Mapped[Optional[Client]] = relationship()

    def usable(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or utcnow()
        if self.revoked:
            return False
        if self.expires_at and self.expires_at <= now:
            return False
        if self.uses_left is not None and self.uses_left <= 0:
            return False
        return True


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="")
    device_rustdesk_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def audit(s: Session, kind: str, *, actor: str = "", device: str = "", ip: str = "", detail: Any = "") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    s.add(AuditEvent(kind=kind, actor=actor, device_rustdesk_id=device, ip=ip, detail=detail[:4000]))
