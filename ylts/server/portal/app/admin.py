"""YLTS Portal web UI: technicians, clients, devices, access grants, enrolment and audit."""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import (
    AccessToken, AuditEvent, Client, Device, EnrolmentToken, Grant, Team, User, audit, utcnow,
)
from .deps import client_ip, get_crypto, get_db
from .permissions import access_map, accessible_devices, request_password_rotation, rotate_lost_access
from .rustdesk_api import throttle
from .security import (
    DUMMY_HASH, Crypto, hash_password, new_token, new_totp_secret, password_policy_error, token_hash,
    totp_uri, verify_password, verify_totp,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SYDNEY = dt.timezone(dt.timedelta(hours=10))  # display only; stored times are UTC


def _local(value: Optional[dt.datetime]) -> str:
    if not value:
        return "—"
    return value.replace(tzinfo=dt.timezone.utc).astimezone().strftime("%d %b %Y %H:%M")


def _ago(value: Optional[dt.datetime]) -> str:
    if not value:
        return "never"
    secs = int((utcnow() - value).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


templates.env.filters["local"] = _local
templates.env.filters["ago"] = _ago
templates.env.globals["settings"] = settings


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class NeedLogin(Exception):
    pass


# --------------------------------------------------------------------------- session helpers

def current_user(request: Request, s: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise NeedLogin()
    u = s.get(User, uid)
    if not u or not u.active or request.session.get("pwv") != u.password_hash[-12:]:
        request.session.clear()
        raise NeedLogin()
    request.state.user = u
    return u


def admin_user(u: User = Depends(current_user)) -> User:
    if not u.is_admin:
        raise HTTPException(403, "Administrator access required")
    return u


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def check_csrf(request: Request) -> None:
    if request.method == "POST":
        form = await request.form()
        sent = str(form.get("csrf", ""))
        if not sent or not secrets.compare_digest(sent, request.session.get("csrf", "")):
            raise HTTPException(400, "Form expired - go back, refresh the page and try again.")


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["csrf"] = csrf_token(request)
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _parse_expiry(choice: str, custom: str) -> Optional[dt.datetime]:
    now = utcnow()
    table = {"1h": dt.timedelta(hours=1), "4h": dt.timedelta(hours=4), "1d": dt.timedelta(days=1),
             "7d": dt.timedelta(days=7), "30d": dt.timedelta(days=30)}
    if choice in table:
        return now + table[choice]
    if choice == "custom" and custom:
        # <input type=datetime-local> in the admin's local time
        local = dt.datetime.fromisoformat(custom)
        if local.tzinfo is None:
            local = local.astimezone()
        return local.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return None


# --------------------------------------------------------------------------- login / account

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", step=request.session.get("tfa_uid") and "tfa" or "password")


@router.post("/login", dependencies=[Depends(check_csrf)])
def login_submit(request: Request, username: str = Form(""), password: str = Form(""), code: str = Form(""),
                 s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto)):
    ip = client_ip(request)
    tfa_uid = request.session.get("tfa_uid")
    if tfa_uid:
        u = s.get(User, tfa_uid)
        keys = (f"ip:{ip}", f"user:{u.username if u else ''}")
        if throttle.blocked(*keys):
            flash(request, "Too many attempts. Try again in 15 minutes.", "err")
            return back("/login")
        if u and u.active and verify_totp(crypto.decrypt(u.totp_secret_enc), code):
            request.session.pop("tfa_uid", None)
            return _finish_login(request, s, u, ip)
        throttle.fail(*keys)
        flash(request, "That code didn't work. Check your authenticator app's clock and try again.", "err")
        return back("/login")

    username = username.strip().lower()
    keys = (f"ip:{ip}", f"user:{username}")
    if throttle.blocked(*keys):
        flash(request, "Too many attempts. Try again in 15 minutes.", "err")
        return back("/login")
    u = s.scalar(select(User).where(User.username == username))
    ok = verify_password(password, u.password_hash if u else DUMMY_HASH)
    if not u or not ok or not u.active:
        throttle.fail(*keys)
        audit(s, "portal.login_failed", actor=username, ip=ip)
        flash(request, "Wrong username or password.", "err")
        return back("/login")
    if u.has_2fa:
        request.session["tfa_uid"] = u.id
        return back("/login")
    return _finish_login(request, s, u, ip)


def _finish_login(request: Request, s: Session, u: User, ip: str) -> RedirectResponse:
    throttle.reset(f"ip:{ip}", f"user:{u.username}")
    request.session.clear()
    request.session["uid"] = u.id
    request.session["pwv"] = u.password_hash[-12:]
    u.last_login = utcnow()
    audit(s, "portal.login", actor=u.username, ip=ip)
    if u.require_2fa and not u.has_2fa:
        flash(request, "Please set up two-factor authentication to continue.", "warn")
        return back("/account")
    return back("/" if u.is_admin else "/my-devices")


@router.get("/login/cancel")
def login_cancel(request: Request):
    request.session.pop("tfa_uid", None)
    return back("/login")


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request):
    request.session.clear()
    return back("/login")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, u: User = Depends(current_user), crypto: Crypto = Depends(get_crypto)):
    setup_secret = request.session.get("totp_setup")
    qr_svg = ""
    if setup_secret:
        uri = totp_uri(setup_secret, u.username, settings.company_name)
        img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        qr_svg = buf.getvalue().decode()
    return render(request, "account.html", setup_secret=setup_secret, qr_svg=qr_svg)


