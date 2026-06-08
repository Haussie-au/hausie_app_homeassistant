from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    raw = os.getenv("HAUSIE_ADDON_DATA_DIR", "").strip()
    return Path(raw) if raw else Path("/data")


def resolve_license_state_path() -> Path:
    raw = os.getenv("HAUSIE_LICENSE_STATE_PATH", "").strip()
    return Path(raw) if raw else (_data_dir() / "hausie_license_state.json")


def resolve_helpers_snapshot_path() -> Path:
    raw = os.getenv("HAUSIE_HELPERS_SNAPSHOT_PATH", "").strip()
    return Path(raw) if raw else (_data_dir() / "hausie_helpers_snapshot.json")


def resolve_free_plan_cache_path(kind: str) -> Path:
    safe_kind = "create" if str(kind or "").strip().lower() not in {"base", "create"} else str(kind).strip().lower()
    return _data_dir() / f"hausie_free_plan_{safe_kind}.json"


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_license_state(path: Path | None = None) -> dict[str, Any]:
    data = _load_json(path or resolve_license_state_path(), default={})
    return data if isinstance(data, dict) else {}


def save_license_state(data: dict[str, Any], path: Path | None = None) -> None:
    _save_json(path or resolve_license_state_path(), data if isinstance(data, dict) else {})


def load_helpers_snapshot(path: Path | None = None) -> dict[str, Any]:
    data = _load_json(path or resolve_helpers_snapshot_path(), default={})
    return data if isinstance(data, dict) else {}


def save_helpers_snapshot(data: dict[str, Any], path: Path | None = None) -> None:
    _save_json(path or resolve_helpers_snapshot_path(), data if isinstance(data, dict) else {})


def load_free_plan_cache(kind: str) -> dict[str, Any]:
    data = _load_json(resolve_free_plan_cache_path(kind), default={})
    return data if isinstance(data, dict) else {}


def save_free_plan_cache(kind: str, data: dict[str, Any]) -> None:
    _save_json(resolve_free_plan_cache_path(kind), data if isinstance(data, dict) else {})
