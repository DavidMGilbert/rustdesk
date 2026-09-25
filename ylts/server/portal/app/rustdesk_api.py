"""The HTTP API that RustDesk clients talk to (the "API server" setting).

Implements the subset of the RustDesk Server Pro API used by the open-source client:
login/logout, current user, address books, the accessible-devices panel, and the
heartbeat / sysinfo / audit calls made by hosts.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from collections import defaultdict, deque
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import AccessToken, Client, Device, PendingTfa, User, audit, utcnow
from .deps import api_user, client_ip, get_crypto, get_db
from .permissions import accessible_devices
from .security import DUMMY_HASH, Crypto, new_token, token_hash, verify_password, verify_totp

router = APIRouter(prefix="/api")

OK = Response(status_code=200)  # RustDesk treats "200 + empty body" as success for edits


def err(message: str, status: int = 200) -> JSONResponse:
    # The client reads {"error": ...}; many calls expect HTTP 200 with the error in the body.
    return JSONResponse({"error": message}, status_code=status)


async def body_json(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


# --------------------------------------------------------------------------- login throttling

class Throttle:
    """In-memory failure counter: 5 failures per key in 15 minutes locks that key for 15 minutes."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self.fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str) -> deque[float]:
        q = self.fails[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._trim(k)) >= self.limit for k in keys)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        for k in keys:
            self._trim(k).append(now)

    def reset(self, *keys: str) -> None:
        for k in keys:
            self.fails.pop(k, None)


throttle = Throttle()


def user_payload(u: User) -> dict[str, Any]:
    return {
        "name": u.username,
        "display_name": u.display_name,
        "email": u.email,
        "note": "",
        "status": 1 if u.active else 0,
        "is_admin": bool(u.is_admin),
        "avatar": "",
    }


def _issue_token(s: Session, u: User, body: dict, ip: str) -> dict[str, Any]:
    tok = new_token()
    now = utcnow()
    s.add(AccessToken(
        token_hash=token_hash(tok), user_id=u.id,
        client_rustdesk_id=str(body.get("id") or "")[:32], client_uuid=str(body.get("uuid") or "")[:128],
        device_info=json.dumps(body.get("deviceInfo") or {})[:2000], ip=ip,
        created_at=now, last_used=now, expires_at=now + dt.timedelta(days=settings.token_days),
    ))
    u.last_login = now
    audit(s, "login.ok", actor=u.username, ip=ip, detail={"client_id": body.get("id"), "device": body.get("deviceInfo")})
    return {"access_token": tok, "type": "access_token", "user": user_payload(u)}


@router.get("/login-options")
def login_options() -> list[str]:
    return []  # no OIDC providers; username + password (+ TOTP)


@router.post("/login")
async def login(request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto)):
    body = await body_json(request)
    if not isinstance(body, dict):
        return err("Bad request", 400)
    ip = client_ip(request)
    username = str(body.get("username") or "").strip().lower()
    keys = (f"ip:{ip}", f"user:{username}")
    if throttle.blocked(*keys):
        return err("Too many failed attempts. Try again in 15 minutes.")

    # Step 2: TOTP code for a pending challenge
    if body.get("secret") and (body.get("tfaCode") or body.get("verificationCode")):
        pending = s.scalar(select(PendingTfa).where(PendingTfa.secret_hash == token_hash(str(body["secret"]))))
        if not pending or pending.expires_at <= utcnow():
            return err("Verification expired, please sign in again.")
        u = s.get(User, pending.user_id)
        code = str(body.get("tfaCode") or body.get("verificationCode") or "")
        if not u or not u.active or not verify_totp(crypto.decrypt(u.totp_secret_enc), code):
            pending.attempts += 1
            throttle.fail(*keys)
            if pending.attempts >= 5:
                s.delete(pending)
            audit(s, "login.tfa_failed", actor=username, ip=ip)
            return err("Invalid verification code")
        s.delete(pending)
        throttle.reset(*keys)
        return _issue_token(s, u, body, ip)

    # Step 1: username + password
    password = str(body.get("password") or "")
    u = s.scalar(select(User).where(User.username == username)) if username else None
    ok = verify_password(password, u.password_hash if u else DUMMY_HASH)
    if not u or not ok or not u.active:
        throttle.fail(*keys)
        audit(s, "login.failed", actor=username, ip=ip, detail={"reason": "disabled" if (u and ok) else "bad credentials"})
        return err("Wrong username or password")
    if u.require_2fa and not u.has_2fa:
        return err("Two-factor authentication must be set up in the YLTS Portal before signing in.")
    if u.has_2fa:
        secret = new_token(24)
        s.add(PendingTfa(secret_hash=token_hash(secret), user_id=u.id,
                         expires_at=utcnow() + dt.timedelta(minutes=5)))
        return {"type": "email_check", "tfa_type": "tfa_check", "secret": secret,
                "user": {"name": u.username}}
    throttle.reset(*keys)
    return _issue_token(s, u, body, ip)