@router.post("/account/password", dependencies=[Depends(check_csrf)])
def account_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                     s: Session = Depends(get_db), u: User = Depends(current_user)):
    if not verify_password(current, u.password_hash):
        flash(request, "Your current password is wrong.", "err")
    elif new != confirm:
        flash(request, "The new passwords don't match.", "err")
    elif (e := password_policy_error(new)):
        flash(request, e, "err")
    else:
        u.password_hash = hash_password(new)
        request.session["pwv"] = u.password_hash[-12:]
        audit(s, "user.password_changed", actor=u.username, ip=client_ip(request))
        flash(request, "Password changed.")
    return back("/account")


@router.post("/account/2fa/start", dependencies=[Depends(check_csrf)])
def tfa_start(request: Request, u: User = Depends(current_user)):
    request.session["totp_setup"] = new_totp_secret()
    return back("/account")


@router.post("/account/2fa/confirm", dependencies=[Depends(check_csrf)])
def tfa_confirm(request: Request, code: str = Form(""), s: Session = Depends(get_db),
                u: User = Depends(current_user), crypto: Crypto = Depends(get_crypto)):
    secret = request.session.get("totp_setup")
    if not secret or not verify_totp(secret, code):
        flash(request, "That code didn't match. Scan the QR code again and enter the current 6-digit code.", "err")
        return back("/account")
    u.totp_secret_enc = crypto.encrypt(secret)
    request.session.pop("totp_setup", None)
    audit(s, "user.2fa_enabled", actor=u.username, ip=client_ip(request))
    flash(request, "Two-factor authentication is on for the portal and the technician app.")
    return back("/account")


@router.post("/account/2fa/disable", dependencies=[Depends(check_csrf)])
def tfa_disable(request: Request, password: str = Form(""), s: Session = Depends(get_db),
                u: User = Depends(current_user)):
    if u.require_2fa:
        flash(request, "Two-factor authentication is required for your account.", "err")
    elif not verify_password(password, u.password_hash):
        flash(request, "Wrong password.", "err")
    else:
        u.totp_secret_enc = ""
        audit(s, "user.2fa_disabled", actor=u.username, ip=client_ip(request))
        flash(request, "Two-factor authentication turned off.", "warn")
    return back("/account")


@router.get("/my-devices", response_class=HTMLResponse)
def my_devices(request: Request, s: Session = Depends(get_db), u: User = Depends(current_user)):
    return render(request, "my_devices.html", devices=accessible_devices(s, u), now=utcnow())


