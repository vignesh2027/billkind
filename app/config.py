"""Settings, read once from the environment and an optional .env file."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SANDBOX_API = "https://api-m.sandbox.paypal.com"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    app_base_url: str = field(default_factory=lambda: _env("APP_BASE_URL", "http://localhost:8800").rstrip("/"))
    paypal_mode: str = field(default_factory=lambda: _env("PAYPAL_MODE", "fake"))
    paypal_client_id: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_ID"))
    paypal_client_secret: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_SECRET"))
    paypal_base_url_env: str = field(default_factory=lambda: _env("PAYPAL_BASE_URL"))
    paypal_sdk: str = field(default_factory=lambda: _env("PAYPAL_SDK", "apimatic"))  # apimatic or rest
    paypal_webhook_id: str = field(default_factory=lambda: _env("PAYPAL_WEBHOOK_ID"))
    ag_studio_license_key: str = field(default_factory=lambda: _env("AG_STUDIO_LICENSE_KEY"))
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    ai_text_model: str = field(default_factory=lambda: _env("AI_TEXT_MODEL", "openai/gpt-oss-120b"))
    ai_vision_model: str = field(default_factory=lambda: _env("AI_VISION_MODEL", "qwen/qwen3.8-27b"))
    ai_disabled: bool = field(default_factory=lambda: _env("AI_DISABLED", "0") == "1")
    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(ROOT / "data" / "billkind.db")))
    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", str(ROOT / "data" / "uploads")))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET") or secrets.token_hex(32))
    background_jobs: bool = field(default_factory=lambda: _env("BACKGROUND_JOBS", "1") == "1")
    fake_payout_delay: float = field(default_factory=lambda: float(_env("FAKE_PAYOUT_DELAY", "2.5")))

    @property
    def is_fake(self) -> bool:
        return self.paypal_mode != "sandbox"

    @property
    def paypal_base_url(self) -> str:
        if self.paypal_base_url_env:
            return self.paypal_base_url_env.rstrip("/")
        if self.is_fake:
            return self.app_base_url + "/fakepaypal"
        return SANDBOX_API

    @property
    def webhook_id(self) -> str:
        if self.paypal_webhook_id:
            return self.paypal_webhook_id
        return "WH-FAKE-BILLKIND" if self.is_fake else ""

    @property
    def ai_enabled(self) -> bool:
        return bool(self.groq_api_key) and not self.ai_disabled


settings = Settings()
