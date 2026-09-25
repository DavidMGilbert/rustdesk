"""API used by the YLTS agent service installed on client PCs."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import Device, EnrolmentToken, audit, utcnow
from .deps import bearer_token, client_ip, get_crypto, get_db
from .security import Crypto, generate_device_password, new_token, token_hash

router = APIRouter(prefix="/agent")

CHECKIN_SECONDS = 30
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


class EnrolIn(BaseModel):
    enrol_token: str = Field(max_length=200)
    rustdesk_id: str = Field(max_length=32)
    hostname: str = Field(default="", max_length=128)
    os: str = Field(default="", max_length=128)
    os_username: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=32)


class CheckinIn(BaseModel):
    rustdesk_id: str = Field(default="", max_length=32)
    hostname: str = Field(default="", max_length=128)
    os_username: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=32)
    applied_version: int = 0  # password version the agent last applied successfully
    approval_required: bool = False  # client ticked "ask me before a technician connects"


class AckIn(BaseModel):
    version: int
    ok: bool
    error: str = Field(default="", max_length=500)


class HelpIn(BaseModel):
    message: str = Field(default="", max_length=2000)
    contact: str = Field(default="", max_length=200)


def _deny(msg: str, status: int = 401) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _agent_device(request: Request, s: Session) -> Optional[Device]:
    tok = bearer_token(request)
    if not tok:
        return None
    return s.scalar(select(Device).where(Device.agent_token_hash == token_hash(tok)))


def _client_config(d: Device) -> dict[str, Any]:
    return {
        "company": settings.company_name,
        "support_phone": settings.support_phone,
        "support_email": settings.support_email,
        "support_url": settings.support_url,
        "client_name": d.client.name if d.client else "",
        "device_label": d.label,
    }


def _password_cmd(d: Device, crypto: Crypto, reported_version: int) -> Optional[dict[str, Any]]:
    """Decide whether the agent must (re)apply a password this check-in."""
    if d.pending_password_enc:
        return {"password": crypto.decrypt(d.pending_password_enc), "version": d.pending_version}
    if not d.password_enc:
        return None
    stale = d.password_set_at is None or (utcnow() - d.password_set_at) > dt.timedelta(
        hours=settings.password_reassert_hours)
    if reported_version != d.password_version or stale:
        # Re-assert the current password: covers reinstalls and local changes on the PC.
        return {"password": crypto.decrypt(d.password_enc), "version": d.password_version}
    return None


@router.post("/enrol")
def enrol(body: EnrolIn, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto)):
    ip = client_ip(request)
    et = s.scalar(select(EnrolmentToken).where(EnrolmentToken.token_hash == token_hash(body.enrol_token.strip())))
    if not et or not et.usable():
        audit(s, "enrol.denied", ip=ip, device=body.rustdesk_id, detail="invalid or expired enrolment token")
        return _deny("Enrolment token is invalid or expired", 403)
    if not _ID_RE.match(body.rustdesk_id):
        return _deny("Invalid RustDesk ID", 400)

    d = s.scalar(select(Device).where(Device.rustdesk_id == body.rustdesk_id))
    created = d is None
    if d is None:
        d = Device(rustdesk_id=body.rustdesk_id, client_id=et.client_id, password_version=0, pending_version=0,
                   disabled=False, paused_by_user=False)
        s.add(d)
    elif d.disabled:
        return _deny("This device has been disabled by YLTS", 403)
    d.hostname = body.hostname or d.hostname
    d.os = body.os or d.os
    d.os_username = body.os_username or d.os_username
    d.agent_version = body.agent_version
    d.agent_last_seen = utcnow()
    d.rustdesk_uuid = ""  # re-pin on the next sysinfo from this install
    device_token = new_token()
    d.agent_token_hash = token_hash(device_token)
    # Fresh password on every enrolment; it becomes current when the agent confirms it.
    d.pending_version = max(d.password_version, d.pending_version) + 1
    d.pending_password_enc = crypto.encrypt(generate_device_password())
    if et.uses_left is not None:
        et.uses_left -= 1
    s.flush()
    audit(s, "enrol.ok", ip=ip, device=d.rustdesk_id,
          detail={"new": created, "token": et.label or et.token_hint, "client": et.client.name if et.client else ""})
    return {
        "device_token": device_token,
        "set_password": {"password": crypto.decrypt(d.pending_password_enc), "version": d.pending_version},
        "checkin_seconds": CHECKIN_SECONDS,
        "config": _client_config(d),
    }


@router.post("/checkin")
def checkin(body: CheckinIn, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto)):
    d = _agent_device(request, s)
    if d is None:
        return _deny("Unknown device token")
    if d.disabled:
        return _deny("This device has been disabled by YLTS", 403)
    now = utcnow()
    d.agent_last_seen = now
    d.agent_version = body.agent_version or d.agent_version
    if body.hostname:
        d.hostname = body.hostname
    if body.os_username:
        d.os_username = body.os_username
    if body.approval_required != d.paused_by_user:
        audit(s, "device.approval_mode", device=d.rustdesk_id,
              detail="client requires approval" if body.approval_required else "unattended access allowed")
        d.paused_by_user = body.approval_required
    if body.rustdesk_id and body.rustdesk_id != d.rustdesk_id and _ID_RE.match(body.rustdesk_id):
        clash = s.scalar(select(Device).where(Device.rustdesk_id == body.rustdesk_id))
        if clash is None:
            audit(s, "device.id_changed", device=body.rustdesk_id, detail={"old": d.rustdesk_id})
            d.rustdesk_id = body.rustdesk_id
            d.rustdesk_uuid = ""
    live = d.last_seen is not None and (now - d.last_seen) < dt.timedelta(minutes=2)
    resp: dict[str, Any] = {"checkin_seconds": CHECKIN_SECONDS, "config": _client_config(d),
                            "sessions": len(d.conns) if live else 0}
    cmd = _password_cmd(d, crypto, body.applied_version)
    if cmd:
        resp["set_password"] = cmd
    return resp


@router.post("/ack")
def ack(body: AckIn, request: Request, s: Session = Depends(get_db)):
    d = _agent_device(request, s)
    if d is None:
        return _deny("Unknown device token")
    if not body.ok:
        audit(s, "password.apply_failed", device=d.rustdesk_id, detail=body.error)
        return {"ok": True}
    if d.pending_password_enc and body.version == d.pending_version:
        d.password_enc = d.pending_password_enc
        d.password_version = d.pending_version
        d.pending_password_enc = ""
        d.password_set_at = utcnow()
        audit(s, "password.rotated", device=d.rustdesk_id, detail={"version": d.password_version})
    elif body.version == d.password_version:
        d.password_set_at = utcnow()
    return {"ok": True}


@router.post("/help")
def help_request(body: HelpIn, request: Request, s: Session = Depends(get_db)):
    d = _agent_device(request, s)
    if d is None:
        return _deny("Unknown device token")
    audit(s, "help.requested", device=d.rustdesk_id, ip=client_ip(request),
          detail={"message": body.message, "contact": body.contact, "user": d.os_username,
                  "client": d.client.name if d.client else ""})
    return {"ok": True}
