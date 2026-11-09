"""Локальный каталог доступных инфоресурсов: один путь на строку."""

from pathlib import Path

from .config import CATALOG_PATH
from .models import CatalogEntry, CatalogListing

CATALOG = Path(CATALOG_PATH)


def normalize_path(value: str) -> str:
    return value.strip().removesuffix("$;").strip("/")


def _title_from_path(path: str) -> str:
    return normalize_path(path).rsplit("/", 1)[-1]


def _load_paths() -> list[str]:
    file = CATALOG
    if not file.exists():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = normalize_path(line)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def load_entries() -> list[CatalogEntry]:
    return [CatalogEntry(title=_title_from_path(path), path=path) for path in _load_paths()]


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


def upsert_path(path: str) -> None:
    paths = _load_paths()
    target = normalize_path(path)
    if target and target not in paths:
        paths.append(target)
    _write_paths(paths)


def remove_entry(path: str) -> None:
    target = normalize_path(path)
    _write_paths([item for item in _load_paths() if item != target])


def _write_paths(paths: list[str]) -> None:
    file = CATALOG
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(paths)
    if payload:
        payload += "\n"
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(file)
