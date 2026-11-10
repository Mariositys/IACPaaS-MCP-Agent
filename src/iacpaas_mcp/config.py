"""Настройки прототипа: базовый URL захардкожен, секреты из .env или окружения."""

import os
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
