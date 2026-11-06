"""Hosted demo on Modal: one always-on container, SQLite backed up to a Modal Volume.

Deploy: modal deploy modal_app.py
The URL is https://<workspace>--billkind.us-east.modal.run (set BILLKIND_URL if Modal reports another). Secrets come from the local .env at deploy time
(GROQ_API_KEY, SESSION_SECRET, and the PayPal sandbox values once they exist).
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
WORKSPACE = os.environ.get("MODAL_WORKSPACE", "applemacbook6sep2004")
BASE_URL = os.environ.get("BILLKIND_URL", f"https://{WORKSPACE}--billkind.us-east.modal.run")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("tesseract-ocr", "fonts-dejavu-core")
    .pip_install_from_requirements(str(HERE / "requirements.txt"))
    .run_commands("pip install --no-deps paypal-agent-toolkit==1.11.0")
    .env({"APP_BASE_URL": BASE_URL, "DB_PATH": "/root/live/billkind.db", "UPLOAD_DIR": "/data/uploads",
          "BACKGROUND_JOBS": "1"})
    .add_local_dir(str(HERE / "app"), "/root/billkind/app", ignore=["**/__pycache__"])
    .add_local_dir(str(HERE / "scripts"), "/root/billkind/scripts", ignore=["**/__pycache__"])
    .add_local_dir(str(HERE / "samples"), "/root/billkind/samples")
)

app = modal.App("billkind")
volume = modal.Volume.from_name("billkind-data", create_if_missing=True)


def _dotenv() -> dict:
    env = {}
    path = HERE / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.split(" #")[0].strip().strip('"').strip("'")
                if v and k.strip() != "APP_BASE_URL":
                    env[k.strip()] = v
    return env


secrets = [modal.Secret.from_dict(_dotenv())]


@app.function(image=image, volumes={"/data": volume}, secrets=secrets, min_containers=1, max_containers=1,
              cpu=1.0, memory=1024, timeout=24 * 3600)
@modal.concurrent(max_inputs=200)
@modal.asgi_app(label="billkind")
def web():
    import shutil
    import sqlite3
    import sys
    import threading
    import time

    sys.path.insert(0, "/root/billkind")
    os.chdir("/root/billkind")
    live = Path(os.environ["DB_PATH"])
    live.parent.mkdir(parents=True, exist_ok=True)
    saved = Path("/data/billkind.db")
    if saved.exists() and not live.exists():
        shutil.copy(saved, live)

    from app import db
    from scripts import seed

    db.connect()
    if not db.one("SELECT 1 FROM users LIMIT 1"):
        seed.seed()

    def backup_forever():
        while True:
            time.sleep(30)
            try:
                src = sqlite3.connect(live)
                dst = sqlite3.connect("/data/billkind.db.tmp")
                src.backup(dst)
                dst.close()
                src.close()
                os.replace("/data/billkind.db.tmp", saved)
                volume.commit()
            except Exception as exc:  # keep serving even if a backup fails
                print("backup failed:", exc)

    threading.Thread(target=backup_forever, daemon=True).start()

    from app.main import app as fastapi_app
    return fastapi_app
