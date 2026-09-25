# YLTS Remote Support: hand-off notes

Status as of 25 September 2026. This package is **partly finished**. The server side is complete and tested. The Windows agent compiles and runs its command-line status on this machine. The branded RustDesk apps are produced by `ci/ylts-windows.yml`, which has not been run yet: it needs a fork of RustDesk at tag 1.5.0, the two repository secrets, and a GitHub Windows runner.

## What this is

It's a self-hosted remote support system for Your Local Tech Solutions, built on open-source RustDesk:

| Piece | What it does | Runs on |
|---|---|---|
| **hbbs / hbbr** | RustDesk ID and relay servers (the stock `rustdesk/rustdesk-server` image) | VPS (Docker) |
| **YLTS Portal** | Web admin, plus the "API server" that RustDesk apps talk to: technician logins, per-client address books, access control, audit | VPS (Docker) |
| **YLTS Remote Host** | Branded RustDesk, incoming only, installed as a service on client PCs | Client PCs |
| **YLTS Agent** | Windows service plus tray icon: registers the PC, applies and rotates its access password, "ask me first" mode, help requests | Client PCs |
| **YLTS Technician** | Branded RustDesk, outgoing only, for staff | Staff PCs |
| **YLTS Quick Support** | Portable RustDesk for one-off jobs: the client reads out an ID and one-time code | Client, ad hoc |

### How access control works

The open-source RustDesk server doesn't enforce who can connect to what. Anyone with a device's ID and password can connect. This design enforces permissions through the password instead:

1. Every enrolled PC gets its own random 20-character password. The portal generates it and stores it encrypted, and the agent applies it to the host.
2. Technicians sign into the Technician app with their portal account (optional TOTP 2FA). The portal gives them one read-only address book per client they're allowed to reach. Each device entry carries its password, so they connect by double-clicking and never see it.
3. When anyone loses access to a device (grant removed or expired, removed from a team, account disabled or deleted, device moved to another client), the portal queues a new password. The agent applies it at its next check-in, about every 30 seconds, and confirms. After that, any copy of the old password stops working.
4. Admins can reach every device. Admins can also reveal a device's password in an emergency, and each reveal is written to the audit log.

**Known limitation:** a technician with legitimate access could dig the password out of their own app's memory or local files. Rotation limits this to the period while they still have access. Server-side enforcement would need RustDesk Server Pro.

## Folder map

```
server/
  docker-compose.yml   hbbs, hbbr, portal, Caddy (automatic HTTPS)
  Caddyfile
  .env.example         copy to .env and fill in
  scripts/genkeys.py   generates the portal secrets
  scripts/backup.sh    nightly backup of DB + RustDesk keys (cron)
  portal/              FastAPI app, SQLite, Jinja templates, tests
agent/                 Go source for ylts-agent.exe (service + tray)
branding/
  make_icons.py        generates the icon set (placeholder "Y" mark)
  apply_branding.py    patches a RustDesk checkout for YLTS
  variants/*.json      per-app settings (host / technician / quick support)
  generated/           icons produced by make_icons.py
installer/
  host.nsi             client PC installer (host + agent + enrolment)
  technician.nsi       staff installer
ci/
  ylts-windows.yml     GitHub Actions workflow (copy into a RustDesk 1.5.0 fork)
  install-into-fork.ps1  copies this package into that fork as ylts/
```

## Component status

