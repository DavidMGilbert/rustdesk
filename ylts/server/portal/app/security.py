"""Password hashing, tokens, encryption at rest and TOTP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string

import pyotp
from cryptography.fernet import Fernet, InvalidToken

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


# A pre-computed hash used to keep timing similar when a username does not exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_device_password(length: int = 20) -> str:
    """Random password for a host. Avoids characters that break command lines or are ambiguous."""
    alphabet = string.ascii_letters + string.digits
    alphabet = alphabet.translate({ord(c): None for c in "0OIl1"})
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.islower() for c in pw) and any(c.isupper() for c in pw):
            return pw


def password_policy_error(password: str) -> str:
    if len(password) < 12:
        return "Password must be at least 12 characters."
    return ""


class Crypto:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._f.decrypt(value.encode()).decode()
        except InvalidToken:
            return ""


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_uri(secret: str, username: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)