# --------------------------------------------------------------------------- dashboard

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_db), u: User = Depends(current_user)):
    if not u.is_admin:
        return back("/my-devices")
    devices = s.scalars(select(Device)).all()
    now = utcnow()
    help_reqs = s.scalars(select(AuditEvent).where(AuditEvent.kind == "help.requested")
                          .order_by(AuditEvent.id.desc()).limit(8)).all()
    recent = s.scalars(select(AuditEvent).where(AuditEvent.kind.not_in(["help.requested"]))
                       .order_by(AuditEvent.id.desc()).limit(15)).all()
    stats = {
        "devices": len(devices),
        "online": sum(1 for d in devices if d.online(now)),
        "sessions": sum(len(d.conns) for d in devices if d.online(now)),
        "pending": sum(1 for d in devices if d.pending_password_enc),
        "clients": s.scalar(select(func.count(Client.id))),
        "techs": s.scalar(select(func.count(User.id)).where(User.active.is_(True))),
    }
    names = {d.rustdesk_id: d.label for d in devices}
    return render(request, "dashboard.html", stats=stats, help_reqs=help_reqs, recent=recent,
                  names=names, loads=_loads)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


# --------------------------------------------------------------------------- clients

@router.get("/clients", response_class=HTMLResponse)
def clients(request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    rows = s.scalars(select(Client).order_by(Client.name)).all()
    now = utcnow()
    online = {c.id: sum(1 for d in c.devices if not d.disabled and d.online(now)) for c in rows}
    return render(request, "clients.html", clients=rows, online=online)


@router.post("/clients", dependencies=[Depends(check_csrf)])
def client_create(request: Request, name: str = Form(...), note: str = Form(""), s: Session = Depends(get_db),
                  u: User = Depends(admin_user)):
    name = name.strip()
    if not name or s.scalar(select(Client).where(Client.name == name)):
        flash(request, "A client needs a unique name.", "err")
        return back("/clients")
    c = Client(name=name, note=note.strip())
    s.add(c)
    s.flush()
    audit(s, "client.created", actor=u.username, detail=name)
    flash(request, f"Client {name} added. Next: create an enrolment token for their PCs.")
    return back(f"/clients/{c.id}")


@router.get("/clients/{cid}", response_class=HTMLResponse)
def client_detail(cid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    c = s.get(Client, cid) or _404()
    grants = s.scalars(select(Grant).where(Grant.client_id == cid)).all()
    tokens = s.scalars(select(EnrolmentToken).where(EnrolmentToken.client_id == cid)
                       .order_by(EnrolmentToken.id.desc())).all()
    return render(request, "client_detail.html", c=c, grants=grants, tokens=tokens, now=utcnow(),
                  users=_all_users(s), teams=_all_teams(s), new_token=request.session.pop("new_token", None))


@router.post("/clients/{cid}", dependencies=[Depends(check_csrf)])
def client_update(cid: int, request: Request, name: str = Form(...), note: str = Form(""),
                  s: Session = Depends(get_db), u: User = Depends(admin_user)):
    c = s.get(Client, cid) or _404()
    name = name.strip()
    other = s.scalar(select(Client).where(Client.name == name, Client.id != cid))
    if not name or other:
        flash(request, "A client needs a unique name.", "err")
    else:
        c.name, c.note = name, note.strip()
        audit(s, "client.updated", actor=u.username, detail=name)
        flash(request, "Saved.")
    return back(f"/clients/{cid}")


@router.post("/clients/{cid}/delete", dependencies=[Depends(check_csrf)])
def client_delete(cid: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                  u: User = Depends(admin_user)):
    c = s.get(Client, cid) or _404()
    before = access_map(s)
    for d in list(c.devices):
        d.client_id = None
    s.delete(c)
    rotate_lost_access(s, crypto, before, f"client {c.name} deleted", u.username)
    audit(s, "client.deleted", actor=u.username, detail=c.name)
    flash(request, f"Client {c.name} deleted. Its devices are now unassigned.", "warn")
    return back("/clients")


# --------------------------------------------------------------------------- devices

@router.get("/devices", response_class=HTMLResponse)
def devices(request: Request, q: str = "", client: str = "", s: Session = Depends(get_db),
            u: User = Depends(admin_user)):
    stmt = select(Device).order_by(Device.hostname)
    if client == "none":
        stmt = stmt.where(Device.client_id.is_(None))
    elif client.isdigit():
        stmt = stmt.where(Device.client_id == int(client))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Device.hostname.ilike(like), Device.alias.ilike(like), Device.rustdesk_id.ilike(like),
                              Device.os_username.ilike(like), Device.tags.ilike(like)))
    return render(request, "devices.html", devices=s.scalars(stmt).all(), clients=_all_clients(s), q=q,
                  client=client, now=utcnow())


