from __future__ import annotations

import json
import ipaddress
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .clients.ha_client import HAClient
from .flow_logger import get_logger
from .license_state import load_license_state


def _run_command(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_tailscale_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")


def _first_tailscale_ip(raw: str) -> str:
    for match in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw or ""):
        if _is_tailscale_ipv4(match):
            return match
    return ""


def _resolve_tailscale_ip() -> tuple[str, str]:
    configured = os.getenv("HAUSIE_TAILSCALE_IP", "").strip()
    if configured:
        return configured, "manual"

    try:
        output = _run_command(["tailscale", "ip", "-4"])
    except Exception:
        output = ""
    ip = _first_tailscale_ip(output)
    if ip:
        return ip, "tailscale-cli"

    for command in (
        ["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"],
        ["ip", "-4", "-o", "addr", "show"],
    ):
        try:
            output = _run_command(command)
        except Exception:
            output = ""
        ip = _first_tailscale_ip(output)
        if ip:
            return ip, "network-interface"

    return "", "missing"


class HeartbeatReporter:
    """Send periodic heartbeat payloads to Hausie Cloud."""

    def __init__(
        self,
        *,
        ha_client: HAClient,
        endpoint_url: str,
        device_id: str,
        token: str | None = None,
        interval_s: int = 180,
        support_interval_s: int | None = None,
        state_path: str = "/data/hausie_support_state.json",
        on_actions: Callable[[list[Any], dict[str, Any]], None] | None = None,
    ) -> None:
        self._log = get_logger("heartbeat")
        self._ha = ha_client
        self._endpoint = endpoint_url.rstrip("/")
        self._device_id = device_id
        self._token = token
        self._interval_s = max(30, int(interval_s or 180))
        configured_support_interval = support_interval_s
        if configured_support_interval is None:
            configured_support_interval = int(os.getenv("HAUSIE_SUPPORT_HEARTBEAT_INTERVAL", "15"))
        self._support_interval_s = max(5, int(configured_support_interval or 15))
        self._state_path = Path(state_path)
        self._on_actions = on_actions
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval_lock = threading.Lock()

    def _read_support_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _build_payload(self) -> dict[str, Any]:
        support = self._read_support_state()
        license_state = load_license_state()
        tailscale_ip, tailscale_ip_source = _resolve_tailscale_ip()
        try:
            config = self._ha.get_config()
        except Exception:
            config = {}
        return {
            "device_id": self._device_id,
            "timestamp": int(time.time()),
            "support_active": bool(support.get("support_active", False)),
            "support_started_at": support.get("support_started_at"),
            "support_timeout": support.get("support_timeout"),
            "ha_version": config.get("version") or config.get("version_core"),
            "addon_version": os.getenv("HAUSIE_ADDON_VERSION") or "",
            "tailscale_node_id": os.getenv("HAUSIE_TAILSCALE_NODE_ID") or "",
            "tailscale_ip": tailscale_ip,
            "tailscale_ip_source": tailscale_ip_source,
            "current_plan": str(license_state.get("plan") or "").strip(),
            "license_status": str(license_state.get("license_status") or "").strip(),
            "offline_valid_until": license_state.get("offline_valid_until"),
        }

    def _next_interval(self) -> int:
        support = self._read_support_state()
        with self._interval_lock:
            support_interval_s = self._support_interval_s
            interval_s = self._interval_s
        if bool(support.get("support_active", False)):
            return support_interval_s
        return interval_s

    def _send_once(self) -> None:
        payload = self._build_payload()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = requests.post(self._endpoint, headers=headers, json=payload, timeout=10)
            if resp.status_code // 100 != 2:
                self._log.warn(f"Heartbeat failed {resp.status_code}: {resp.text}")
                return
            data = None
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict) and self._on_actions:
                actions = data.get("actions")
                self._on_actions(actions if isinstance(actions, list) else [], data)
            self._log.ok("Heartbeat sent.")
        except Exception as exc:
            self._log.warn(f"Heartbeat error: {exc}")

    def send_now(self) -> None:
        self._send_once()

    def update_intervals(
        self,
        *,
        interval_s: int | None = None,
        support_interval_s: int | None = None,
    ) -> None:
        with self._interval_lock:
            if interval_s is not None:
                self._interval_s = max(30, int(interval_s))
            if support_interval_s is not None:
                self._support_interval_s = max(5, int(support_interval_s))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        self._log.start("Heartbeat reporter started.")
        while not self._stop.is_set():
            self._send_once()
            self._stop.wait(self._next_interval())
