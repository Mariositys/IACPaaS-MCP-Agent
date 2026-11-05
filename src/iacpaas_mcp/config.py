"""Настройки прототипа: базовый URL захардкожен, секреты из .env или окружения."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://iacpaas.dvo.ru"
TIMEOUT = 60
MAX_LIMIT = 500
MAX_DEPTH = 10
ALLOW_WRITE = os.getenv("IACPAAS_ALLOW_WRITE", "false").lower() == "true"
CATALOG_PATH = Path(".iacpaas/catalog.json")

API_KEY = os.getenv("IACPAAS_API_KEY")
USERNAME = os.getenv("IACPAAS_USERNAME")
PASSWORD = os.getenv("IACPAAS_PASSWORD")
