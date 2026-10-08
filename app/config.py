"""Runtime settings. Secrets come only from the environment, never from files in the repo."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    clinic: dict
    knowledge_dir: Path
    data_dir: Path
    store_backend: str = "local"  # "local" (JSON file) or "google" (Calendar + Sheets)
    anthropic_model: str = "claude-opus-5-5"
    llm_enabled: bool = False
    pii_pepper: str = "dev-only-pepper-change-me"
    whatsapp_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_verify_token: str | None = None
    internal_api_token: str | None = None
    n8n_handoff_webhook_url: str | None = None
    use_mcp: bool = True
    google: dict = field(default_factory=dict)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader; real environment variables always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    clinic =json.loads((ROOT / "config" / "clinic.json").read_text(encoding="utf-8"))
    data_dir = Path(_env("DATA_DIR", str(ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        clinic=clinic,
        knowledge_dir=ROOT / "knowledge",
        data_dir=data_dir,
        store_backend=_env("STORE_BACKEND", "local"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        llm_enabled=bool(_env("ANTHROPIC_API_KEY")) and _env("LLM_ENABLED", "true") == "true",
        pii_pepper=_env("PII_PEPPER", "dev-only-pepper-change-me"),
        whatsapp_token=_env("WHATSAPP_TOKEN"),
        whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_app_secret=_env("WHATSAPP_APP_SECRET"),
        whatsapp_verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
        internal_api_token=_env("INTERNAL_API_TOKEN"),
        n8n_handoff_webhook_url=_env("N8N_HANDOFF_WEBHOOK_URL"),
        use_mcp=_env("USE_MCP", "true") == "true",
        google={
            "service_account_file": _env("GOOGLE_SERVICE_ACCOUNT_FILE"),
            "calendar_id": _env("GOOGLE_CALENDAR_ID"),
            "sheet_id": _env("GOOGLE_SHEET_ID"),
        },
    )