| Component | Status | Verified how |
|---|---|---|
| Docker Compose stack | Done | `docker compose config` passes. **Not run**: no Docker engine was available |
| Portal: admin UI | Done | 12 automated tests pass (`cd server/portal && pytest`) |
| Portal: RustDesk API | Done | Tested against the request and response shapes read from the current RustDesk client source; **not yet tested with a real RustDesk app** |
| Portal: agent API | Done | Covered by the same tests |
| Agent (Go) | Code complete | Compiles for Windows x64. `ylts-agent version` and `status` run on Windows. No unit tests. `go.sum` is still produced by the workflow's `go mod tidy`. |
| Branding script | Done | Ran against RustDesk source (commit `483cc10`, 25 Sep 2026): patches apply, re-running is harmless, patched Rust parses. **No full build done.** |
| App variant configs | Done | Valid JSON; option names checked against RustDesk source |
| NSIS installers | Done | Both compile with `makensis` using placeholder payloads. **Never run on Windows.** |
| GitHub Actions build | Written, not run | `ci/ylts-windows.yml`. Branding anchors re-checked against tag 1.5.0. |
| Code signing | **Not started** | n/a |
| Real YLTS logo | **Not done**: placeholder mark | n/a |

## Remaining work (in order)

### 1. Stand up the server (about 1 hour)

1. Create a small Sydney VPS (1 vCPU / 1 GB is plenty). Point an A record at it, e.g. `remote.ylts.com.au`.
2. Open these ports: TCP 80, 443, 21115, 21116, 21117 and UDP 21116.
3. Copy `server/` to the VPS. Then `cp .env.example .env`, run `python3 scripts/genkeys.py` and paste the output into `.env`. Set `RUSTDESK_HOST` and the support contact details.
4. `docker compose up -d --build`
5. Sign into `https://remote.ylts.com.au` with the bootstrap admin. Change that password immediately and turn on 2FA (My account).
6. Open **Server** in the portal and copy the **Key**. The build needs it.
7. Add `scripts/backup.sh` to root's crontab. Keep `data/rustdesk/id_ed25519` safe: if it's lost, every deployed app has to be rebuilt.

### 2. Build the branded RustDesk apps

The workflow is `ci/ylts-windows.yml`. It follows the Windows x64 job from RustDesk 1.5.0, then brands the build, packs the three apps, builds the agent, and builds both installers. It does not sign anything and it does not build an MSI.

1. Fork `github.com/rustdesk/rustdesk` and check out tag `1.5.0` (not `master`). Run `git submodule update --init`.
2. From this package: `powershell -File ci\install-into-fork.ps1 -Fork <path-to-fork>`. That copies the package in as `ylts/` and installs the workflow at `.github/workflows/ylts-windows.yml`.
3. Commit those files and push the fork.
4. In the fork, add repository secrets `RUSTDESK_HOST` (the DNS name only, such as `remote.ylts.com.au`) and `RUSTDESK_KEY` (the hbbs public key from the portal Server page).
5. Run the **YLTS Windows** workflow. The artifact `ylts-windows-1.0.0` contains `YLTS-Remote-Host.exe`, `YLTS-Technician.exe`, `YLTS-Quick-Support-qs.exe`, `ylts-agent.exe`, `YLTS-Remote-Setup.exe` and `YLTS-Technician-Setup.exe`. The `-qs` suffix is what switches on RustDesk's quick-support mode.
6. `ylts/agent/go.sum` is already in this package from the first Windows build (`go mod tidy` on Go 1.24.7). The workflow runs `go mod tidy` again and uploads a fresh copy with the agent, in case the dependencies move.

Before each RustDesk upgrade: `apply_branding.py` checks that the source lines it edits still exist and stops with a clear error if they don't. That's the signal to update its anchors, and to update the toolchain pins at the top of `ci/ylts-windows.yml` if the upstream Windows job has changed. Anchors were re-checked against tag 1.5.0 on 25 September 2026.

### 3. Test on Windows before any client sees it

Use a clean Windows 10 and Windows 11 VM:

- [ ] Portal: create a client, a technician and an enrolment token.
- [ ] Run `YLTS-Remote-Setup.exe`, paste the token. Within about a minute the PC should show under the client in the portal with a confirmed password (v1).
- [ ] The tray icon appears and shows the Support ID. The "ask me first" tick box switches the host between password and click-to-accept within a few seconds.
- [ ] "Request help" shows up on the portal dashboard.
- [ ] Technician app: sign in, see the client's address book, double-click to connect without a password prompt. The client PC shows the session window, and a tray notification appears.
- [ ] Remove the technician's access. The device's password rotates, and the technician can no longer connect.
- [ ] Silent install: `YLTS-Remote-Setup.exe /S /ENROL=<token>`.
- [ ] Uninstall from Settings > Apps removes the host, agent, service and tray.
- [ ] Quick Support exe runs without installing and shows an ID and a one-time code.

