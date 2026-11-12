"""Password hashing and session helpers."""

from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import HTTPException, Request

from .. import db


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.one("SELECT * FROM users WHERE id = ?", (uid,))
    # Sessions are signed cookies with no server-side store. Signing out bumps the account's epoch, which retires
    # every cookie issued before it, including copies taken from another browser.
    if not user or int(request.session.get("ep", 0)) != int(user.get("session_epoch") or 0):
        request.session.clear()
        return None
    return user


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def require(request: Request, *roles: str) -> dict:
    user = current_user(request)
    if not user:
        raise LoginRequired(str(request.url.path))
    if roles and user["role"] not in roles and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="This page is for a different kind of account.")
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = os.urandom(16).hex()
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str | None) -> None:
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(status_code=400, detail="Your session expired. Please reload the page.")
