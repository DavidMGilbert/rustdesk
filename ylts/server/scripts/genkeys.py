#!/usr/bin/env python3
"""Print fresh secrets for server/.env (run once, keep them safe)."""
import base64
import os
import secrets

print(f"PORTAL_SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"PORTAL_ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}")
print(f"BOOTSTRAP_ADMIN_PASSWORD={secrets.token_urlsafe(18)}")