@router.post("/currentUser")
def current_user(user: User = Depends(api_user)):
    return user_payload(user)


@router.post("/logout")
def logout(request: Request, s: Session = Depends(get_db), user: User = Depends(api_user)):
    tok = getattr(request.state, "access_token", None)
    if tok is not None:
        s.delete(s.get(AccessToken, tok.id))
        audit(s, "logout", actor=user.username, ip=client_ip(request))
    return OK


# --------------------------------------------------------------------------- address books

def _page(request: Request, items: list) -> dict[str, Any]:
    try:
        current = max(1, int(request.query_params.get("current", "1")))
        size = min(1000, max(1, int(request.query_params.get("pageSize", "100"))))
    except ValueError:
        current, size = 1, 100
    start = (current - 1) * size
    return {"total": len(items), "data": items[start:start + size]}


def _client_guid(client_id: Optional[int]) -> str:
    return f"client-{client_id}" if client_id else "unassigned"


def _grouped(devices: list[Device]) -> dict[str, list[Device]]:
    out: dict[str, list[Device]] = defaultdict(list)
    for d in devices:
        out[_client_guid(d.client_id)].append(d)
    return out


def _platform(os_str: str) -> str:
    o = (os_str or "").lower()
    if "windows" in o:
        return "Windows"
    if "mac" in o:
        return "Mac OS"
    if "linux" in o:
        return "Linux"
    if "android" in o:
        return "Android"
    return ""


def _shared_peer(d: Device, crypto: Crypto) -> dict[str, Any]:
    peer = {
        "id": d.rustdesk_id,
        "hash": "",
        "username": d.os_username,
        "hostname": d.hostname,
        "platform": _platform(d.os) or "Windows",
        "alias": d.label,
        "tags": d.tag_list,
        "note": d.note,
    }
    if d.paused_by_user:
        peer["note"] = ("The client has asked to approve each session on screen. " + d.note).strip()
    pw = crypto.decrypt(d.password_enc)
    if pw:
        peer["password"] = pw
    return peer


def _personal_guid(u: User) -> str:
    return f"personal-{u.id}"


def _load_personal(u: User) -> dict[str, Any]:
    try:
        data = json.loads(u.personal_ab or "{}")
    except ValueError:
        data = {}
    data.setdefault("peers", [])
    data.setdefault("tags", [])
    return data


def _save_personal(u: User, data: dict[str, Any]) -> None:
    u.personal_ab = json.dumps(data)


@router.post("/ab/settings")
def ab_settings(user: User = Depends(api_user)):
    return {"max_peer_one_ab": 0}


@router.post("/ab/personal")
def ab_personal(user: User = Depends(api_user)):
    return {"guid": _personal_guid(user)}


@router.post("/ab/shared/profiles")
def ab_shared_profiles(request: Request, s: Session = Depends(get_db), user: User = Depends(api_user)):
    groups = _grouped(accessible_devices(s, user))
    profiles = []
    for guid in groups:
        if guid == "unassigned":
            name, note = "Unassigned devices", "Devices not yet assigned to a client"
        else:
            c = s.get(Client, int(guid.split("-", 1)[1]))
            name, note = (c.name, c.note) if c else (guid, "")
        profiles.append({"guid": guid, "name": name, "owner": settings.company_name,
                         "note": note, "rule": 1, "info": {}})  # rule 1 = read-only
    profiles.sort(key=lambda p: p["name"].lower())
    return _page(request, profiles)


@router.post("/ab/peers")
def ab_peers(request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
             user: User = Depends(api_user)):
    guid = request.query_params.get("ab", "")
    if guid == _personal_guid(user):
        return _page(request, _load_personal(user)["peers"])
    devices = _grouped(accessible_devices(s, user)).get(guid, [])
    return _page(request, [_shared_peer(d, crypto) for d in devices])