Things most likely to need fixing on first run:

- whether `--get-id` and `--password` print to stdout when called from a service;
- the host's install path, which assumes `C:\Program Files\YLTS Remote Host\YLTS Remote Host.exe`;
- tray behaviour with a high-DPI display or multiple monitors.

### 4. Before rollout

- **Code signing:** without a certificate, SmartScreen and some antivirus products will warn about or block the host and agent. Budget for an OV or EV code-signing certificate, or Azure Trusted Signing, and sign every .exe, including the installers.
- **Real logo:** put a square PNG (512 px or larger) at `branding/logo-square.png` and re-run `make_icons.py`. The portal's `static/icon.svg` also needs replacing.
- **Privacy notice:** tell clients in writing what the software does: unattended access, session logging, and how to turn on approval or uninstall. The installer's welcome page has a short summary.
- Review the portal's audit log retention. It currently grows forever.

## Configuration reference (`server/.env`)

| Variable | Meaning |
|---|---|
| `RUSTDESK_HOST` | Public DNS name used for the portal, ID server and relay |
| `ACME_EMAIL` | Let's Encrypt contact |
| `PORTAL_SECRET_KEY` | Signs admin session cookies |
| `PORTAL_ENCRYPTION_KEY` | Fernet key that encrypts device passwords and 2FA secrets. **Losing it means every device has to be re-enrolled.** |
| `BOOTSTRAP_ADMIN_USER` / `_PASSWORD` | First admin, created only when the database is empty |
| `COMPANY_NAME`, `SUPPORT_PHONE`, `SUPPORT_EMAIL`, `SUPPORT_URL` | Shown in the client tray menu |
| `TOKEN_DAYS` | How long a Technician app sign-in lasts (default 30) |
| `PASSWORD_REASSERT_HOURS` | How often agents re-apply the current password even when nothing has changed (default 6) |

## Security notes for whoever takes this over

- Device passwords and TOTP secrets are encrypted at rest. Staff passwords use scrypt. Login is throttled to 5 failures per 15 minutes for each IP address and each username.
- Admin UI: CSRF tokens on every form, a strict CSP, secure cookies, 12-hour sessions.
- The agent's device token is stored in `C:\ProgramData\YLTS\agent.json`, readable only by SYSTEM and Administrators. Signed-in users can write to `C:\ProgramData\YLTS\control\`, which is how the tray talks to the service. The service reads only small regular files from there.
- The agent passes the new password to the host on the command line (`--password`). While that runs, any local administrator could see it in the process list. That's acceptable, because an admin already controls the PC, but note it.
- Host settings lock-down: permanent-password-only, the session window can't be hidden, LAN discovery and direct IP access are off, and security, network and server settings are hidden from the client. RustDesk's own tray icon is hidden so the YLTS icon is the only one.
- hbbs and hbbr run with `-k _`, so only apps built with your server's key can use them.

## Useful commands

```sh
# portal tests
cd server/portal && pip install -r requirements.txt pytest httpx && pytest -q

# run the portal locally without Docker
cd server/portal && PORTAL_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
  PORTAL_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())') \
  BOOTSTRAP_ADMIN_PASSWORD=change-me-now-please SECURE_COOKIES=0 \
  uvicorn app.main:app_factory --factory --reload

# on a client PC (admin prompt)
"C:\Program Files\YLTS Agent\ylts-agent.exe" status
"C:\Program Files\YLTS Agent\ylts-agent.exe" enrol ylts_newtoken
type C:\ProgramData\YLTS\agent.log
```
