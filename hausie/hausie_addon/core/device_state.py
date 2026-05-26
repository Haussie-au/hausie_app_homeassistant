from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Tuple, Optional


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def resolve_state_path() -> Path:
    raw = os.getenv("HAUSIE_DEVICE_STATE_PATH", "").strip()
    return Path(raw) if raw else Path("/data/hausie_device.json")


def load_device_state(path: Path | None = None) -> dict:
    state_path = path or resolve_state_path()
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_device_state(data: dict, path: Path | None = None) -> None:
    state_path = path or resolve_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def resolve_device_credentials() -> Tuple[Optional[str], Optional[str]]:
    device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or None
    token = os.getenv("HAUSIE_CLOUD_TOKEN") or _read_secret_file(os.getenv("HAUSIE_CLOUD_TOKEN_FILE"))
    if device_id and token:
        return device_id, token

    state = load_device_state()
    if not device_id:
        device_id = (state.get("hausie_device_id") or state.get("device_id") or "").strip() or None
    if not token:
        token = (state.get("device_token") or state.get("hausie_device_token") or "").strip() or None
    return device_id, token


def persist_device_credentials(
    device_id: str | None,
    token: str | None,
    *,
    path: Path | None = None,
) -> None:
    if not device_id and not token:
        return
    data = load_device_state(path)
    if device_id:
        data["hausie_device_id"] = device_id
    if token:
        data["device_token"] = token
    save_device_state(data, path)
