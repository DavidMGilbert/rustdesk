"""End-to-end tests: portal admin, agent enrolment/rotation, and the RustDesk client API."""
from __future__ import annotations

import datetime as dt
import re

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Device, Grant, utcnow
from app.main import create_app
from app.permissions import expire_grants
from app.rustdesk_api import throttle

ADMIN_PW = "correct horse battery staple"


@pytest.fixture()
def app(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.secret_key = "x" * 40
    cfg.encryption_key = Fernet.generate_key().decode()
    cfg.bootstrap_admin_user = "admin"
    cfg.bootstrap_admin_password = ADMIN_PW
    cfg.secure_cookies = False
    throttle.fails.clear()
    return create_app(cfg)


@pytest.fixture()
def web(app):
    with TestClient(app) as c:
        yield c


def csrf(client: TestClient, path: str) -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no csrf on {path}"
    return m.group(1)


def post(client: TestClient, path: str, data: dict, from_page: str | None = None):
    data = dict(data, csrf=csrf(client, from_page or path))
    return client.post(path, data=data, follow_redirects=False)


def admin_login(web: TestClient) -> None:
    r = post(web, "/login", {"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 303 and r.headers["location"] == "/"


def db(app):
    return app.state.db.Session()


def setup_world(web, app):
    """Admin creates a client, a technician and an enrolment token; a PC enrols."""
    admin_login(web)
    r = post(web, "/clients", {"name": "Smith Accounting", "note": ""}, "/clients")
    client_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = post(web, "/technicians", {"username": "jo", "display_name": "Jo Tech", "email": "",
                                   "password": "technician-pass-1"}, "/technicians")
    tech_id = int(r.headers["location"].rsplit("/", 1)[1])
    post(web, "/enrolment", {"client_id": str(client_id), "label": "office", "uses": "5", "expiry": "7d"},
         "/enrolment")
    html = web.get("/enrolment").text
    token = re.search(r'id="tok">([^<]+)<', html).group(1)
    r = web.post("/agent/enrol", json={"enrol_token": token, "rustdesk_id": "123456789",
                                       "hostname": "RECEPTION-PC", "os": "Windows 11", "agent_version": "1.0.0"})
    assert r.status_code == 200, r.text
    enrol = r.json()
    dev_tok = enrol["device_token"]
    pw1 = enrol["set_password"]["password"]
    r = web.post("/agent/ack", json={"version": enrol["set_password"]["version"], "ok": True},
                 headers={"Authorization": f"Bearer {dev_tok}"})
    assert r.status_code == 200
    return client_id, tech_id, dev_tok, pw1


def tech_login(web, username="jo", password="technician-pass-1"):
    r = web.post("/api/login", json={"username": username, "password": password, "id": "987654321",
                                     "uuid": "abc", "type": "account", "deviceInfo": {"os": "windows", "name": "TECH-LAPTOP"}})
    return r.json()


def shared_books(web, tok):
    h = {"Authorization": f"Bearer {tok}"}
    profiles = web.post(f"/api/ab/shared/profiles?current=1&pageSize=100", headers=h).json()
    out = {}
    for p in profiles["data"]:
        peers = web.post(f"/api/ab/peers?current=1&pageSize=100&ab={p['guid']}", headers=h).json()
        out[p["name"]] = peers["data"]
    return out


def test_full_flow(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)

    # Technician signs into the RustDesk app: no access yet
    res = tech_login(web)
    assert res["type"] == "access_token" and res["user"]["name"] == "jo"
    tok = res["access_token"]
    assert shared_books(web, tok) == {}
    h = {"Authorization": f"Bearer {tok}"}
    assert web.post("/api/currentUser", headers=h).json()["display_name"] == "Jo Tech"
    assert web.post("/api/ab/personal", headers=h).json()["guid"].startswith("personal-")

    # Admin grants the technician the whole client
    post(web, "/access", {"subject": f"user:{tech_id}", "target": f"client:{client_id}", "expiry": "never"}, "/access")
    books = shared_books(web, tok)
    assert list(books) == ["Smith Accounting"]
    peer = books["Smith Accounting"][0]
    assert peer["id"] == "123456789" and peer["password"] == pw1 and peer["hostname"] == "RECEPTION-PC"

    # The shared book is read-only for technicians
    r = web.post(f"/api/ab/peer/add/client-{client_id}", json={"id": "1"}, headers=h)
    assert "error" in r.json()
    # ...but their personal book is editable
    guid = web.post("/api/ab/personal", headers=h).json()["guid"]
    assert web.post(f"/api/ab/peer/add/{guid}", json={"id": "555666777", "alias": "home"}, headers=h).text == ""
    assert web.post(f"/api/ab/peers?ab={guid}", headers=h).json()["data"][0]["alias"] == "home"

    # Agent check-in with nothing to do
    ah = {"Authorization": f"Bearer {dev_tok}"}
    with db(app) as s:
        d = s.query(Device).one()
        ver = d.password_version
    r = web.post("/agent/checkin", json={"rustdesk_id": "123456789", "applied_version": ver}, headers=ah).json()
    assert "set_password" not in r and r["config"]["client_name"] == "Smith Accounting"

    # Removing access queues a password change; old password stays until the PC confirms
    with db(app) as s:
        gid = s.query(Grant).one().id
    post(web, f"/access/{gid}/delete", {}, "/access")
    assert shared_books(web, tok) == {}
    r = web.post("/agent/checkin", json={"rustdesk_id": "123456789", "applied_version": ver}, headers=ah).json()
    new = r["set_password"]
    assert new["password"] != pw1 and new["version"] == ver + 1
    web.post("/agent/ack", json={"version": new["version"], "ok": True}, headers=ah)
    with db(app) as s:
        d = s.query(Device).one()
        assert d.password_version == ver + 1 and not d.pending_password_enc
        assert app.state.crypto.decrypt(d.password_enc) == new["password"]


def test_team_grant_and_membership_removal_rotates(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    r = post(web, "/teams", {"name": "Field", "note": ""}, "/teams")
    team_id = int(r.headers["location"].rsplit("/", 1)[1])
    post(web, f"/teams/{team_id}/members", {"user_id": str(tech_id), "action": "add"}, f"/teams/{team_id}")
    with db(app) as s:
        dev_id = s.query(Device).one().id
    post(web, "/access", {"subject": f"team:{team_id}", "target": f"device:{dev_id}", "expiry": "1d"}, "/access")
    tok = tech_login(web)["access_token"]
    assert "Smith Accounting" in shared_books(web, tok)
    post(web, f"/teams/{team_id}/members", {"user_id": str(tech_id), "action": "remove"}, f"/teams/{team_id}")
    assert shared_books(web, tok) == {}
    with db(app) as s:
        assert s.query(Device).one().pending_password_enc


def test_grant_expiry(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    post(web, "/access", {"subject": f"user:{tech_id}", "target": f"client:{client_id}", "expiry": "1h"}, "/access")
    with db(app) as s:
        g = s.query(Grant).one()
        g.expires_at = utcnow() - dt.timedelta(seconds=1)
        s.commit()
        assert expire_grants(s, app.state.crypto) == 1
        s.commit()
        assert s.query(Grant).count() == 0
        assert s.query(Device).one().pending_password_enc


def test_disabled_tech_loses_access_and_tokens(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    post(web, "/access", {"subject": f"user:{tech_id}", "target": f"client:{client_id}", "expiry": "never"}, "/access")
    tok = tech_login(web)["access_token"]
    post(web, f"/technicians/{tech_id}", {"display_name": "Jo", "email": ""}, f"/technicians/{tech_id}")  # active unticked
    r = web.post("/api/ab/shared/profiles", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401 and "error" in r.json()
    assert "Wrong username" in tech_login(web)["error"]
    with db(app) as s:
        assert s.query(Device).one().pending_password_enc


def test_totp_login_for_technician_app(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    secret = pyotp.random_base32()
    with db(app) as s:
        from app.db import User
        u = s.get(User, tech_id)
        u.totp_secret_enc = app.state.crypto.encrypt(secret)
        s.commit()
    step1 = tech_login(web)
    assert step1["type"] == "email_check" and step1["tfa_type"] == "tfa_check"
    bad = web.post("/api/login", json={"username": "jo", "secret": step1["secret"], "tfaCode": "000000",
                                      "type": "email_code"}).json()
    assert "error" in bad
    good = web.post("/api/login", json={"username": "jo", "secret": step1["secret"],
                                       "tfaCode": pyotp.TOTP(secret).now(), "type": "email_code"}).json()
    assert good["type"] == "access_token"


def test_login_throttle(web, app):
    setup_world(web, app)
    for _ in range(5):
        assert "Wrong" in tech_login(web, password="nope-nope-nope")["error"]
    assert "Too many" in tech_login(web)["error"]


def test_host_heartbeat_sysinfo_and_uuid_pinning(web, app):
    setup_world(web, app)
    r = web.post("/api/sysinfo", json={"id": "123456789", "uuid": "UUID-A", "hostname": "RECEPTION-PC",
                                       "username": "reception", "os": "Windows 11 Pro", "cpu": "i5", "version": "1.4.2"})
    assert r.text == "SYSINFO_UPDATED"
    # An impostor with a different UUID is ignored
    web.post("/api/sysinfo", json={"id": "123456789", "uuid": "UUID-EVIL", "hostname": "EVIL"})
    with db(app) as s:
        d = s.query(Device).one()
        assert d.rustdesk_uuid == "UUID-A" and d.hostname == "RECEPTION-PC" and d.os_username == "reception"
        dev_id = d.id
    hb = web.post("/api/heartbeat", json={"id": "123456789", "uuid": "UUID-A", "ver": 1, "conns": [7]}).json()
    assert "disconnect" not in hb
    post(web, f"/devices/{dev_id}/disconnect", {}, f"/devices/{dev_id}")
    hb = web.post("/api/heartbeat", json={"id": "123456789", "uuid": "UUID-A", "ver": 1, "conns": [7]}).json()
    assert hb["disconnect"] == [7]
    web.post("/api/audit/conn", json={"id": "123456789", "uuid": "UUID-A", "action": "new", "ip": "1.2.3.4", "conn_id": 7})
    assert "conn.new" in web.get("/audit").text


def test_enrolment_token_limits(web, app):
    admin_login(web)
    post(web, "/enrolment", {"client_id": "", "label": "one", "uses": "1", "expiry": "1d"}, "/enrolment")
    token = re.search(r'id="tok">([^<]+)<', web.get("/enrolment").text).group(1)
    ok = web.post("/agent/enrol", json={"enrol_token": token, "rustdesk_id": "111222333"})
    assert ok.status_code == 200
    again = web.post("/agent/enrol", json={"enrol_token": token, "rustdesk_id": "444555666"})
    assert again.status_code == 403
    assert web.post("/agent/enrol", json={"enrol_token": "junk", "rustdesk_id": "444555666"}).status_code == 403
    assert web.post("/agent/checkin", json={}, headers={"Authorization": "Bearer junk"}).status_code == 401


def test_help_request_and_approval_mode(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    ah = {"Authorization": f"Bearer {dev_tok}"}
    web.post("/agent/help", json={"message": "Printer broken", "contact": "0400 000 000"}, headers=ah)
    web.post("/agent/checkin", json={"approval_required": True, "applied_version": 1}, headers=ah)
    page = web.get("/").text
    assert "Printer broken" in page
    with db(app) as s:
        assert s.query(Device).one().paused_by_user


def test_all_admin_pages_render(web, app):
    client_id, tech_id, dev_tok, pw1 = setup_world(web, app)
    with db(app) as s:
        dev_id = s.query(Device).one().id
    r = post(web, "/teams", {"name": "Field", "note": ""}, "/teams")
    team_path = r.headers["location"]
    for path in ["/", "/clients", f"/clients/{client_id}", "/devices", f"/devices/{dev_id}", "/technicians",
                 f"/technicians/{tech_id}", "/teams", team_path, "/access", "/enrolment", "/audit", "/settings",
                 "/account", "/my-devices", "/devices?q=RECEP", "/audit?kind=grant"]:
        r = web.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    # 2FA setup page renders a QR code
    post(web, "/account/2fa/start", {}, "/account")
    assert "<svg" in web.get("/account").text
    # reveal password is audited
    post(web, f"/devices/{dev_id}/reveal", {}, f"/devices/{dev_id}")
    assert pw1 in web.get(f"/devices/{dev_id}").text
    assert "password.revealed" in web.get("/audit").text


def test_non_admin_cannot_use_admin_pages(web, app):
    setup_world(web, app)
    post(web, "/logout", {}, "/")
    r = post(web, "/login", {"username": "jo", "password": "technician-pass-1"})
    assert r.headers["location"] == "/my-devices"
    assert web.get("/technicians").status_code == 403
    assert web.get("/my-devices").status_code == 200


def test_csrf_required(web, app):
    admin_login(web)
    r = web.post("/clients", data={"name": "X"}, follow_redirects=False)
    assert r.status_code == 400