@router.post("/ab/tags/{guid}")
def ab_tags(guid: str, s: Session = Depends(get_db), user: User = Depends(api_user)):
    if guid == _personal_guid(user):
        return _load_personal(user)["tags"]
    names = sorted({t for d in _grouped(accessible_devices(s, user)).get(guid, []) for t in d.tag_list})
    return [{"name": n, "color": _tag_color(n)} for n in names]


def _tag_color(name: str) -> int:
    palette = [0xFF1565C0, 0xFF2E7D32, 0xFFEF6C00, 0xFF6A1B9A, 0xFFC62828, 0xFF00838F, 0xFF4E342E]
    return palette[sum(map(ord, name)) % len(palette)]


def _personal_or_error(guid: str, user: User) -> Optional[JSONResponse]:
    if guid != _personal_guid(user):
        return err("This address book is managed by YLTS and is read-only")
    return None


_PEER_FIELDS = {"id", "hash", "password", "username", "hostname", "platform", "alias", "tags", "note",
                "forceAlwaysRelay", "rdpPort", "rdpUsername", "loginName", "same_server"}


@router.post("/ab/peer/add/{guid}")
async def ab_peer_add(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    p = await body_json(request)
    if not isinstance(p, dict) or not p.get("id"):
        return err("Missing id")
    data = _load_personal(user)
    p = {k: v for k, v in p.items() if k in _PEER_FIELDS}
    data["peers"] = [x for x in data["peers"] if x.get("id") != p["id"]] + [p]
    _save_personal(user, data)
    return OK


@router.put("/ab/peer/update/{guid}")
async def ab_peer_update(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    p = await body_json(request)
    if not isinstance(p, dict) or not p.get("id"):
        return err("Missing id")
    data = _load_personal(user)
    for x in data["peers"]:
        if x.get("id") == p["id"]:
            x.update({k: v for k, v in p.items() if k in _PEER_FIELDS})
            break
    else:
        return err("Peer not found")
    _save_personal(user, data)
    return OK


@router.delete("/ab/peer/{guid}")
async def ab_peer_delete(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    ids = await body_json(request)
    ids = set(ids) if isinstance(ids, list) else set()
    data = _load_personal(user)
    data["peers"] = [x for x in data["peers"] if x.get("id") not in ids]
    _save_personal(user, data)
    return OK


@router.post("/ab/tag/add/{guid}")
async def ab_tag_add(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    t = await body_json(request)
    if not isinstance(t, dict) or not t.get("name"):
        return err("Missing name")
    data = _load_personal(user)
    data["tags"] = [x for x in data["tags"] if x.get("name") != t["name"]]
    data["tags"].append({"name": str(t["name"]), "color": int(t.get("color") or 0)})
    _save_personal(user, data)
    return OK


@router.put("/ab/tag/rename/{guid}")
async def ab_tag_rename(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    b = await body_json(request)
    old, new = str(b.get("old", "")), str(b.get("new", ""))
    if not old or not new:
        return err("Missing tag name")
    data = _load_personal(user)
    for t in data["tags"]:
        if t.get("name") == old:
            t["name"] = new
    for p in data["peers"]:
        p["tags"] = [new if x == old else x for x in p.get("tags", [])]
    _save_personal(user, data)
    return OK


@router.put("/ab/tag/update/{guid}")
async def ab_tag_update(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    b = await body_json(request)
    data = _load_personal(user)
    for t in data["tags"]:
        if t.get("name") == b.get("name"):
            t["color"] = int(b.get("color") or 0)
    _save_personal(user, data)
    return OK


@router.delete("/ab/tag/{guid}")
async def ab_tag_delete(guid: str, request: Request, user: User = Depends(api_user)):
    if (e := _personal_or_error(guid, user)):
        return e
    names = await body_json(request)
    names = set(names) if isinstance(names, list) else set()
    data = _load_personal(user)
    data["tags"] = [t for t in data["tags"] if t.get("name") not in names]
    for p in data["peers"]:
        p["tags"] = [x for x in p.get("tags", []) if x not in names]
    _save_personal(user, data)
    return OK


# --------------------------------------------------------------------------- accessible devices panel

@router.get("/users")
def list_users(request: Request, user: User = Depends(api_user)):
    # Only reveal yourself; the panel is used to browse devices, not staff.
    return _page(request, [user_payload(user)])


@router.get("/peers")
def list_peers(request: Request, s: Session = Depends(get_db), user: User = Depends(api_user)):
    items = []
    for d in accessible_devices(s, user):
        items.append({
            "id": d.rustdesk_id,
            "info": {"username": d.os_username, "os": d.os, "device_name": d.hostname},
            "status": 1,
            "user": "", "user_name": "",
            "device_group_name": d.client.name if d.client else "Unassigned devices",
            "note": d.note,
        })
    return _page(request, items)


@router.get("/device-group/accessible")
def device_groups(request: Request, s: Session = Depends(get_db), user: User = Depends(api_user)):
    names = sorted({d.client.name if d.client else "Unassigned devices" for d in accessible_devices(s, user)})
    return _page(request, [{"name": n} for n in names])


# --------------------------------------------------------------------------- host-side calls

def _device_for_host(s: Session, body: dict, request: Request) -> Optional[Device]:
    """Find the enrolled device for a heartbeat/sysinfo/audit call and pin its RustDesk UUID."""
    rid = str(body.get("id") or "")
    uuid = str(body.get("uuid") or "")
    if not rid:
        return None
    d = s.scalar(select(Device).where(Device.rustdesk_id == rid))
    if not d:
        return None
    if d.rustdesk_uuid and uuid and d.rustdesk_uuid != uuid:
        audit(s, "security.uuid_mismatch", device=rid, ip=client_ip(request),
              detail="A machine reported this ID with a different hardware UUID; ignored.")
        return None
    if not d.rustdesk_uuid and uuid:
        d.rustdesk_uuid = uuid
    return d


@router.post("/heartbeat")
async def heartbeat(request: Request, s: Session = Depends(get_db)):
    body = await body_json(request)
    if not isinstance(body, dict):
        return {}
    d = _device_for_host(s, body, request)
    resp: dict[str, Any] = {"modified_at": 0}
    if not d:
        return resp
    d.last_seen = utcnow()
    conns = [int(c) for c in body.get("conns") or [] if str(c).lstrip("-").isdigit()]
    d.active_conns = json.dumps(conns)
    if d.disabled and conns:
        resp["disconnect"] = conns
    elif d.disconnect_requested:
        if conns:
            resp["disconnect"] = conns
            audit(s, "session.disconnect_sent", device=d.rustdesk_id, detail={"conns": conns})
        d.disconnect_requested = False
    if not d.hostname:
        resp["sysinfo"] = True
    return resp


@router.post("/sysinfo")
async def sysinfo(request: Request, s: Session = Depends(get_db)):
    body = await body_json(request)
    if isinstance(body, dict):
        d = _device_for_host(s, body, request)
        if d:
            d.hostname = str(body.get("hostname") or d.hostname)[:128]
            d.os_username = str(body.get("username") or d.os_username)[:128]
            d.os = str(body.get("os") or d.os)[:128]
            d.version = str(body.get("version") or d.version)[:32]
            info = {k: v for k, v in body.items() if k not in ("id", "uuid")}
            d.info_json = json.dumps(info)[:8000]
            d.last_seen = utcnow()
    return PlainTextResponse("SYSINFO_UPDATED")


@router.post("/sysinfo_ver")
def sysinfo_ver():
    return PlainTextResponse("1")


@router.post("/audit/{kind}")
async def audit_event(kind: str, request: Request, s: Session = Depends(get_db)):
    body = await body_json(request)
    if not isinstance(body, dict) or kind not in ("conn", "file", "alarm"):
        return OK
    d = _device_for_host(s, body, request)
    if not d:
        return OK
    action = str(body.get("action") or body.get("typ") or "")
    detail = {k: v for k, v in body.items() if k not in ("uuid", "nonce")}
    audit(s, f"{kind}.{action}" if action else kind, device=d.rustdesk_id,
          ip=str(body.get("ip") or client_ip(request)), detail=detail)
    return OK


@router.get("/ab")
def legacy_ab(user: User = Depends(api_user)):
    # Only called by clients that don't know /api/ab/personal; send them an empty book.
    return PlainTextResponse("null")