@router.get("/devices/{did}", response_class=HTMLResponse)
def device_detail(did: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    grants = s.scalars(select(Grant).where(or_(Grant.device_id == did,
                                               Grant.client_id == d.client_id if d.client_id else false()))).all()
    who = [x for x in s.scalars(select(User).where(User.active.is_(True)).order_by(User.username)).all()
           if any(dev.id == d.id for dev in accessible_devices(s, x))]
    events = s.scalars(select(AuditEvent).where(AuditEvent.device_rustdesk_id == d.rustdesk_id)
                       .order_by(AuditEvent.id.desc()).limit(40)).all()
    revealed = request.session.pop("revealed", None)
    return render(request, "device_detail.html", d=d, grants=grants, who=who, events=events, now=utcnow(),
                  clients=_all_clients(s), users=_all_users(s), teams=_all_teams(s), revealed=revealed,
                  loads=_loads)


@router.post("/devices/{did}", dependencies=[Depends(check_csrf)])
def device_update(did: int, request: Request, alias: str = Form(""), tags: str = Form(""), note: str = Form(""),
                  client_id: str = Form(""), s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                  u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    before = access_map(s)
    d.alias, d.note = alias.strip()[:128], note.strip()
    d.tags = ",".join(t.strip() for t in tags.split(",") if t.strip())[:255]
    new_client = int(client_id) if client_id.isdigit() else None
    moved = new_client != d.client_id
    d.client_id = new_client
    if moved:
        n = rotate_lost_access(s, crypto, before, "device moved to another client", u.username)
        if n:
            flash(request, "Device moved. Its password is being changed because some technicians lost access.", "warn")
    audit(s, "device.updated", actor=u.username, device=d.rustdesk_id,
          detail={"alias": d.alias, "tags": d.tags, "client": new_client})
    flash(request, "Saved.")
    return back(f"/devices/{did}")


@router.post("/devices/{did}/rotate", dependencies=[Depends(check_csrf)])
def device_rotate(did: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                  u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    request_password_rotation(s, crypto, d, "manual rotation", u.username)
    flash(request, "New password queued. The PC applies it within about a minute of being online.")
    return back(f"/devices/{did}")


@router.post("/devices/{did}/reveal", dependencies=[Depends(check_csrf)])
def device_reveal(did: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                  u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    request.session["revealed"] = crypto.decrypt(d.password_enc) or "(no confirmed password yet)"
    audit(s, "password.revealed", actor=u.username, device=d.rustdesk_id, ip=client_ip(request))
    return back(f"/devices/{did}")


@router.post("/devices/{did}/disconnect", dependencies=[Depends(check_csrf)])
def device_disconnect(did: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    d.disconnect_requested = True
    audit(s, "session.disconnect_requested", actor=u.username, device=d.rustdesk_id)
    flash(request, "Active sessions will be closed on the next heartbeat (within ~30 seconds).")
    return back(f"/devices/{did}")


@router.post("/devices/{did}/disable", dependencies=[Depends(check_csrf)])
def device_disable(did: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                   u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    d.disabled = not d.disabled
    if d.disabled:
        d.disconnect_requested = True
        request_password_rotation(s, crypto, d, "device disabled", u.username)
    audit(s, "device.disabled" if d.disabled else "device.enabled", actor=u.username, device=d.rustdesk_id)
    flash(request, "Device disabled: hidden from all technicians and its password is being changed."
          if d.disabled else "Device enabled.", "warn" if d.disabled else "ok")
    return back(f"/devices/{did}")


@router.post("/devices/{did}/delete", dependencies=[Depends(check_csrf)])
def device_delete(did: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    d = s.get(Device, did) or _404()
    audit(s, "device.deleted", actor=u.username, device=d.rustdesk_id, detail=d.label)
    s.delete(d)
    flash(request, "Device removed. If the PC is still running the agent, uninstall it or it will stop "
          "checking in (its token no longer works).", "warn")
    return back("/devices")


# --------------------------------------------------------------------------- technicians

@router.get("/technicians", response_class=HTMLResponse)
def technicians(request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    return render(request, "technicians.html", users=_all_users(s, include_inactive=True))


@router.post("/technicians", dependencies=[Depends(check_csrf)])
def technician_create(request: Request, username: str = Form(...), display_name: str = Form(""),
                      email: str = Form(""), password: str = Form(...), is_admin: str = Form(""),
                      require_2fa: str = Form(""), s: Session = Depends(get_db), u: User = Depends(admin_user)):
    username = username.strip().lower()
    if not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
        flash(request, "Usernames can contain letters, numbers, dots, dashes and underscores.", "err")
        return back("/technicians")
    if s.scalar(select(User).where(User.username == username)):
        flash(request, "That username is taken.", "err")
        return back("/technicians")
    if (e := password_policy_error(password)):
        flash(request, e, "err")
        return back("/technicians")
    t = User(username=username, display_name=display_name.strip(), email=email.strip(),
             password_hash=hash_password(password), is_admin=bool(is_admin), require_2fa=bool(require_2fa))
    s.add(t)
    s.flush()
    audit(s, "user.created", actor=u.username, detail={"user": username, "admin": t.is_admin})
    flash(request, f"{t.label} added. Now grant them access to clients or add them to a team.")
    return back(f"/technicians/{t.id}")


@router.get("/technicians/{uid}", response_class=HTMLResponse)
def technician_detail(uid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    grants = s.scalars(select(Grant).where(Grant.user_id == uid)).all()
    team_grants = s.scalars(select(Grant).where(Grant.team_id.in_([x.id for x in t.teams] or [0]))).all()
    tokens = s.scalars(select(AccessToken).where(AccessToken.user_id == uid)
                       .order_by(AccessToken.last_used.desc())).all()
    return render(request, "technician_detail.html", t=t, grants=grants, team_grants=team_grants, tokens=tokens,
                  devices=accessible_devices(s, t), clients=_all_clients(s), teams=_all_teams(s),
                  all_devices=s.scalars(select(Device).order_by(Device.hostname)).all(), now=utcnow(), loads=_loads)


@router.post("/technicians/{uid}", dependencies=[Depends(check_csrf)])
def technician_update(uid: int, request: Request, display_name: str = Form(""), email: str = Form(""),
                      is_admin: str = Form(""), active: str = Form(""), require_2fa: str = Form(""),
                      s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                      u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    if t.id == u.id and (not is_admin or not active):
        flash(request, "You can't remove your own admin rights or disable yourself.", "err")
        return back(f"/technicians/{uid}")
    before = access_map(s)
    t.display_name, t.email = display_name.strip(), email.strip()
    t.is_admin, t.active, t.require_2fa = bool(is_admin), bool(active), bool(require_2fa)
    if not t.active:
        for tok in s.scalars(select(AccessToken).where(AccessToken.user_id == uid)).all():
            s.delete(tok)
    n = rotate_lost_access(s, crypto, before, f"access changed for {t.username}", u.username)
    audit(s, "user.updated", actor=u.username,
          detail={"user": t.username, "admin": t.is_admin, "active": t.active, "require_2fa": t.require_2fa})
    flash(request, "Saved." + (f" Passwords are being changed on {n} device(s) they can no longer reach." if n else ""))
    return back(f"/technicians/{uid}")


@router.post("/technicians/{uid}/password", dependencies=[Depends(check_csrf)])
def technician_password(uid: int, request: Request, password: str = Form(...), s: Session = Depends(get_db),
                        u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    if (e := password_policy_error(password)):
        flash(request, e, "err")
        return back(f"/technicians/{uid}")
    t.password_hash = hash_password(password)
    for tok in s.scalars(select(AccessToken).where(AccessToken.user_id == uid)).all():
        s.delete(tok)
    audit(s, "user.password_reset", actor=u.username, detail=t.username)
    flash(request, "Password reset and all their technician-app sessions signed out.")
    return back(f"/technicians/{uid}")


@router.post("/technicians/{uid}/reset-2fa", dependencies=[Depends(check_csrf)])
def technician_reset_2fa(uid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    t.totp_secret_enc = ""
    audit(s, "user.2fa_reset", actor=u.username, detail=t.username)
    flash(request, "Two-factor authentication removed. They'll need to set it up again.", "warn")
    return back(f"/technicians/{uid}")


@router.post("/technicians/{uid}/signout", dependencies=[Depends(check_csrf)])
def technician_signout(uid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    for tok in s.scalars(select(AccessToken).where(AccessToken.user_id == uid)).all():
        s.delete(tok)
    audit(s, "user.signed_out", actor=u.username, detail=t.username)
    flash(request, "Signed out of the technician app everywhere.")
    return back(f"/technicians/{uid}")


@router.post("/technicians/{uid}/teams", dependencies=[Depends(check_csrf)])
def technician_add_team(uid: int, request: Request, team_id: int = Form(...), s: Session = Depends(get_db),
                        u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    team = s.get(Team, team_id) or _404()
    if team not in t.teams:
        t.teams.append(team)
        audit(s, "team.member_added", actor=u.username, detail={"team": team.name, "user": t.username})
    return back(f"/technicians/{uid}")


@router.post("/technicians/{uid}/delete", dependencies=[Depends(check_csrf)])
def technician_delete(uid: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                      u: User = Depends(admin_user)):
    t = s.get(User, uid) or _404()
    if t.id == u.id:
        flash(request, "You can't delete yourself.", "err")
        return back(f"/technicians/{uid}")
    before = access_map(s)
    s.delete(t)
    n = rotate_lost_access(s, crypto, before, f"technician {t.username} deleted", u.username)
    audit(s, "user.deleted", actor=u.username, detail=t.username)
    flash(request, f"{t.label} deleted. Passwords are being changed on {n} device(s).", "warn")
    return back("/technicians")


# --------------------------------------------------------------------------- teams

@router.get("/teams", response_class=HTMLResponse)
def teams(request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    return render(request, "teams.html", teams=_all_teams(s))


@router.post("/teams", dependencies=[Depends(check_csrf)])
def team_create(request: Request, name: str = Form(...), note: str = Form(""), s: Session = Depends(get_db),
                u: User = Depends(admin_user)):
    name = name.strip()
    if not name or s.scalar(select(Team).where(Team.name == name)):
        flash(request, "A team needs a unique name.", "err")
        return back("/teams")
    t = Team(name=name, note=note.strip())
    s.add(t)
    s.flush()
    audit(s, "team.created", actor=u.username, detail=name)
    return back(f"/teams/{t.id}")


@router.get("/teams/{tid}", response_class=HTMLResponse)
def team_detail(tid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    t = s.get(Team, tid) or _404()
    grants = s.scalars(select(Grant).where(Grant.team_id == tid)).all()
    return render(request, "team_detail.html", t=t, grants=grants, users=_all_users(s), clients=_all_clients(s),
                  all_devices=s.scalars(select(Device).order_by(Device.hostname)).all(), now=utcnow())


@router.post("/teams/{tid}/members", dependencies=[Depends(check_csrf)])
def team_members(tid: int, request: Request, user_id: int = Form(...), action: str = Form("add"),
                 s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto), u: User = Depends(admin_user)):
    t = s.get(Team, tid) or _404()
    member = s.get(User, user_id) or _404()
    before = access_map(s)
    if action == "remove" and member in t.members:
        t.members.remove(member)
        rotate_lost_access(s, crypto, before, f"{member.username} removed from team {t.name}", u.username)
        audit(s, "team.member_removed", actor=u.username, detail={"team": t.name, "user": member.username})
    elif action == "add" and member not in t.members:
        t.members.append(member)
        audit(s, "team.member_added", actor=u.username, detail={"team": t.name, "user": member.username})
    return back(request.headers.get("referer") or f"/teams/{tid}")


@router.post("/teams/{tid}/delete", dependencies=[Depends(check_csrf)])
def team_delete(tid: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                u: User = Depends(admin_user)):
    t = s.get(Team, tid) or _404()
    before = access_map(s)
    s.delete(t)
    rotate_lost_access(s, crypto, before, f"team {t.name} deleted", u.username)
    audit(s, "team.deleted", actor=u.username, detail=t.name)
    flash(request, f"Team {t.name} deleted.", "warn")
    return back("/teams")


# --------------------------------------------------------------------------- grants

@router.get("/access", response_class=HTMLResponse)
def access(request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    grants = s.scalars(select(Grant).order_by(Grant.id.desc())).all()
    return render(request, "access.html", grants=grants, users=_all_users(s), teams=_all_teams(s),
                  clients=_all_clients(s), all_devices=s.scalars(select(Device).order_by(Device.hostname)).all(),
                  now=utcnow())


@router.post("/access", dependencies=[Depends(check_csrf)])
def grant_create(request: Request, subject: str = Form(...), target: str = Form(...), expiry: str = Form("never"),
                 expiry_custom: str = Form(""), note: str = Form(""), s: Session = Depends(get_db),
                 u: User = Depends(admin_user)):
    back_to = request.headers.get("referer") or "/access"
    try:
        skind, sid = subject.split(":", 1)
        tkind, tid = target.split(":", 1)
        g = Grant(note=note.strip()[:255], created_by=u.username, expires_at=_parse_expiry(expiry, expiry_custom))
        if skind == "user":
            g.user_id = int(sid)
        elif skind == "team":
            g.team_id = int(sid)
        else:
            raise ValueError
        if tkind == "client":
            g.client_id = int(tid)
        elif tkind == "device":
            g.device_id = int(tid)
        else:
            raise ValueError
    except ValueError:
        flash(request, "Pick who gets access and what they can reach.", "err")
        return back(back_to)
    existing = s.scalar(select(Grant).where(Grant.user_id == g.user_id, Grant.team_id == g.team_id,
                                            Grant.client_id == g.client_id, Grant.device_id == g.device_id))
    if existing:
        existing.expires_at, existing.note = g.expires_at, g.note or existing.note
        g = existing
    else:
        s.add(g)
    s.flush()
    audit(s, "grant.created", actor=u.username, detail={"grant": g.id, "user": g.user_id, "team": g.team_id,
                                                        "client": g.client_id, "device": g.device_id,
                                                        "expires": g.expires_at})
    flash(request, "Access granted. It appears in their technician app the next time the address book refreshes.")
    return back(back_to)


@router.post("/access/{gid}/delete", dependencies=[Depends(check_csrf)])
def grant_delete(gid: int, request: Request, s: Session = Depends(get_db), crypto: Crypto = Depends(get_crypto),
                 u: User = Depends(admin_user)):
    g = s.get(Grant, gid) or _404()
    before = access_map(s)
    audit(s, "grant.deleted", actor=u.username, detail={"grant": g.id, "user": g.user_id, "team": g.team_id,
                                                        "client": g.client_id, "device": g.device_id})
    s.delete(g)
    n = rotate_lost_access(s, crypto, before, "access removed", u.username)
    flash(request, "Access removed." + (f" Passwords are being changed on {n} device(s)." if n else ""))
    return back(request.headers.get("referer") or "/access")


# --------------------------------------------------------------------------- enrolment tokens

def install_command(token: str) -> str:
    return f'YLTS-Remote-Setup.exe /S /ENROL={token}'


@router.get("/enrolment", response_class=HTMLResponse)
def enrolment(request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    tokens = s.scalars(select(EnrolmentToken).order_by(EnrolmentToken.id.desc())).all()
    return render(request, "enrolment.html", tokens=tokens, clients=_all_clients(s), now=utcnow(),
                  new_token=request.session.pop("new_token", None), install_command=install_command)


@router.post("/enrolment", dependencies=[Depends(check_csrf)])
def enrolment_create(request: Request, client_id: str = Form(""), label: str = Form(""), uses: str = Form(""),
                     expiry: str = Form("30d"), expiry_custom: str = Form(""), s: Session = Depends(get_db),
                     u: User = Depends(admin_user)):
    token = "ylts_" + new_token(18)
    et = EnrolmentToken(token_hash=token_hash(token), token_hint=token[:9], label=label.strip()[:128],
                        client_id=int(client_id) if client_id.isdigit() else None,
                        uses_left=int(uses) if uses.strip().isdigit() else None,
                        expires_at=_parse_expiry(expiry, expiry_custom), created_by=u.username)
    s.add(et)
    s.flush()
    audit(s, "enrol_token.created", actor=u.username, detail={"id": et.id, "client": et.client_id, "label": et.label})
    request.session["new_token"] = {"token": token, "command": install_command(token)}
    return back(request.headers.get("referer") or "/enrolment")


@router.post("/enrolment/{eid}/revoke", dependencies=[Depends(check_csrf)])
def enrolment_revoke(eid: int, request: Request, s: Session = Depends(get_db), u: User = Depends(admin_user)):
    et = s.get(EnrolmentToken, eid) or _404()
    et.revoked = True
    audit(s, "enrol_token.revoked", actor=u.username, detail={"id": et.id})
    flash(request, "Enrolment token revoked. Already-enrolled PCs are not affected.")
    return back(request.headers.get("referer") or "/enrolment")


# --------------------------------------------------------------------------- audit & settings

@router.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, kind: str = "", device: str = "", actor: str = "", page: int = 1,
              s: Session = Depends(get_db), u: User = Depends(admin_user)):
    stmt = select(AuditEvent)
    if kind:
        stmt = stmt.where(AuditEvent.kind.startswith(kind))
    if device:
        stmt = stmt.where(AuditEvent.device_rustdesk_id == device)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    per = 100
    rows = s.scalars(stmt.order_by(AuditEvent.id.desc()).offset((max(page, 1) - 1) * per).limit(per + 1)).all()
    names = {d.rustdesk_id: d.label for d in s.scalars(select(Device)).all()}
    return render(request, "audit.html", events=rows[:per], more=len(rows) > per, page=page, kind=kind,
                  device=device, actor=actor, names=names, loads=_loads)


@router.get("/settings", response_class=HTMLResponse)
def server_settings(request: Request, u: User = Depends(admin_user)):
    return render(request, "settings.html", key=settings.rustdesk_key())


# --------------------------------------------------------------------------- helpers

def _404():
    raise HTTPException(404, "Not found")


def _all_users(s: Session, include_inactive: bool = False) -> list[User]:
    stmt = select(User).order_by(User.username)
    if not include_inactive:
        stmt = stmt.where(User.active.is_(True))
    return list(s.scalars(stmt).all())


def _all_teams(s: Session) -> list[Team]:
    return list(s.scalars(select(Team).order_by(Team.name)).all())


def _all_clients(s: Session) -> list[Client]:
    return list(s.scalars(select(Client).order_by(Client.name)).all())
