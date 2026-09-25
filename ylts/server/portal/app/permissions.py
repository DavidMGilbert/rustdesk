"""Access rules: who can reach which devices, and password rotation when access shrinks."""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import Device, Grant, User, audit, utcnow
from .security import Crypto, generate_device_password


def _user_grants(s: Session, user: User, now: Optional[dt.datetime] = None) -> list[Grant]:
    now = now or utcnow()
    team_ids = [t.id for t in user.teams]
    conds = [Grant.user_id == user.id]
    if team_ids:
        conds.append(Grant.team_id.in_(team_ids))
    grants = s.scalars(select(Grant).where(or_(*conds))).all()
    return [g for g in grants if g.active(now)]


def accessible_devices(s: Session, user: User) -> list[Device]:
    """Devices the user may connect to. Disabled users and devices never match."""
    if not user.active:
        return []
    q = select(Device).where(Device.disabled.is_(False))
    if user.is_admin:
        return list(s.scalars(q.order_by(Device.hostname)).all())
    grants = _user_grants(s, user)
    device_ids = {g.device_id for g in grants if g.device_id}
    client_ids = {g.client_id for g in grants if g.client_id}
    if not device_ids and not client_ids:
        return []
    conds = []
    if device_ids:
        conds.append(Device.id.in_(device_ids))
    if client_ids:
        conds.append(Device.client_id.in_(client_ids))
    return list(s.scalars(q.where(or_(*conds)).order_by(Device.hostname)).all())


def can_access(s: Session, user: User, device: Device) -> bool:
    return any(d.id == device.id for d in accessible_devices(s, user))


def devices_covered_by_grant(s: Session, grant: Grant) -> list[Device]:
    if grant.device_id:
        d = s.get(Device, grant.device_id)
        return [d] if d else []
    if grant.client_id:
        return list(s.scalars(select(Device).where(Device.client_id == grant.client_id)).all())
    return []


def request_password_rotation(s: Session, crypto: Crypto, device: Device, reason: str, actor: str = "system") -> None:
    """Queue a new password for the device. The agent applies it on its next check-in and
    confirms; until then the previous password stays in the technicians' address books."""
    device.pending_version = max(device.password_version, device.pending_version) + 1
    device.pending_password_enc = crypto.encrypt(generate_device_password())
    audit(s, "password.rotate_requested", actor=actor, device=device.rustdesk_id, detail=reason)


def rotate_devices(s: Session, crypto: Crypto, devices: Iterable[Device], reason: str, actor: str = "system") -> int:
    seen: set[int] = set()
    for d in devices:
        if d.id in seen:
            continue
        seen.add(d.id)
        request_password_rotation(s, crypto, d, reason, actor)
    return len(seen)


AccessMap = dict[int, set[int]]


def access_map(s: Session) -> AccessMap:
    """user id -> ids of devices that user can reach right now."""
    s.flush()
    return {u.id: {d.id for d in accessible_devices(s, u)} for u in s.scalars(select(User)).all()}


def rotate_lost_access(s: Session, crypto: Crypto, before: AccessMap, reason: str, actor: str = "system") -> int:
    """Compare against a snapshot taken before a change. Any device that at least one person
    can no longer reach gets a new password, so a copy of the old one stops working."""
    after = access_map(s)
    lost: set[int] = set()
    for uid, devs in before.items():
        lost |= devs - after.get(uid, set())
    if not lost:
        return 0
    devices = s.scalars(select(Device).where(Device.id.in_(lost))).all()
    return rotate_devices(s, crypto, devices, reason, actor)


def expire_grants(s: Session, crypto: Crypto) -> int:
    """Delete grants past their expiry and rotate passwords of devices someone lost access to."""
    now = utcnow()
    expired = s.scalars(select(Grant).where(Grant.expires_at.is_not(None), Grant.expires_at <= now)).all()
    if not expired:
        return 0
    # Snapshot while treating the expired grants as still active.
    before: AccessMap = {}
    for u in s.scalars(select(User)).all():
        devs = {d.id for d in accessible_devices(s, u)}
        team_ids = {t.id for t in u.teams}
        for g in expired:
            if g.user_id == u.id or (g.team_id and g.team_id in team_ids):
                if u.active:
                    devs |= {d.id for d in devices_covered_by_grant(s, g) if not d.disabled}
        before[u.id] = devs
    for g in expired:
        audit(s, "grant.expired", detail={"grant": g.id, "user": g.user_id, "team": g.team_id,
                                          "client": g.client_id, "device": g.device_id})
        s.delete(g)
    rotate_lost_access(s, crypto, before, "access grant expired")
    return len(expired)
