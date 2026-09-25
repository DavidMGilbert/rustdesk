"""Runtime settings for the YLTS Portal, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # Public HTTPS base URL of the portal, e.g. https://remote.ylts.com.au
    public_url: str = field(default_factory=lambda: _env("PORTAL_PUBLIC_URL", "http://localhost:8000"))
    # Host name clients use for the RustDesk ID/relay server (usually the same host)
    rustdesk_host: str = field(default_factory=lambda: _env("RUSTDESK_HOST", "localhost"))
    # Where hbbs writes id_ed25519.pub (shared docker volume). Used to show the key in the UI.
    rustdesk_key_file: str = field(default_factory=lambda: _env("RUSTDESK_KEY_FILE", "/rustdesk-data/id_ed25519.pub"))
    data_dir: Path = field(default_factory=lambda: Path(_env("PORTAL_DATA_DIR", "./data")))
    # Signs admin UI session cookies. Must be long and random.
    secret_key: str = field(default_factory=lambda: _env("PORTAL_SECRET_KEY"))
    # Fernet key that encrypts device passwords and TOTP secrets at rest.
    encryption_key: str = field(default_factory=lambda: _env("PORTAL_ENCRYPTION_KEY"))
    bootstrap_admin_user: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_USER", "admin"))
    bootstrap_admin_password: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_PASSWORD"))
    company_name: str = field(default_factory=lambda: _env("COMPANY_NAME", "Your Local Tech Solutions"))
    support_phone: str = field(default_factory=lambda: _env("SUPPORT_PHONE", ""))
    support_email: str = field(default_factory=lambda: _env("SUPPORT_EMAIL", ""))
    support_url: str = field(default_factory=lambda: _env("SUPPORT_URL", "https://www.ylts.com.au"))
    # RustDesk client access tokens (technician logins) expire after this many days.
    token_days: int = field(default_factory=lambda: int(_env("TOKEN_DAYS", "30")))
    # Agents re-apply the device password this often even without changes (hours).
    password_reassert_hours: int = field(default_factory=lambda: int(_env("PASSWORD_REASSERT_HOURS", "6")))
    # Secure cookies need HTTPS; disable only for local testing.
    secure_cookies: bool = field(default_factory=lambda: _env("SECURE_COOKIES", "1") == "1")

    @property
    def db_url(self) -> str:
        override = _env("PORTAL_DB_URL")
        if override:
            return override
        return f"sqlite:///{self.data_dir / 'portal.db'}"

    def rustdesk_key(self) -> str:
        try:
            return Path(self.rustdesk_key_file).read_text().strip()
        except OSError:
            return _env("RUSTDESK_KEY", "")

    def validate(self) -> None:
        problems = []
        if len(self.secret_key) < 32:
            problems.append("PORTAL_SECRET_KEY must be set (32+ random characters)")
        if not self.encryption_key:
            problems.append("PORTAL_ENCRYPTION_KEY must be set (generate with scripts/genkeys.py)")
        if problems:
            raise RuntimeError("; ".join(problems))


settings = Settings()
