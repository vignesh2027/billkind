"""Test setup: a real uvicorn server on a free port, the local PayPal fake, a temporary database, AI off."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="billkind-test-")
_sock = socket.socket()
_sock.bind(("127.0.0.1", 0))
PORT = _sock.getsockname()[1]
_sock.close()
# Keep real credentials for the opt-in sandbox contract test before the app settings are pointed at the fake.
# (Parsed by hand: importing app.config here would freeze settings before the test overrides below.)
_envfile = Path(__file__).resolve().parent.parent / ".env"
_dot = dict(line.split("=", 1) for line in (_envfile.read_text().splitlines() if _envfile.exists() else [])
            if "=" in line and not line.strip().startswith("#"))
os.environ.setdefault("SANDBOX_CLIENT_ID", _dot.get("PAYPAL_CLIENT_ID", "").strip())
os.environ.setdefault("SANDBOX_CLIENT_SECRET", _dot.get("PAYPAL_CLIENT_SECRET", "").strip())
os.environ.update({
    "DB_PATH": str(Path(_tmp) / "test.db"), "UPLOAD_DIR": str(Path(_tmp) / "uploads"),
    "APP_BASE_URL": f"http://127.0.0.1:{PORT}", "PAYPAL_MODE": "fake", "AI_DISABLED": "1",
    "BACKGROUND_JOBS": "0", "FAKE_PAYOUT_DELAY": "0.2", "SESSION_SECRET": "test", "PAYPAL_WEBHOOK_ID": "",
    "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": "", "PAYPAL_BASE_URL": "",
    "DEMO_SELF_HEAL": "0",  # tests/test_demo_heal.py switches it on where it is under test
    "DEMO_ADMIN_LOGIN": "1",  # the hosted demo offers one-click Admin and Biller; tests/test_security.py turns it off
})

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from scripts import seed  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session", autouse=True)
def server():
    db.connect()
    seed.seed()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(BASE + "/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield BASE
    srv.should_exit = True


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Every test signs in from 127.0.0.1; the per-address limits are tested on their own."""
    from app.web import limits
    for w in limits.ALL:
        w.reset()
    yield


TEST_PASSWORD = "a-test-only-password"
_ONE_CLICK = {"sponsor@billkind.demo", "bigsponsor@billkind.demo", "poster@billkind.demo", "biller@billkind.demo",
              "admin@billkind.demo"}


def login(email: str) -> tuple[httpx.Client, str]:
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    if email in _ONE_CLICK:
        c.post("/login", data={"email": email, "demo": "1", "next": "/inbox"})
    else:  # other seeded accounts are not one-click: give them a private password for the test
        from app.web.auth import hash_password
        db.run("UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(TEST_PASSWORD), email))
        c.post("/login", data={"email": email, "password": TEST_PASSWORD, "next": "/inbox"})
    import re
    html = c.get("/inbox").text
    return c, re.search(r'<meta name="csrf" content="([0-9a-f]+)">', html).group(1)


def wait_for(fn, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()
