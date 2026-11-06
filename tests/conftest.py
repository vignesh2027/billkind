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
os.environ.update({
    "DB_PATH": str(Path(_tmp) / "test.db"), "UPLOAD_DIR": str(Path(_tmp) / "uploads"),
    "APP_BASE_URL": f"http://127.0.0.1:{PORT}", "PAYPAL_MODE": "fake", "AI_DISABLED": "1",
    "BACKGROUND_JOBS": "0", "FAKE_PAYOUT_DELAY": "0.2", "SESSION_SECRET": "test", "PAYPAL_WEBHOOK_ID": "",
    "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": "", "PAYPAL_BASE_URL": "",
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


def login(email: str) -> tuple[httpx.Client, str]:
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    c.post("/login", data={"email": email, "demo": "1", "next": "/inbox"})
    import re
    html = c.get("/inbox").text
    return c, re.search(r'<meta name="csrf" content="([0-9a-f]+)">', html).group(1)


def wait_for(fn, timeout: float = 6.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()
