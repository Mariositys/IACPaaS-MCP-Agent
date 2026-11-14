"""Настройки прототипа: базовый URL захардкожен, секреты из .env или окружения."""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = "https://iacpaas.dvo.ru"
TIMEOUT = 60
MAX_LIMIT = 500
MAX_DEPTH = 10
ALLOW_WRITE = os.getenv("IACPAAS_ALLOW_WRITE", "false").lower() == "true"
_catalog = Path(os.getenv("IACPAAS_CATALOG", "catalog.txt")).expanduser()
CATALOG_PATH = str(_catalog if _catalog.is_absolute() else PROJECT_ROOT / _catalog)

API_KEY = os.getenv("IACPAAS_API_KEY")
USERNAME = os.getenv("IACPAAS_USERNAME")
PASSWORD = os.getenv("IACPAAS_PASSWORD")

APPLY_TOKEN_PATH = PROJECT_ROOT / "data" / "apply.token"
_APPLY_TOKEN = os.getenv("IACPAAS_CONFIRM_TOKEN", "").strip()


def get_apply_token() -> str:
    """Одноразовый локальный секрет для внутреннего apply-тулза."""
    global _APPLY_TOKEN
    if _APPLY_TOKEN:
        return _APPLY_TOKEN
    APPLY_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if APPLY_TOKEN_PATH.exists():
        _APPLY_TOKEN = APPLY_TOKEN_PATH.read_text(encoding="utf-8").strip()
    else:
        _APPLY_TOKEN = secrets.token_urlsafe(32)
        APPLY_TOKEN_PATH.write_text(_APPLY_TOKEN, encoding="utf-8")
    return _APPLY_TOKEN
