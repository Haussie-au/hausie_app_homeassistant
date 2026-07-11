from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Tuple, Optional

HAUSIE_SUPPORT_USERNAME = "hausie_support_user"


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


def resolve_ha_runtime_credentials() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    token = os.getenv("HA_TOKEN") or _read_secret_file(os.getenv("HA_TOKEN_FILE"))
    username = os.getenv("HA_UI_USERNAME") or None
    password = os.getenv("HA_UI_PASSWORD") or _read_secret_file(os.getenv("HA_UI_PASSWORD_FILE"))

    state = load_device_state()
    if not token:
        token = (state.get("ha_token") or "").strip() or None
    if not username:
        username = (
            state.get("ha_ui_username")
            or state.get("ha_support_username")
            or ""
        ).strip() or None
    if not password:
        password = (
            state.get("ha_ui_password")
            or state.get("ha_support_password")
            or ""
        ).strip() or None

    if password and not username:
        username = HAUSIE_SUPPORT_USERNAME
    return token, username, password


def persist_ha_runtime_credentials(
    *,
    ha_token: str | None = None,
    ha_ui_username: str | None = None,
    ha_ui_password: str | None = None,
    path: Path | None = None,
) -> None:
    if ha_token is None and ha_ui_username is None and ha_ui_password is None:
        return
    data = load_device_state(path)
    if ha_token is not None and str(ha_token).strip():
        data["ha_token"] = str(ha_token).strip()
    if ha_ui_username is not None and str(ha_ui_username).strip():
        data["ha_ui_username"] = str(ha_ui_username).strip()
    if ha_ui_password is not None and str(ha_ui_password).strip():
        data["ha_ui_password"] = str(ha_ui_password).strip()
    save_device_state(data, path)


def migrate_ha_runtime_credentials_from_env(path: Path | None = None) -> bool:
    env_token = os.getenv("HA_TOKEN") or _read_secret_file(os.getenv("HA_TOKEN_FILE"))
    env_username = os.getenv("HA_UI_USERNAME") or None
    env_password = os.getenv("HA_UI_PASSWORD") or _read_secret_file(os.getenv("HA_UI_PASSWORD_FILE"))
    if not env_token and not env_password:
        return False

    data = load_device_state(path)
    updated = False
    if env_token and str(data.get("ha_token") or "").strip() != env_token.strip():
        data["ha_token"] = env_token.strip()
        updated = True
    if env_password and str(data.get("ha_ui_password") or "").strip() != env_password.strip():
        data["ha_ui_password"] = env_password.strip()
        updated = True

    resolved_username = (env_username or HAUSIE_SUPPORT_USERNAME).strip()
    if env_password and str(data.get("ha_ui_username") or "").strip() != resolved_username:
        data["ha_ui_username"] = resolved_username
        updated = True

    if updated:
        save_device_state(data, path)
    return updated
