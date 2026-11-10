"""Локальный кэш экспортированных ИР.

Платформа отдает ресурс только целиком или поддеревом, без offset/limit.
Кэш сохраняет один полный экспорт на диск, чтобы последующие чтения, поиск,
подсчет и постраничный обход не скачивали ресурс заново.
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .catalog import find_entry
from .client import IacpaasClient
from .config import PROJECT_ROOT
from .models import ErrorResult

CACHE_TTL = 300
CACHE_DIR = PROJECT_ROOT / "data" / "graph-cache"


class GraphCache:
    def __init__(self, directory: Path = CACHE_DIR, ttl: int = CACHE_TTL):
        self.directory = directory
        self.ttl = ttl
        self._locks: dict[str, asyncio.Lock] = {}

    def _file(self, path: str) -> Path:
        key = hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{key}.json"

    def _lock(self, path: str) -> asyncio.Lock:
        return self._locks.setdefault(path.casefold(), asyncio.Lock())

    def _read(self, path: str) -> dict[str, Any] | None:
        target = self._file(path)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("document"), dict):
            return None
        if payload.get("path") != path:
            return None
        if time.time() - float(payload.get("saved_at", 0)) > self.ttl:
            return None
        return payload

    def _write(self, path: str, document: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._file(path)
        temporary = target.with_suffix(".tmp")
        payload = {
            "path": path,
            "saved_at": time.time(),
            "document": document,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)

    def invalidate(self, path: str) -> None:
        try:
            self._file(path).unlink()
        except FileNotFoundError:
            pass
        self._locks.pop(path.casefold(), None)

    async def load(
        self,
        client: IacpaasClient,
        path: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any] | ErrorResult:
        try:
            if find_entry(path) is None:
                return ErrorResult(
                    error=f"Ресурс '{path}' отсутствует в catalog.txt. Сначала вызовите list_inforesources.",
                    kind="not_found",
                )
        except Exception as exc:  # noqa: BLE001 - ошибка файла возвращается клиенту
            return ErrorResult(error=f"catalog.txt: {exc}", kind="platform")

        if not refresh:
            cached = self._read(path)
            if cached is not None:
                return cached["document"]

        async with self._lock(path):
            if not refresh:
                cached = self._read(path)
                if cached is not None:
                    return cached["document"]
            document = await client.export(path)
            if isinstance(document, ErrorResult):
                return document
            if not isinstance(document, dict):
                return ErrorResult(error="Ответ IACPaaS не является объектом", kind="platform")
            try:
                self._write(path, document)
            except OSError:
                pass
            return document


cache = GraphCache()
