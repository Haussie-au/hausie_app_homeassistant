from __future__ import annotations

import json
import os
import requests
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .clients.ha_client import HAClient
from .flow_logger import get_logger


_BLOCK_START = "# hausie-support-keys-start"
_BLOCK_END = "# hausie-support-keys-end"


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def _parse_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    lines = [line.strip() for line in raw.replace("\r", "\n").split("\n")]
    keys = [line for line in lines if line and not line.startswith("#")]
    return keys


def _load_public_keys() -> list[str]:
    raw = os.getenv("HAUSIE_SUPPORT_PUBLIC_KEYS")
    if not raw:
        raw = _read_secret_file(os.getenv("HAUSIE_SUPPORT_PUBLIC_KEYS_FILE"))
    return _parse_keys(raw)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _remove_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_block = False
    for line in lines:
        if line.strip() == _BLOCK_START:
            in_block = True
            continue
        if line.strip() == _BLOCK_END:
            in_block = False
            continue
        if not in_block:
            out.append(line)
    return out


def _merge_keys(existing_text: str, keys: list[str], enable: bool) -> str:
    lines = existing_text.splitlines()
    cleaned = _remove_block(lines)
    if not enable:
        return "\n".join([line for line in cleaned if line.strip()]) + "\n"
    block = [_BLOCK_START] + keys + [_BLOCK_END]
    merged = cleaned + [""] + block if cleaned else block
    return "\n".join(merged) + "\n"


@dataclass
class SupportState:
    active: bool = False
    started_at: float | None = None
    timeout_s: int = 900
    applied_keys: list[str] | None = None

    def expired(self) -> bool:
        if not self.active or not self.started_at:
            return False
        return (time.time() - float(self.started_at)) >= self.timeout_s


