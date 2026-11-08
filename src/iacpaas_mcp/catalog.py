"""Локальный каталог доступных инфоресурсов."""

import json
from pathlib import Path

from .config import CATALOG_PATH
from .models import CatalogEntry, CatalogListing

CATALOG = Path(CATALOG_PATH)


def normalize_path(value: str) -> str:
    return value.strip().removesuffix("$;").strip("/")


def load_entries() -> list[CatalogEntry]:
    file = CATALOG
    if not file.exists():
        return []
    raw = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("catalog.json должен содержать массив")
    return [CatalogEntry.model_validate(item) for item in raw]


def list_entries(query: str = "", limit: int = 20) -> CatalogListing:
    entries = load_entries()
    needle = query.strip().casefold()
    found = (
        [entry for entry in entries if needle in f"{entry.title} {entry.path}".casefold()]
        if needle
        else entries
    )
    return CatalogListing(
        resources=found[:limit],
        total_matches=len(found),
        truncated=len(found) > limit,
    )


def find_entry(path: str) -> CatalogEntry | None:
    target = normalize_path(path)
    for entry in load_entries():
        if normalize_path(entry.path) == target:
            return entry
    return None


def upsert_entry(entry: CatalogEntry) -> None:
    entries = load_entries()
    target = normalize_path(entry.path)
    for index, current in enumerate(entries):
        if normalize_path(current.path) == target:
            entries[index] = entry
            break
    else:
        entries.append(entry)
    _write(entries)


def remove_entry(path: str) -> None:
    target = normalize_path(path)
    entries = [entry for entry in load_entries() if normalize_path(entry.path) != target]
    _write(entries)


def _write(entries: list[CatalogEntry]) -> None:
    file = CATALOG
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = [entry.model_dump(mode="json") for entry in entries]
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(file)