class RemoteSupportManager:
    """Handle remote support toggle and authorized_keys updates."""

    def __init__(
        self,
        *,
        ha_client: HAClient,
        toggle_entity: str,
        auth_keys_path: str,
        public_keys: list[str],
        support_keys_url: str | None = None,
        device_token: str | None = None,
        timeout_s: int = 900,
        poll_s: int = 10,
        state_path: str = "/data/hausie_support_state.json",
        manage_ssh_addon: bool = True,
        ssh_addon_slug: str | None = None,
        on_state_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._log = get_logger("support")
        self._ha = ha_client
        self._toggle_entity = toggle_entity
        self._auth_keys_path = Path(auth_keys_path)
        self._public_keys = public_keys
        self._support_keys_url = (support_keys_url or "").strip()
        self._device_token = (device_token or "").strip()
        self._timeout_s = max(60, int(timeout_s or 900))
        self._poll_s = max(5, int(poll_s or 10))
        self._state_path = Path(state_path)
        self._manage_ssh = bool(manage_ssh_addon)
        self._ssh_slug = (ssh_addon_slug or "").strip() or None
        self._on_state_change = on_state_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_toggle: bool | None = None
        self._state = self._load_state()

    def _load_state(self) -> SupportState:
        if not self._state_path.exists():
            return SupportState(active=False, started_at=None, timeout_s=self._timeout_s)
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return SupportState(active=False, started_at=None, timeout_s=self._timeout_s)
        return SupportState(
            active=bool(data.get("support_active")),
            started_at=data.get("support_started_at"),
            timeout_s=int(data.get("support_timeout") or self._timeout_s),
            applied_keys=[
                str(key).strip()
                for key in data.get("support_keys", [])
                if str(key).strip()
            ],
        )

    def _save_state(self) -> None:
        payload = {
            "support_active": bool(self._state.active),
            "support_started_at": self._state.started_at,
            "support_timeout": int(self._state.timeout_s),
            "support_keys": list(self._state.applied_keys or []),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _notify_state_change(self) -> None:
        if not self._on_state_change:
            return
        try:
            self._on_state_change(bool(self._state.active))
        except Exception as exc:
            self._log.warn(f"Remote support state notification failed: {exc}")

    def _read_toggle_state(self) -> bool:
        states = self._ha.get_states()
        for item in states:
            if isinstance(item, dict) and item.get("entity_id") == self._toggle_entity:
                return str(item.get("state") or "").lower() == "on"
        return False

    def _set_toggle(self, enabled: bool) -> None:
        try:
            self._ha.call_service(
                "input_boolean",
                "turn_on" if enabled else "turn_off",
                {"entity_id": self._toggle_entity},
            )
        except Exception as exc:
            self._log.warn(f"Failed to set toggle {self._toggle_entity}: {exc}")

    def _apply_keys(self, enabled: bool) -> None:
        public_keys = self._resolve_public_keys() if enabled else self._public_keys
        if enabled and not public_keys:
            self._log.warn("No support public keys configured.")
            raise RuntimeError("No support public keys configured.")
        _ensure_parent(self._auth_keys_path)
        existing = ""
        if self._auth_keys_path.exists():
            existing = self._auth_keys_path.read_text(encoding="utf-8")
        merged = _merge_keys(existing, public_keys, enabled)
        self._auth_keys_path.write_text(merged, encoding="utf-8")

        if enabled:
            self._state.applied_keys = list(public_keys)
            self._sync_ssh_addon_keys(public_keys, enabled=True)
        else:
            keys_to_remove = list(self._state.applied_keys or [])
            self._sync_ssh_addon_keys(keys_to_remove, enabled=False)
            self._state.applied_keys = []

    def _supervisor_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = os.getenv("SUPERVISOR_TOKEN", "").strip()
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not available.")
        url = f"http://supervisor{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = requests.request(method, url, headers=headers, json=payload, timeout=15)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Supervisor API failed {resp.status_code}: {resp.text}")
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _get_ssh_addon_options(self) -> dict[str, Any]:
        if not self._ssh_slug:
            raise RuntimeError("SSH add-on slug not configured.")
        data = self._supervisor_request("GET", f"/addons/{self._ssh_slug}/info")
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        options = body.get("options") if isinstance(body, dict) else None
        return dict(options) if isinstance(options, dict) else {}

    def _set_ssh_addon_options(self, options: dict[str, Any]) -> None:
        if not self._ssh_slug:
            raise RuntimeError("SSH add-on slug not configured.")
        self._supervisor_request(
            "POST",
            f"/addons/{self._ssh_slug}/options",
            {"options": options},
        )

    @staticmethod
    def _merge_option_keys(existing: list[Any], keys: list[str], enabled: bool) -> list[str]:
        key_set = {str(key).strip() for key in keys if str(key).strip()}
        current = [str(key).strip() for key in existing if str(key).strip()]
        if not enabled:
            return [key for key in current if key not in key_set]
        merged = list(current)
        for key in keys:
            cleaned = str(key).strip()
            if cleaned and cleaned not in merged:
                merged.append(cleaned)
        return merged

    def _sync_ssh_addon_keys(self, keys: list[str], *, enabled: bool) -> None:
        if not self._manage_ssh or not self._ssh_slug:
            return
        cleaned = [str(key).strip() for key in keys if str(key).strip()]
        if not cleaned and enabled:
            return
        try:
            options = self._get_ssh_addon_options()
            if isinstance(options.get("ssh"), dict):
                ssh_options = dict(options["ssh"])
                ssh_options["authorized_keys"] = self._merge_option_keys(
                    ssh_options.get("authorized_keys") or [],
                    cleaned,
                    enabled,
                )
                options["ssh"] = ssh_options
            else:
                options["authorized_keys"] = self._merge_option_keys(
                    options.get("authorized_keys") or [],
                    cleaned,
                    enabled,
                )
            self._set_ssh_addon_options(options)
            action = "added to" if enabled else "removed from"
            self._log.ok(f"Support keys {action} SSH add-on options.")
        except Exception as exc:
            self._log.warn(f"Failed to sync SSH add-on authorized_keys: {exc}")

    def _resolve_public_keys(self) -> list[str]:
        if self._support_keys_url and self._device_token:
            try:
                resp = requests.get(
                    self._support_keys_url,
                    headers={"Authorization": f"Bearer {self._device_token}"},
                    timeout=10,
                )
                if resp.status_code // 100 != 2:
                    raise RuntimeError(f"{resp.status_code}: {resp.text}")
                data = resp.json()
                keys = data.get("keys") if isinstance(data, dict) else None
                parsed = [str(key).strip() for key in keys or [] if str(key).strip()]
                if parsed:
                    self._public_keys = parsed
                    return parsed
            except Exception as exc:
                self._log.warn(f"Failed to fetch support public keys: {exc}")
                return []
        return self._public_keys

    def _set_ssh_addon(self, enabled: bool) -> None:
        if not self._manage_ssh:
            return
        if not self._ssh_slug:
            self._log.warn("SSH add-on slug not configured.")
            return
        service = "addon_start" if enabled else "addon_stop"
        try:
            self._ha.call_service("hassio", service, {"addon": self._ssh_slug})
        except Exception as exc:
            self._log.warn(f"Failed to toggle SSH add-on ({self._ssh_slug}): {exc}")

    def _enable(self) -> None:
        if self._state.active:
            return
        self._log.start("Enabling remote support.")
        try:
            self._apply_keys(True)
        except Exception as exc:
            self._log.warn(f"Remote support not enabled: {exc}")
            self._set_toggle(False)
            self._notify_state_change()
            return
        self._set_ssh_addon(True)
        self._state.active = True
        self._state.started_at = time.time()
        self._state.timeout_s = self._timeout_s
        self._save_state()
        self._notify_state_change()
        self._log.ok("Remote support enabled.")

    def _disable(self) -> None:
        if not self._state.active:
            return
        self._log.start("Disabling remote support.")
        self._apply_keys(False)
        self._set_ssh_addon(False)
        self._state.active = False
        self._state.started_at = None
        self._save_state()
        self._notify_state_change()
        self._log.ok("Remote support disabled.")

    def _tick(self) -> None:
        try:
            enabled = self._read_toggle_state()
        except Exception as exc:
            self._log.warn(f"Failed to read toggle: {exc}")
            return

        if self._last_toggle is None:
            self._last_toggle = enabled

        if enabled and not self._state.active:
            self._enable()
        elif not enabled and self._state.active:
            self._disable()

        if self._state.active and self._state.expired():
            self._log.warn("Remote support timed out; disabling.")
            self._disable()
            self._set_toggle(False)
        elif self._state.active:
            self._notify_state_change()

        self._last_toggle = enabled

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._state.active and self._state.expired():
            self._disable()
            self._set_toggle(False)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        self._log.start("Remote support manager started.")
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self._poll_s)
