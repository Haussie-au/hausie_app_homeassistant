from __future__ import annotations

import json
import os
import re
import hmac
import secrets
import shutil
import signal
import tempfile
import threading
import time
import hashlib
import html
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import requests
import yaml
import websocket

from .core.clients.ha_client import HAClient
from .core.cloud_client import CloudClient
from .core.flow_logger import get_logger
from .core.managers.notification_manager import NotificationManager
from .core.mqtt_listener import MQTTNotificationListener
from .core.heartbeat import HeartbeatReporter
from .core.remote_support import RemoteSupportManager, _load_public_keys
from .core.managers.config_manager import ConfigManager
from .core.managers.help_message_manager import HelpMessageManager
from .core.utils.naming import slugify
from .core.device_state import (
    HAUSIE_ADMIN_USERNAME,
    HAUSIE_SUPPORT_USERNAME,
    migrate_ha_runtime_credentials_from_env,
    persist_ha_runtime_credentials,
    resolve_device_credentials,
    resolve_ha_runtime_credentials,
    persist_device_credentials,
    resolve_state_path,
    load_device_state,
    save_device_state,
)
from .core.license_state import (
    load_free_plan_cache,
    load_helpers_snapshot,
    load_license_state,
    save_free_plan_cache,
    save_helpers_snapshot,
    save_license_state,
)
from .orchestration.device_label_updater import DeviceLabelUpdater
from .orchestration.dashboard_updater import DashboardUpdater
from .orchestration.new_device_dashboard import (
    resolve_config_dashboard_path,
    upsert_new_device_button,
)
from .constants import Labels
from .settings import DEFAULT_HAUSIE_CLOUD_URL, Settings

ROOT_DIR = Path(__file__).resolve().parents[1]
_ACTIVE_SERVER: HTTPServer | None = None
_SHUTDOWN_REQUESTED = False
_UI_CSRF_TOKEN = secrets.token_urlsafe(32)
_INGRESS_PROXY_IPS = frozenset({"172.30.32.2", "::ffff:172.30.32.2"})
_INGRESS_UI_PATHS = frozenset(
    {
        "",
        "/",
        "/setup",
        "/setup/status",
        "/pairing",
        "/pairing/status",
        "/credentials",
        "/credentials/status",
        "/logs",
    }
)
_INGRESS_MUTATION_PATHS = frozenset(
    {
        "/setup/initialize",
        "/credentials",
        "/pairing/start",
        "/pairing/stop",
        "/pairing/confirm",
    }
)


def _is_trusted_ingress_request(client_ip: str, headers: Any) -> bool:
    """Accept UI requests only from Home Assistant's authenticated Ingress proxy."""
    ingress_path = str(headers.get("X-Ingress-Path", "") or "").strip()
    remote_user_id = str(headers.get("X-Remote-User-Id", "") or "").strip()
    return (
        str(client_ip or "").strip() in _INGRESS_PROXY_IPS
        and ingress_path.startswith("/api/hassio_ingress/")
        and bool(remote_user_id)
    )


def _has_valid_ui_csrf_token(headers: Any) -> bool:
    supplied = str(headers.get("X-Hausie-CSRF-Token", "") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, _UI_CSRF_TOKEN)


def _ha_restart_exception_is_expected(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "service call failed 504",
            "connection refused",
            "remote end closed connection",
            "read timed out",
            "max retries exceeded",
        )
    )


def _start_background_workflow(action_name: str, runner) -> None:
    log = get_logger("addon")

    def _wrapped() -> None:
        try:
            runner()
        except Exception as exc:
            log.error(f"Background action failed: {action_name} ({exc})")
            if action_name == "initialize_hausie":
                _set_setup_progress("failed", str(exc))

    thread = threading.Thread(
        target=_wrapped,
        name=f"hausie-{action_name}",
        daemon=True,
    )
    thread.start()


def _migrate_legacy_addon_options() -> None:
    """Preserve HA credentials from pre-ingress add-on configuration once."""
    option_keys = {"ha_token", "ha_ui_username", "ha_ui_password"}
    candidate_paths: list[Path] = [Path("/data/options.json")]
    addon_configs = Path("/addon_configs")
    if addon_configs.exists():
        candidate_paths.extend(sorted(addon_configs.glob("*/options.json")))

    options_path: Path | None = None
    data: dict[str, Any] | None = None
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if option_keys.intersection(parsed.keys()):
            options_path = path
            data = parsed
            break
    if not data:
        return
    if options_path and not os.getenv("HAUSIE_DEVICE_STATE_PATH"):
        os.environ["HAUSIE_DEVICE_STATE_PATH"] = str(options_path.parent / "hausie_device.json")
    persist_ha_runtime_credentials(
        ha_token=str(data.get("ha_token") or "").strip() or None,
        ha_ui_username=str(data.get("ha_ui_username") or "").strip() or None,
        ha_ui_password=str(data.get("ha_ui_password") or "").strip() or None,
    )


_migrate_legacy_addon_options()


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return open(path, "r", encoding="utf-8").read().strip()
    except Exception:
        return None


def _resolve_notify_api_key() -> str | None:
    return (
        os.getenv("HAUSIE_NOTIFY_API_KEY")
        or _read_secret_file(os.getenv("HAUSIE_NOTIFY_API_KEY_FILE"))
    )


def _resolve_admin_notify_services() -> list[str]:
    raw = os.getenv("HAUSIE_ADMIN_NOTIFY_SERVICES", "").strip()
    if not raw:
        fallback = os.getenv("HA_DEFAULT_NOTIFY", "notify.notify").strip()
        return [fallback] if fallback else ["notify.notify"]
    services = [svc.strip() for svc in raw.split(",") if svc.strip()]
    return services or ["notify.notify"]


def _resolve_help_messages_path() -> Path | None:
    raw = os.getenv("HAUSIE_HELP_MESSAGES_PATH", "").strip()
    if not raw:
        return None
    return Path(raw)


def _resolve_addon_version() -> str:
    return (
        os.getenv("HAUSIE_ADDON_VERSION")
        or os.getenv("HASSIO_ADDON_VERSION")
        or os.getenv("ADDON_VERSION")
        or ""
    ).strip()


def _resolve_subscription_plan(settings: Settings) -> str | None:
    license_state = load_license_state()
    local_plan = _normalize_plan_id(license_state.get("plan"))
    if local_plan:
        return local_plan
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        return None
    try:
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
            create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
        )
        data = cloud.request_subscription_status()
    except Exception as exc:
        get_logger("addon").warn(f"Subscription status fallback failed: {exc}")
        return None
    return _normalize_plan_id(data.get("tier") or data.get("plan")) or None


def _normalize_plan_id(value: Any, default: str = "plan_1") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    match = re.search(r"plan[\s_]*(\d+)", raw)
    if match:
        return f"plan_{match.group(1)}"
    return raw or default


def _sync_license_state_from_cloud(
    settings: Settings,
    log,
    *,
    force: bool = False,
) -> dict[str, Any]:
    current = load_license_state()
    if current and not force:
        return current
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        return current
    try:
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
            create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
        )
        payload = cloud.request_subscription_status()
    except Exception as exc:
        if force:
            log.warn(f"Live license sync failed; using cached license state: {exc}")
        return current
    if isinstance(payload, dict):
        return _store_license_payload(payload, log)
    return current


def _update_rebuild_state(state: dict, *, plan: str | None, version: str | None) -> None:
    if plan:
        state["last_plan"] = plan
    if version:
        state["last_addon_version"] = version
    state["last_rebuild_at"] = int(time.time())
    save_device_state(state)


_REBUILD_ALLOWED_STEPS = {"create_base", "create_hausie", "sync_inventory"}


def _normalize_rebuild_steps(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    steps: list[str] = []
    for item in value:
        step = str(item or "").strip().lower()
        if step == "sync_inventory":
            step = "create_hausie"
        if step in _REBUILD_ALLOWED_STEPS and step not in steps:
            steps.append(step)
    return steps


def _resolve_local_rebuild_plan(
    *,
    current_plan: str | None,
    current_version: str | None,
    last_plan: str,
    last_version: str,
) -> dict[str, object]:
    plan_changed = False
    version_changed = False

    normalized_current_plan = _normalize_plan_id(current_plan, "") if current_plan else ""
    normalized_last_plan = _normalize_plan_id(last_plan, "") if last_plan else ""

    if normalized_current_plan:
        plan_changed = normalized_current_plan != normalized_last_plan if normalized_last_plan else True
    elif last_plan:
        plan_changed = True

    if current_version:
        version_changed = current_version != last_version if last_version else True
    elif last_version:
        version_changed = True

    steps = ["create_hausie"]
    reason = "no_change"
    if plan_changed or version_changed:
        steps = ["create_base", "create_hausie"]
        if plan_changed and version_changed:
            reason = "plan_and_addon_version_changed"
        elif plan_changed:
            reason = "plan_changed"
        else:
            reason = "addon_version_changed"

    return {
        "execution_plan": steps,
        "plan": normalized_current_plan or None,
        "reason": reason,
        "source": "local",
    }


def _resolve_remote_rebuild_plan(
    settings: Settings,
    *,
    state: dict,
    current_plan: str | None,
    current_version: str | None,
) -> dict[str, object] | None:
    if not settings.HAUSIE_CLOUD_URL:
        return None
    try:
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
            create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
        )
        stored_device_id = ""
        try:
            stored_device_id, _stored_token = resolve_device_credentials()
        except Exception:
            stored_device_id = ""
        device_id = (
            os.getenv("HAUSIE_DEVICE_ID")
            or os.getenv("HASSIO_ADDON_DEVICE_ID")
            or stored_device_id
            or state.get("device_id")
            or ""
        )
        response = cloud.request_rebuild_plan(
            {
                "trigger": "rebuild_hausie",
                "device_id": str(device_id or ""),
                "current_addon_version": current_version or "",
                "current_plan": current_plan or "",
                "last_plan": str(state.get("last_plan") or ""),
                "last_addon_version": str(state.get("last_addon_version") or ""),
            }
        )
    except Exception as exc:
        get_logger("addon").warn(f"Remote rebuild plan unavailable; using local fallback: {exc}")
        return None

    steps = _normalize_rebuild_steps(response.get("execution_plan"))
    if not steps:
        get_logger("addon").warn("Remote rebuild plan invalid; using local fallback.")
        return None
    return {
        "execution_plan": steps,
        "plan": response.get("plan") or current_plan,
        "reason": response.get("reason") or "remote",
        "source": "cloud",
    }


def _execute_rebuild_steps(steps: list[str], log) -> None:
    if "create_base" in steps:
        _run_create_base(manage_activity=False)
    if "create_hausie" in steps:
        _run_sync_inventory(manage_activity=False)


def _resolve_mqtt_enabled() -> bool:
    return os.getenv("HAUSIE_MQTT_ENABLE", "").strip().lower() in {"1", "true", "yes"}


def _resolve_mqtt_secret(name: str) -> str | None:
    return os.getenv(name) or _read_secret_file(os.getenv(f"{name}_FILE"))


_MQTT_LISTENER: MQTTNotificationListener | None = None
_SUPPORT_MANAGER: RemoteSupportManager | None = None
_HEARTBEAT: HeartbeatReporter | None = None
_HEARTBEAT_ACTION_LOCK = threading.Lock()
_HEARTBEAT_ACTION_RUNNING = False
_WORKFLOW_LOCK = threading.Lock()
_LICENSE_MONITOR_THREAD: threading.Thread | None = None
_LICENSE_MONITOR_STOP = threading.Event()
_INVENTORY_MONITOR_THREAD: threading.Thread | None = None
_INVENTORY_MONITOR_STOP = threading.Event()

_BASE_AUTOMATION_IDS = {
    "new_device_created",
    "new_device_saved",
    "ui_help_rotate_messages",
    "new_devices_scan_daily",
    "core_sync_inventory",
    "core_rebuild_hausie",
    "core_restart_hausie",
}

_KEEP_AUTOMATION_IDS = {
    *sorted(_BASE_AUTOMATION_IDS),
    "cleanup_base_assets",
    "cleanup_hausie_assets",
    "test_create_base",
    "test_create_hausie",
    "test_rebuild_all",
}

_BASE_HELPER_FILES = [
    ("input_button", "hausie_input_button_general.yaml"),
    ("input_boolean", "hausie_input_boolean_general.yaml"),
    ("input_number", "hausie_input_number_general.yaml"),
    ("input_select", "hausie_input_select_general.yaml"),
    ("input_text", "hausie_input_text_general.yaml"),
    ("input_datetime", "hausie_input_datetime_general.yaml"),
    ("input_boolean", "hausie_input_boolean.dashboards.yaml"),
    ("input_text", "hausie_input_text.dashboards.yaml"),
    ("input_button", "input_button_general.yaml"),
    ("input_boolean", "input_boolean_general.yaml"),
    ("input_number", "input_number_general.yaml"),
    ("input_select", "input_select_general.yaml"),
    ("input_text", "input_text_general.yaml"),
    ("input_datetime", "input_datetime_general.yaml"),
    ("input_boolean", "input_boolean.dashboards.yaml"),
    ("input_text", "input_text.dashboards.yaml"),
]

_BASE_HELPER_KEEP_FILES = {
    ("input_button", "hausie_input_button_general.yaml"),
    ("input_button", "input_button_general.yaml"),
    ("input_boolean", "hausie_input_boolean_general.yaml"),
    ("input_boolean", "hausie_input_boolean.dashboards.yaml"),
    ("input_boolean", "input_boolean.dashboards.yaml"),
    ("input_text", "hausie_input_text.dashboards.yaml"),
    ("input_text", "input_text.dashboards.yaml"),
}

_BASE_SCRIPT_KEEP_FILES = {
    "hausie_general_scripts.yaml",
    "general_scripts.yaml",
}

_CONFIG_DASHBOARD_FILENAME = "hausie_configuration_dashboard.yaml"
_TEST_DASHBOARD_FILENAME = "hausie_test_dashboard.yaml"
_MAIN_DASHBOARD_FILENAME = "hausie_dashboard.yaml"
_BOOTSTRAP_CONFIG_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "UI" / "config_bootstrap_dashboard.yaml"
_SELF_ADDON_SLUG: str | None = None

_PAIRING_LOCK = threading.Lock()
_PAIRING_THREAD: threading.Thread | None = None
_PAIRING_LABEL_OPTIONS = [
    Labels.BLIND,
    Labels.BUTTON,
    Labels.COOLING,
    Labels.HEATING,
    Labels.MOTION,
    Labels.PLANT,
    Labels.PRIMARY_LIGHT,
    Labels.SECONDARY_LIGHT,
    Labels.TEMPERATURE,
]


def _new_pairing_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "message": "Ready to start ZHA pairing.",
        "active": False,
        "started_at": 0.0,
        "ends_at": 0.0,
        "duration_s": 90,
        "zha_permit_seconds": 90,
        "areas": [],
        "devices": {},
        "selected_device_id": "",
        "completed_device_id": "",
        "error": "",
    }


_PAIRING_STATE: dict[str, Any] = _new_pairing_state()


def _pairing_plan_rank(plan_id: str) -> int:
    normalized = _normalize_plan_id(plan_id, "")
    match = re.search(r"plan_(\d+)", normalized)
    return int(match.group(1)) if match else 0


def _pairing_is_unlocked() -> bool:
    state = load_license_state()
    plan = _normalize_plan_id(state.get("plan"), "plan_1")
    return _pairing_plan_rank(plan) >= 2


def _pairing_state_payload() -> dict[str, Any]:
    with _PAIRING_LOCK:
        devices = list((_PAIRING_STATE.get("devices") or {}).values())
        devices.sort(key=lambda item: (item.get("status") != "ready", item.get("discovered_at") or 0))
        return {
            "status": _PAIRING_STATE.get("status") or "idle",
            "message": _PAIRING_STATE.get("message") or "",
            "active": bool(_PAIRING_STATE.get("active")),
            "started_at": _PAIRING_STATE.get("started_at") or 0,
            "ends_at": _PAIRING_STATE.get("ends_at") or 0,
            "duration_s": _PAIRING_STATE.get("duration_s") or 90,
            "areas": list(_PAIRING_STATE.get("areas") or []),
            "devices": devices,
            "selected_device_id": _PAIRING_STATE.get("selected_device_id") or "",
            "completed_device_id": _PAIRING_STATE.get("completed_device_id") or "",
            "error": _PAIRING_STATE.get("error") or "",
            "labels": list(_PAIRING_LABEL_OPTIONS),
            "plan": _normalize_plan_id(load_license_state().get("plan"), "plan_1"),
            "plan_unlocked": _pairing_is_unlocked(),
        }


def _pairing_area_names(raw: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for area in raw.get("areas") or []:
        if not isinstance(area, dict):
            continue
        name = str(area.get("name") or "").strip()
        if not name:
            continue
        if slugify(name) == "system":
            continue
        names.append(name)
    return sorted(set(names))


def _pairing_pick_device_name(device: dict[str, Any], device_id: str) -> str:
    return str(device.get("name_by_user") or device.get("name") or device_id or "New device").strip()


def _pairing_infer_labels(entity_ids: list[str]) -> list[str]:
    inferred: list[str] = []
    normalized = [str(entity_id or "").strip().lower() for entity_id in entity_ids if str(entity_id or "").strip()]
    for entity_id in normalized:
        domain = entity_id.split(".", 1)[0]
        if domain == "light":
            inferred.append(Labels.PRIMARY_LIGHT)
        if domain == "cover":
            inferred.append(Labels.BLIND)
        if domain == "button":
            inferred.append(Labels.BUTTON)
        if domain in {"binary_sensor", "sensor"} and any(
            marker in entity_id for marker in ("occupancy", "motion", "presence", "pir", "moving")
        ):
            inferred.append(Labels.MOTION)
        if domain == "sensor" and "temperature" in entity_id:
            inferred.append(Labels.TEMPERATURE)
    deduped: list[str] = []
    for label in inferred:
        if label not in deduped:
            deduped.append(label)
    return deduped


def _capture_pairing_snapshot(settings: Settings) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
    ha.fetch_all(include_users=False)
    raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
    entities_by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in raw.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        device_id = str(entity.get("device_id") or "").strip()
        if not device_id:
            continue
        entities_by_device.setdefault(device_id, []).append(entity)
    return raw, entities_by_device


def _refresh_pairing_devices(raw: dict[str, Any], entities_by_device: dict[str, list[dict[str, Any]]], *, baseline_ids: set[str]) -> None:
    now = time.time()
    devices = raw.get("devices") or []
    area_map = _resolve_area_map(raw)
    with _PAIRING_LOCK:
        _PAIRING_STATE["areas"] = _pairing_area_names(raw)
        pairing_devices = _PAIRING_STATE.setdefault("devices", {})
        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = str(device.get("id") or "").strip()
            if not device_id or device_id in baseline_ids:
                continue
            area_name = area_map.get(str(device.get("area_id") or "").strip(), "")
            if area_name and slugify(area_name) == "system":
                continue
            entity_rows = entities_by_device.get(device_id) or []
            entity_ids = sorted(
                {
                    str(row.get("entity_id") or "").strip()
                    for row in entity_rows
                    if str(row.get("entity_id") or "").strip()
                }
            )
            entity_signature = "|".join(entity_ids)
            candidate = pairing_devices.get(device_id)
            if not isinstance(candidate, dict):
                candidate = {
                    "device_id": device_id,
                    "name": _pairing_pick_device_name(device, device_id),
                    "suggested_name": _pairing_pick_device_name(device, device_id),
                    "manufacturer": str(device.get("manufacturer") or "").strip(),
                    "model": str(device.get("model") or "").strip(),
                    "entity_ids": entity_ids,
                    "entity_count": len(entity_ids),
                    "entity_signature": entity_signature,
                    "discovered_at": now,
                    "stable_since": now,
                    "status": "configuring",
                    "area_name": area_name,
                    "suggested_labels": _pairing_infer_labels(entity_ids),
                }
                pairing_devices[device_id] = candidate
            else:
                if candidate.get("entity_signature") != entity_signature:
                    candidate["entity_signature"] = entity_signature
                    candidate["stable_since"] = now
                candidate["name"] = _pairing_pick_device_name(device, device_id)
                candidate["suggested_name"] = candidate.get("suggested_name") or candidate["name"]
                candidate["manufacturer"] = str(device.get("manufacturer") or "").strip()
                candidate["model"] = str(device.get("model") or "").strip()
                candidate["entity_ids"] = entity_ids
                candidate["entity_count"] = len(entity_ids)
                candidate["area_name"] = area_name
                candidate["suggested_labels"] = _pairing_infer_labels(entity_ids)
            if entity_ids and now - float(candidate.get("stable_since") or now) >= 5:
                candidate["status"] = "ready"
            else:
                candidate["status"] = "configuring"
            candidate["updated_at"] = now
        ready_candidates = [
            device_id
            for device_id, candidate in pairing_devices.items()
            if isinstance(candidate, dict) and candidate.get("status") == "ready"
        ]
        if ready_candidates:
            _PAIRING_STATE["status"] = "ready"
            _PAIRING_STATE["message"] = "New device ready to configure."
            if not _PAIRING_STATE.get("selected_device_id"):
                _PAIRING_STATE["selected_device_id"] = ready_candidates[0]
        elif pairing_devices:
            _PAIRING_STATE["status"] = "configuring"
            _PAIRING_STATE["message"] = "Device found. Waiting for Home Assistant to finish configuring it."
        else:
            _PAIRING_STATE["status"] = "searching"
            _PAIRING_STATE["message"] = "Searching for new ZHA devices..."


def _pairing_monitor_loop(duration_s: int) -> None:
    log = get_logger("pairing")
    baseline_ids: set[str] = set()
    try:
        settings = Settings()
        raw, entities_by_device = _capture_pairing_snapshot(settings)
        baseline_ids = {
            str(device.get("id") or "").strip()
            for device in (raw.get("devices") or [])
            if isinstance(device, dict) and str(device.get("id") or "").strip()
        }
        with _PAIRING_LOCK:
            _PAIRING_STATE.clear()
            _PAIRING_STATE.update(_new_pairing_state())
            _PAIRING_STATE.update(
                {
                    "status": "searching",
                    "message": "Searching for new ZHA devices...",
                    "active": True,
                    "started_at": time.time(),
                    "ends_at": time.time() + duration_s,
                    "duration_s": duration_s,
                    "zha_permit_seconds": duration_s,
                    "areas": _pairing_area_names(raw),
                }
            )
        ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
        ha.call_service("zha", "permit", {"duration": duration_s})
        log.ok(f"ZHA pairing started for {duration_s}s.")
        while True:
            with _PAIRING_LOCK:
                active = bool(_PAIRING_STATE.get("active"))
                ends_at = float(_PAIRING_STATE.get("ends_at") or 0)
            if not active or time.time() >= ends_at:
                break
            raw, entities_by_device = _capture_pairing_snapshot(settings)
            _refresh_pairing_devices(raw, entities_by_device, baseline_ids=baseline_ids)
            time.sleep(2)
        raw, entities_by_device = _capture_pairing_snapshot(settings)
        _refresh_pairing_devices(raw, entities_by_device, baseline_ids=baseline_ids)
        with _PAIRING_LOCK:
            _PAIRING_STATE["active"] = False
            if not (_PAIRING_STATE.get("devices") or {}):
                _PAIRING_STATE["status"] = "idle"
                _PAIRING_STATE["message"] = "No new devices were found during this scan."
            elif _PAIRING_STATE.get("status") == "configuring":
                _PAIRING_STATE["message"] = "Device found. Wait a few more seconds and refresh if needed."
    except Exception as exc:
        with _PAIRING_LOCK:
            _PAIRING_STATE["active"] = False
            _PAIRING_STATE["status"] = "error"
            _PAIRING_STATE["error"] = str(exc)
            _PAIRING_STATE["message"] = f"Pairing failed: {exc}"
        log.error(f"Pairing monitor failed: {exc}")


def _start_pairing_session(duration_s: int = 90) -> dict[str, Any]:
    global _PAIRING_THREAD
    if not _pairing_is_unlocked():
        with _PAIRING_LOCK:
            _PAIRING_STATE.clear()
            _PAIRING_STATE.update(_new_pairing_state())
            _PAIRING_STATE["status"] = "upgrade_required"
            _PAIRING_STATE["message"] = "Upgrade Hausie to add new devices by yourself."
        return _pairing_state_payload()
    with _PAIRING_LOCK:
        if _PAIRING_THREAD and _PAIRING_THREAD.is_alive():
            return _pairing_state_payload()
        _PAIRING_STATE.clear()
        _PAIRING_STATE.update(_new_pairing_state())
        _PAIRING_STATE["status"] = "searching"
        _PAIRING_STATE["message"] = "Starting ZHA pairing..."
        _PAIRING_STATE["active"] = True
    _PAIRING_THREAD = threading.Thread(
        target=_pairing_monitor_loop,
        args=(max(30, min(300, int(duration_s or 90))),),
        name="hausie-pairing",
        daemon=True,
    )
    _PAIRING_THREAD.start()
    return _pairing_state_payload()


def _stop_pairing_session() -> dict[str, Any]:
    with _PAIRING_LOCK:
        _PAIRING_STATE["active"] = False
        if _PAIRING_STATE.get("status") not in {"completed", "error"}:
            _PAIRING_STATE["status"] = "idle"
            _PAIRING_STATE["message"] = "Pairing stopped."
    return _pairing_state_payload()

def _resolve_local_ha_root() -> Path | None:
    env_root = os.getenv("HAUSIE_LOCAL_HA_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    local_root = ROOT_DIR / "hausie" / "homeassistant"
    return local_root if local_root.exists() else None


def _mirror_local_artifact(local_root: Path | None, rel_path: str, content: str, log) -> bool:
    if not local_root or not rel_path:
        return False
    try:
        local_path = (local_root / rel_path).resolve()
        local_path.relative_to(local_root)
    except Exception:
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(str(content), encoding="utf-8")
        return True
    except Exception as exc:
        log.warn(f"Local mirror failed for {rel_path}: {exc}")
        return False


def _remove_local_artifact(local_root: Path | None, rel_path: str, log) -> bool:
    if not local_root or not rel_path:
        return False
    try:
        local_path = (local_root / rel_path).resolve()
        local_path.relative_to(local_root)
    except Exception:
        return False
    try:
        if local_path.is_dir():
            shutil.rmtree(local_path, ignore_errors=True)
        elif local_path.exists():
            local_path.unlink()
        return True
    except Exception as exc:
        log.warn(f"Local mirror delete failed for {rel_path}: {exc}")
        return False


def _start_mqtt_listener() -> None:
    global _MQTT_LISTENER
    if not _resolve_mqtt_enabled():
        return
    host = os.getenv("HAUSIE_MQTT_HOST") or os.getenv("MQTT_HOST")
    if not host:
        get_logger("mqtt").warn("MQTT disabled: HAUSIE_MQTT_HOST not set.")
        return
    ha = _resolve_ha_client()
    if not ha:
        get_logger("mqtt").warn("MQTT disabled: HA_TOKEN not set.")
        return
    port = int(os.getenv("HAUSIE_MQTT_PORT", "1883"))
    username = _resolve_mqtt_secret("HAUSIE_MQTT_USERNAME")
    password = _resolve_mqtt_secret("HAUSIE_MQTT_PASSWORD")
    base_topic = os.getenv("HAUSIE_MQTT_BASE_TOPIC", "hausie")
    device_id, _token = resolve_device_credentials()
    plan = os.getenv("HAUSIE_PLAN")
    qos = int(os.getenv("HAUSIE_MQTT_QOS", "0"))
    keepalive = int(os.getenv("HAUSIE_MQTT_KEEPALIVE", "30"))
    default_title = os.getenv("HAUSIE_MQTT_DEFAULT_TITLE", "Hausie")
    _MQTT_LISTENER = MQTTNotificationListener(
        ha_client=ha,
        host=host,
        port=port,
        username=username,
        password=password,
        base_topic=base_topic,
        device_id=device_id,
        plan=plan,
        qos=qos,
        keepalive=keepalive,
        default_title=default_title,
        default_notify_service=os.getenv("HA_DEFAULT_NOTIFY"),
    )
    _MQTT_LISTENER.start()


def _start_remote_support_manager() -> None:
    global _SUPPORT_MANAGER
    if os.getenv("HAUSIE_SUPPORT_ENABLE", "").strip().lower() in {"0", "false", "no"}:
        return
    ha = _resolve_ha_client()
    if not ha:
        get_logger("support").warn("Remote support disabled: HA_TOKEN not set.")
        return
    toggle_entity = os.getenv("HAUSIE_SUPPORT_TOGGLE_ENTITY", "input_boolean.allow_remote_support")
    auth_keys_path = os.getenv("HAUSIE_SUPPORT_AUTH_KEYS_PATH", "/homeassistant/ssh/authorized_keys")
    timeout_min = int(os.getenv("HAUSIE_SUPPORT_TIMEOUT_MINUTES", "15"))
    poll_s = int(os.getenv("HAUSIE_SUPPORT_POLL_SECONDS", "10"))
    public_keys = _load_public_keys()
    _device_id, token = resolve_device_credentials()
    base = os.getenv("HAUSIE_CLOUD_URL", "").strip().rstrip("/") or DEFAULT_HAUSIE_CLOUD_URL
    support_keys_url = os.getenv("HAUSIE_SUPPORT_KEYS_URL", "").strip()
    support_session_url = os.getenv("HAUSIE_SUPPORT_SESSION_URL", "").strip()
    if not support_keys_url and base:
        support_keys_url = f"{base}/api/device/support-keys"
    if not support_session_url and base:
        support_session_url = f"{base}/api/device/support-session"
    manage_ssh = os.getenv("HAUSIE_SUPPORT_MANAGE_SSH", "true").strip().lower() not in {"0", "false", "no"}
    ssh_slug = os.getenv("SSH_ADDON_SLUG", "").strip() or _autodetect_addon_slug("ssh") or "a0d7b954_ssh"
    tailscale_slug = _autodetect_addon_slug("tailscale") or "a0d7b954_tailscale"
    _SUPPORT_MANAGER = RemoteSupportManager(
        ha_client=ha,
        toggle_entity=toggle_entity,
        auth_keys_path=auth_keys_path,
        public_keys=public_keys,
        timeout_s=timeout_min * 60,
        poll_s=poll_s,
        state_path=os.getenv("HAUSIE_SUPPORT_STATE_PATH", "/data/hausie_support_state.json"),
        manage_ssh_addon=manage_ssh,
        ssh_addon_slug=ssh_slug,
        manage_tailscale_addon=True,
        tailscale_addon_slug=tailscale_slug,
        support_session_url=support_session_url,
        support_keys_url=support_keys_url,
        device_token=token,
        on_state_change=_send_heartbeat_now,
    )
    _SUPPORT_MANAGER.start()


def _send_heartbeat_now(_support_active: bool | None = None) -> None:
    heartbeat = _HEARTBEAT
    if not heartbeat:
        return
    threading.Thread(target=heartbeat.send_now, daemon=True).start()


def _register_with_pairing_code(pairing_code: str) -> dict[str, str | bool]:
    pairing_code = str(pairing_code or "").strip()
    if not pairing_code:
        raise ValueError("Hausie pairing code is required.")
    log = get_logger("register")
    device_id, token = resolve_device_credentials()
    base_url = os.getenv("HAUSIE_CLOUD_URL", "").strip() or DEFAULT_HAUSIE_CLOUD_URL
    if device_id and token:
        try:
            cloud = CloudClient(base_url=base_url, token=token, timeout_s=20)
            if cloud.has_valid_device_credentials():
                log.ok(f"Pairing skipped: device already registered ({device_id}).")
                return {"device_id": device_id, "paired": True, "already_paired": True}
            log.warn(f"Stored device credentials are no longer valid for {device_id}; relinking with pairing code.")
        except Exception as exc:
            raise RuntimeError(f"Pairing validation failed: {exc}") from exc
    log.start("Registering add-on with pairing code.")
    try:
        cloud = CloudClient(base_url=base_url, timeout_s=20)
        response = cloud.register_device({"pairing_code": pairing_code})
    except Exception as exc:
        raise RuntimeError(f"Pairing failed: {exc}") from exc
    device_id = str(response.get("hausie_device_id") or "").strip()
    token = str(response.get("device_token") or "").strip()
    if not device_id or not token:
        raise RuntimeError("Pairing failed: missing device credentials in response.")
    state_path = resolve_state_path()
    if state_path.exists():
        try:
            state_path.unlink()
        except Exception:
            pass
    persist_device_credentials(device_id, token)
    os.environ["HAUSIE_DEVICE_ID"] = device_id
    os.environ["HAUSIE_CLOUD_TOKEN"] = token
    log.ok(f"Pairing completed: {device_id}")
    return {"device_id": device_id, "paired": True, "already_paired": False}


def _auto_register_from_pairing_code() -> None:
    pairing_code = os.getenv("HAUSIE_PAIRING_CODE", "").strip()
    if not pairing_code:
        return
    try:
        _register_with_pairing_code(pairing_code)
    except Exception as exc:
        get_logger("register").warn(str(exc))


def _start_heartbeat() -> None:
    global _HEARTBEAT
    if os.getenv("HAUSIE_HEARTBEAT_ENABLE", "true").strip().lower() in {"0", "false", "no"}:
        return
    ha = _resolve_ha_client()
    if not ha:
        get_logger("heartbeat").warn("Heartbeat disabled: HA_TOKEN not set.")
        return
    device_id, token = resolve_device_credentials()
    device_id = (device_id or "").strip()
    if not device_id:
        get_logger("heartbeat").warn("Heartbeat disabled: HAUSIE_DEVICE_ID not set.")
        return
    base = os.getenv("HAUSIE_CLOUD_URL", "").strip().rstrip("/") or DEFAULT_HAUSIE_CLOUD_URL
    endpoint = os.getenv("HAUSIE_HEARTBEAT_URL", "").strip()
    if not endpoint:
        if not base:
            get_logger("heartbeat").warn("Heartbeat disabled: HAUSIE_CLOUD_URL not set.")
            return
        endpoint = f"{base}/api/device/heartbeat"
    interval_s = _resolve_heartbeat_interval()
    support_interval_s = _resolve_support_heartbeat_interval()
    _HEARTBEAT = HeartbeatReporter(
        ha_client=ha,
        endpoint_url=endpoint,
        device_id=device_id,
        token=token,
        interval_s=interval_s,
        support_interval_s=support_interval_s,
        state_path=os.getenv("HAUSIE_SUPPORT_STATE_PATH", "/data/hausie_support_state.json"),
        on_actions=_handle_heartbeat_actions,
    )
    _HEARTBEAT.start()


def _start_license_monitor() -> None:
    global _LICENSE_MONITOR_THREAD
    if _LICENSE_MONITOR_THREAD and _LICENSE_MONITOR_THREAD.is_alive():
        return

    def _loop() -> None:
        log = get_logger("license")
        while not _LICENSE_MONITOR_STOP.is_set():
            try:
                license_state = load_license_state()
                status = str(license_state.get("license_status") or "").strip().lower()
                if status != "downgraded" and (
                    _license_clock_invalid(license_state) or _license_time_expired(license_state)
                ):
                    downgraded_license = dict(license_state)
                    downgraded_license.update(
                        {
                            "plan": "plan_1",
                            "license_status": "downgraded",
                            "offline_valid_until": None,
                            "addon_message": {
                                "title": "Free plan active",
                                "body": "This home was downgraded to the free plan because the subscription could not be validated.",
                            },
                        }
                    )
                    log.warn("License validation expired locally; downgrading to free plan.")
                    _run_apply_plan(
                        target_plan="plan_1",
                        license_payload=downgraded_license,
                        allow_local_free_fallback=True,
                    )
            except Exception as exc:
                get_logger("license").warn(f"License monitor failed: {exc}")
            _LICENSE_MONITOR_STOP.wait(300)

    _LICENSE_MONITOR_STOP.clear()
    _LICENSE_MONITOR_THREAD = threading.Thread(target=_loop, daemon=True)
    _LICENSE_MONITOR_THREAD.start()


def _canonicalize_inventory_signature_value(value: Any) -> Any:
    """Normalize inventory values so ordering noise does not change the signature."""
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_inventory_signature_value(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        normalized = [_canonicalize_inventory_signature_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )
    return value


def _build_inventory_signature(raw: dict[str, Any], labels: list[dict[str, Any]]) -> str:
    """Return a stable hash for the current HA inventory snapshot."""
    snapshot = {
        "areas": raw.get("areas") or [],
        "devices": raw.get("devices") or [],
        "entities": raw.get("entities") or [],
        "labels": labels or [],
    }
    canonical = _canonicalize_inventory_signature_value(snapshot)
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_inventory_monitor_interval() -> int:
    raw = os.getenv("HAUSIE_INVENTORY_MONITOR_INTERVAL_S", "").strip()
    try:
        interval = int(raw) if raw else 300
    except ValueError:
        interval = 300
    return max(60, interval)


def _capture_inventory_signature(settings: Settings) -> str:
    """Fetch the latest HA inventory snapshot and return its signature."""
    ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
    ha.fetch_all(include_users=False)
    raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
    labels = ha.fetch_labels()
    return _build_inventory_signature(raw, labels)


def _start_inventory_monitor() -> None:
    global _INVENTORY_MONITOR_THREAD
    if _INVENTORY_MONITOR_THREAD and _INVENTORY_MONITOR_THREAD.is_alive():
        return

    def _loop() -> None:
        log = get_logger("inventory")
        interval_s = _resolve_inventory_monitor_interval()
        while not _INVENTORY_MONITOR_STOP.is_set():
            try:
                settings = Settings()
                if not settings.HA_TOKEN or not settings.HAUSIE_CLOUD_URL:
                    _INVENTORY_MONITOR_STOP.wait(interval_s)
                    continue
                current_signature = _capture_inventory_signature(settings)
                state = load_device_state()
                last_signature = str(state.get("last_inventory_signature") or "").strip()
                if not last_signature:
                    state["last_inventory_signature"] = current_signature
                    state["last_inventory_baselined_at"] = int(time.time())
                    state.pop("inventory_change_pending", None)
                    save_device_state(state)
                    log.info("Inventory signature missing; stored startup baseline without auto-sync.")
                elif current_signature != last_signature:
                    state["last_inventory_signature"] = current_signature
                    state["last_inventory_changed_at"] = int(time.time())
                    state.pop("inventory_change_pending", None)
                    save_device_state(state)
                    log.info("Inventory change detected; updated local baseline without auto-sync.")
            except RuntimeError as exc:
                log.info(f"Inventory monitor skipped: {exc}")
            except Exception as exc:
                log.warn(f"Inventory monitor failed: {exc}")
            _INVENTORY_MONITOR_STOP.wait(interval_s)

    _INVENTORY_MONITOR_STOP.clear()
    _INVENTORY_MONITOR_THREAD = threading.Thread(target=_loop, daemon=True)
    _INVENTORY_MONITOR_THREAD.start()


def _supervisor_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    token = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if not token:
        if raise_on_error:
            raise RuntimeError("Supervisor token is unavailable.")
        return {}
    url = f"http://supervisor{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, url, headers=headers, json=payload, timeout=15)
    if resp.status_code // 100 != 2:
        if raise_on_error:
            try:
                error_payload = resp.json()
            except Exception:
                error_payload = {}
            if not isinstance(error_payload, dict):
                error_payload = {}
            detail = str(
                error_payload.get("message")
                or error_payload.get("error")
                or resp.text
                or f"HTTP {resp.status_code}"
            ).strip()
            raise RuntimeError(f"Supervisor request {method} {path} failed: {detail}")
        return {}
    try:
        payload = resp.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_self_addon_slug() -> str:
    """Return the Supervisor-assigned slug for this installed add-on."""
    global _SELF_ADDON_SLUG
    if _SELF_ADDON_SLUG:
        return _SELF_ADDON_SLUG

    data = _supervisor_request("GET", "/addons/self/info")
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    candidate = str(body.get("slug") or "").strip() if isinstance(body, dict) else ""
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", candidate):
        _SELF_ADDON_SLUG = candidate
        return candidate

    configured = str(os.getenv("HAUSIE_ADDON_SLUG") or "").strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", configured):
        return configured
    return "hausie"


def _resolve_hausie_app_path(suffix: str) -> str:
    return f"/config/app/{_resolve_self_addon_slug()}/{suffix.lstrip('/')}"


def _resolve_hausie_app_page_path() -> str:
    return f"/app/{_resolve_self_addon_slug()}"


def _resolve_pairing_ingress_path() -> str:
    return _resolve_hausie_app_path("ingress/pairing")


def _resolve_credentials_ingress_path() -> str:
    return _resolve_hausie_app_page_path()


def _resolve_setup_ingress_path() -> str:
    return _resolve_hausie_app_page_path()


def _resolve_hausie_app_info_path() -> str:
    return _resolve_hausie_app_page_path()


def _normalize_ingress_path(value: str | None) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or "://" in path:
        return ""
    return path.rstrip("/")


def _autodetect_addon_slug(*keywords: str) -> str | None:
    lowered = [str(item or "").strip().lower() for item in keywords if str(item or "").strip()]
    if not lowered:
        return None
    data = _supervisor_request("GET", "/addons")
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    addons = body.get("addons") if isinstance(body, dict) else None
    if not isinstance(addons, list):
        return None
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        candidates = [
            str(addon.get("slug") or "").strip(),
            str(addon.get("name") or "").strip(),
            str(addon.get("repository") or "").strip(),
        ]
        haystack = " ".join(item.lower() for item in candidates if item)
        if haystack and all(keyword in haystack for keyword in lowered):
            slug = str(addon.get("slug") or "").strip()
            if slug:
                return slug
    return None


def _patch_add_device_shortcut(log) -> None:
    dashboard_path = resolve_config_dashboard_path()
    if not dashboard_path.exists() or not _pairing_is_unlocked():
        return
    try:
        doc = yaml.safe_load(dashboard_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warn(f"Add Device shortcut patch skipped: {exc}")
        return
    if not isinstance(doc, dict):
        return
    target_path = _resolve_pairing_ingress_path()
    updated = False
    for view in doc.get("views") or []:
        if not isinstance(view, dict) or str(view.get("path") or "").strip() != "main":
            continue
        for section in view.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for card in section.get("cards") or []:
                if not isinstance(card, dict) or str(card.get("name") or "").strip() != "Add Device":
                    continue
                tap_action = card.get("tap_action") if isinstance(card.get("tap_action"), dict) else {}
                if tap_action.get("action") == "url" and str(tap_action.get("url_path") or "").strip() == target_path:
                    return
                card["tap_action"] = {
                    "action": "url",
                    "url_path": target_path,
                }
                updated = True
    if updated:
        dashboard_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log.ok(f"Patched Add Device shortcut to {target_path}.")


def _ha_credentials_status_payload() -> dict[str, Any]:
    token, username, password = resolve_ha_runtime_credentials()
    state = load_device_state()
    admin_password_configured = bool(state.get("hausie_admin_password_configured"))
    validation = state.get("ha_credentials_validation") if isinstance(state.get("ha_credentials_validation"), dict) else {}
    credentials_valid = bool(validation.get("valid"))
    validation_error = str(validation.get("error") or "").strip()
    missing_fields: list[str] = []
    if not token:
        missing_fields.append("ha_token")
    if not password:
        missing_fields.append("support_password")
    if not admin_password_configured:
        missing_fields.append("admin_password")
    return {
        "has_token": bool(token),
        "has_support_password": bool(password),
        "has_admin_password": admin_password_configured,
        "credentials_valid": credentials_valid,
        "validation_error": validation_error,
        "admin_username": HAUSIE_ADMIN_USERNAME,
        "support_username": username or HAUSIE_SUPPORT_USERNAME,
        "missing_fields": missing_fields,
        "setup_required": bool(missing_fields) or not credentials_valid,
    }


def _set_setup_progress(status: str, message: str, *, initialized: bool = False) -> None:
    state = load_device_state()
    device_id, _token = resolve_device_credentials()
    state["bootstrap_setup"] = {
        "status": status,
        "message": message,
        "updated_at": int(time.time()),
        "initialized": bool(initialized),
        "device_id": str(device_id or ""),
    }
    save_device_state(state)


def _setup_status_payload() -> dict[str, Any]:
    credentials = _ha_credentials_status_payload()
    device_id, device_token = resolve_device_credentials()
    paired = bool(device_id and device_token)
    state = load_device_state()
    setup_state = state.get("bootstrap_setup") if isinstance(state.get("bootstrap_setup"), dict) else {}
    initialized = bool(
        paired
        and setup_state.get("initialized")
        and str(setup_state.get("device_id") or "") == str(device_id or "")
    )

    if setup_state.get("status") == "initializing":
        phase = "initializing"
        message = str(setup_state.get("message") or "Initializing Hausie...")
    elif setup_state.get("status") == "failed":
        phase = "failed"
        message = str(setup_state.get("message") or "Hausie initialization failed. Review the add-on logs and try again.")
    elif credentials["setup_required"]:
        phase = "credentials"
        message = credentials["validation_error"] or "Enter the Home Assistant token and the Hausie account passwords."
    elif not paired:
        phase = "pairing"
        message = "Enter the pairing code for this Hausie home."
    elif initialized:
        phase = "complete"
        message = "Hausie is initialized for this home."
    else:
        phase = "ready"
        message = "Ready to initialize Hausie."

    return {
        "credentials": credentials,
        "has_token": credentials["has_token"],
        "has_support_password": credentials["has_support_password"],
        "has_admin_password": credentials["has_admin_password"],
        "credentials_valid": credentials["credentials_valid"],
        "validation_error": credentials["validation_error"],
        "paired": paired,
        "device_id": str(device_id or ""),
        "initialized": initialized,
        "initializing": phase == "initializing",
        "phase": phase,
        "message": message,
    }


def _credentials_require_setup() -> bool:
    return bool(_ha_credentials_status_payload()["setup_required"])


def _build_credentials_shortcut_card(target_path: str) -> dict[str, Any]:
    return {
        "show_name": True,
        "show_icon": True,
        "type": "button",
        "icon": "mdi:key-chain-variant",
        "grid_options": {"columns": 12, "rows": 2},
        "name": "Set Hausie credentials",
        "tap_action": {
            "action": "url",
            "url_path": target_path,
        },
    }


def _patch_credentials_shortcut(log) -> None:
    dashboard_path = resolve_config_dashboard_path()
    if not dashboard_path.exists():
        return
    try:
        doc = yaml.safe_load(dashboard_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warn(f"Credentials shortcut patch skipped: {exc}")
        return
    if not isinstance(doc, dict):
        return

    target_path = _resolve_credentials_ingress_path()
    desired_card = _build_credentials_shortcut_card(target_path)
    needs_setup = _credentials_require_setup()
    updated = False

    for view in doc.get("views") or []:
        if not isinstance(view, dict) or str(view.get("path") or "").strip() != "main":
            continue
        target_cards: list[dict[str, Any]] | None = None
        for section in view.get("sections") or []:
            if not isinstance(section, dict):
                continue
            cards = section.get("cards") or []
            if any(isinstance(card, dict) and str(card.get("name") or "").strip() == "Refresh Hausie" for card in cards):
                target_cards = cards
                break
        if target_cards is None:
            continue

        existing_index = next(
            (
                idx
                for idx, card in enumerate(target_cards)
                if isinstance(card, dict) and str(card.get("name") or "").strip() == "Set Hausie credentials"
            ),
            None,
        )

        if needs_setup:
            if existing_index is None:
                refresh_index = next(
                    (
                        idx
                        for idx, card in enumerate(target_cards)
                        if isinstance(card, dict) and str(card.get("name") or "").strip() == "Refresh Hausie"
                    ),
                    0,
                )
                target_cards.insert(refresh_index, desired_card)
                updated = True
            elif target_cards[existing_index] != desired_card:
                target_cards[existing_index] = desired_card
                updated = True
        elif existing_index is not None:
            target_cards.pop(existing_index)
            updated = True

    if updated:
        dashboard_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        action = "added" if needs_setup else "removed"
        log.ok(f"Credentials shortcut {action} in config dashboard.")


def _patch_hausie_app_shortcut(log) -> None:
    """Keep the persisted Hausie App shortcut aligned with this add-on's slug."""
    dashboard_path = resolve_config_dashboard_path()
    if not dashboard_path.exists():
        return
    try:
        doc = yaml.safe_load(dashboard_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warn(f"Hausie App shortcut patch skipped: {exc}")
        return
    if not isinstance(doc, dict):
        return

    target_path = _resolve_hausie_app_info_path()
    updated = False
    for view in doc.get("views") or []:
        if not isinstance(view, dict) or str(view.get("path") or "").strip() != "main":
            continue
        for section in view.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for card in section.get("cards") or []:
                if not isinstance(card, dict) or str(card.get("name") or "").strip() != "Hausie App":
                    continue
                desired_action = {"action": "url", "url_path": target_path}
                if card.get("tap_action") != desired_action:
                    card["tap_action"] = desired_action
                    updated = True

    if updated:
        dashboard_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log.ok(f"Patched Hausie App shortcut to {target_path}.")


def _save_ha_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("credentials")
    requested_token = str(payload.get("ha_token") or payload.get("token") or "").strip()
    requested_support_password = str(payload.get("support_password") or payload.get("ha_ui_password") or "").strip()
    requested_admin_password = str(payload.get("admin_password") or "").strip()
    current_token, _current_username, current_password = resolve_ha_runtime_credentials()
    state = load_device_state()
    admin_password_configured = bool(state.get("hausie_admin_password_configured"))

    token_to_use = requested_token or current_token
    support_password_to_use = requested_support_password or current_password
    if not token_to_use:
        raise ValueError("Home Assistant token is required.")
    if not support_password_to_use:
        raise ValueError("Support user password is required.")
    if not requested_admin_password and not admin_password_configured:
        raise ValueError("Hausie administrator password is required.")

    os.environ["HA_TOKEN"] = token_to_use
    os.environ["HA_UI_USERNAME"] = HAUSIE_SUPPORT_USERNAME
    os.environ["HA_UI_PASSWORD"] = support_password_to_use

    ha = _resolve_ha_client()
    if not ha:
        raise RuntimeError("Home Assistant client is unavailable. Check the access token and retry.")

    try:
        users = ha.fetch_users()
        users_by_username = {
            str(user.get("username") or user.get("name") or "").strip().lower(): user
            for user in users
            if isinstance(user, dict)
        }

        if requested_admin_password:
            admin_user = users_by_username.get(HAUSIE_ADMIN_USERNAME)
            if admin_user:
                ha.change_auth_user_password(admin_user.get("id"), requested_admin_password)
                log.ok(f"Administrator password updated: {HAUSIE_ADMIN_USERNAME}")
            else:
                ha.create_auth_user(
                    name="Hausie Administrator",
                    username=HAUSIE_ADMIN_USERNAME,
                    password=requested_admin_password,
                    is_admin=True,
                    local_only=True,
                )
                log.ok(f"Administrator user created: {HAUSIE_ADMIN_USERNAME}")
            state["hausie_admin_password_configured"] = True
            save_device_state(state)

        if requested_support_password:
            support_user = users_by_username.get(HAUSIE_SUPPORT_USERNAME)
            if support_user:
                if not support_user.get("isAdmin"):
                    raise RuntimeError(
                        f"Existing support account '{HAUSIE_SUPPORT_USERNAME}' is not an administrator."
                    )
                ha.change_auth_user_password(support_user.get("id"), support_password_to_use)
                log.ok(f"Support user password updated: {HAUSIE_SUPPORT_USERNAME}")
            else:
                ha.create_auth_user(
                    name="Hausie Support User",
                    username=HAUSIE_SUPPORT_USERNAME,
                    password=support_password_to_use,
                    is_admin=True,
                    local_only=True,
                )
                log.ok(f"Support user created: {HAUSIE_SUPPORT_USERNAME}")

        if requested_admin_password or requested_support_password:
            try:
                _supervisor_request("DELETE", "/auth/cache", raise_on_error=True)
            except Exception as exc:
                log.warn(f"Home Assistant auth cache reset skipped: {exc}")
    except Exception as exc:
        raise RuntimeError(f"Could not update local Hausie users: {exc}") from exc

    persist_ha_runtime_credentials(
        ha_token=requested_token or None,
        ha_ui_username=HAUSIE_SUPPORT_USERNAME,
        ha_ui_password=requested_support_password or None,
    )
    validation = _validate_ha_credentials(log)
    if not validation["credentials_valid"]:
        raise RuntimeError(validation["validation_error"] or "Home Assistant credentials could not be verified.")

    try:
        _sync_local_config()
    except Exception as exc:
        log.warn(f"Config dashboard refresh skipped after credentials save: {exc}")

    if _MQTT_LISTENER is None:
        try:
            _start_mqtt_listener()
        except Exception:
            pass
    if _SUPPORT_MANAGER is None:
        try:
            _start_remote_support_manager()
        except Exception:
            pass
    if _HEARTBEAT is None:
        try:
            _start_heartbeat()
        except Exception:
            pass
    try:
        _start_license_monitor()
    except Exception:
        pass
    try:
        _start_inventory_monitor()
    except Exception:
        pass

    return validation


def _sync_local_config() -> None:
    if os.getenv("HAUSIE_SYNC_CONFIG_ON_START", "true").strip().lower() in {"0", "false", "no"}:
        return
    config_path = os.getenv("PI_CONFIG_PATH", "/homeassistant/configuration.yaml").strip()
    if not config_path:
        return
    helper_created = False
    try:
        helper_created = _ensure_remote_support_helper(_ha_config_root())
        manager = ConfigManager(
            pi_sender=None,
            config_path=config_path,
            require_remote=False,
        )
        manager.sync_config_dashboard()
        _ensure_bootstrap_config_dashboard()
        _patch_credentials_shortcut(get_logger("config"))
        _patch_add_device_shortcut(get_logger("config"))
        _patch_hausie_app_shortcut(get_logger("config"))
        get_logger("config").ok("configuration.yaml synced (local).")
    except Exception as exc:
        get_logger("config").warn(f"configuration.yaml sync failed: {exc}")
        return

    if not helper_created:
        return
    ha = _resolve_ha_client()
    if not ha:
        return
    try:
        ha.call_service("input_boolean", "reload", {})
        get_logger("config").ok("Reloaded input_boolean helpers after restoring remote support toggle.")
    except Exception as exc:
        get_logger("config").warn(f"input_boolean.reload failed after restoring remote support toggle: {exc}")


def _resolve_ha_client() -> HAClient | None:
    token, _username, _password = resolve_ha_runtime_credentials()
    if not token:
        return None
    ha_ws_url = os.getenv("HA_WS_URL", "ws://homeassistant:8123/api/websocket")
    ha_rest_url = os.getenv("HA_REST_URL", "http://homeassistant:8123/api")
    return HAClient(ha_url_ws=ha_ws_url, ha_url_rest=ha_rest_url, token=token)


def _validate_ha_credentials(log=None) -> dict[str, Any]:
    """Validate the local token and both required Hausie accounts once per startup/save."""
    state = load_device_state()
    token, _username, support_password = resolve_ha_runtime_credentials()
    valid = False
    error = ""
    if not token or not support_password or not state.get("hausie_admin_password_configured"):
        error = "Home Assistant token and both Hausie account passwords are required."
    else:
        ha = _resolve_ha_client()
        try:
            if not ha:
                raise RuntimeError("Home Assistant client is unavailable.")
            users = ha.fetch_users()
            usernames = {
                str(user.get("username") or user.get("name") or "").strip().lower()
                for user in users
                if isinstance(user, dict)
            }
            required = {HAUSIE_ADMIN_USERNAME, HAUSIE_SUPPORT_USERNAME}
            missing_users = sorted(required - usernames)
            if missing_users:
                raise RuntimeError(f"Missing local Hausie user: {', '.join(missing_users)}.")
            valid = True
        except Exception as exc:
            error = f"Credential verification failed: {exc}"

    state["ha_credentials_validation"] = {
        "valid": valid,
        "error": error,
        "checked_at": int(time.time()),
    }
    save_device_state(state)
    if log:
        if valid:
            log.ok("Local Hausie credentials verified.")
        elif error:
            log.warn(error)
    return _ha_credentials_status_payload()


def _set_hausie_system_state(ha: HAClient | None, *, busy: bool, status: str) -> None:
    if not ha:
        return
    try:
        ha.call_service(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.hausie_system_status",
                "value": str(status or "Idle"),
            },
        )
    except Exception:
        pass
    try:
        ha.call_service(
            "input_boolean",
            "turn_on" if busy else "turn_off",
            {"entity_id": "input_boolean.hausie_system_busy"},
        )
    except Exception:
        pass


@contextmanager
def _workflow_activity(status: str, *, manage_lock: bool = True):
    if manage_lock and not _WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("Another Hausie action is already in progress.")
    ha = _resolve_ha_client()
    _set_hausie_system_state(ha, busy=True, status=status)
    try:
        yield
    finally:
        _set_hausie_system_state(ha, busy=False, status="Idle")
        if manage_lock:
            _WORKFLOW_LOCK.release()


def _resolve_heartbeat_interval() -> int:
    try:
        state = load_device_state()
        override = state.get("heartbeat_interval_override_s")
        if isinstance(override, (int, float)) and int(override) >= 60:
            return int(override)
    except Exception:
        pass
    config_path = ROOT_DIR / "config" / "heartbeat_settings.yaml"
    if config_path.exists():
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            interval = data.get("heartbeat", {}).get("interval_seconds")
            if isinstance(interval, (int, float)):
                return max(60, int(interval))
            if isinstance(interval, str) and interval.strip().isdigit():
                return max(60, int(interval.strip()))
        except Exception:
            pass
    return 900


def _resolve_support_heartbeat_interval() -> int | None:
    try:
        state = load_device_state()
        override = state.get("support_heartbeat_interval_override_s")
        if isinstance(override, (int, float)) and 5 <= int(override) <= 300:
            return int(override)
    except Exception:
        pass
    raw = os.getenv("HAUSIE_SUPPORT_HEARTBEAT_INTERVAL", "").strip()
    if not raw:
        return None
    try:
        return max(5, min(300, int(raw)))
    except Exception:
        return None


def _create_ha_support_user(action: dict[str, Any]) -> None:
    ha = _resolve_ha_client()
    if not ha:
        raise RuntimeError("HA_TOKEN not set.")
    username = str(action.get("username") or "hausie_support_temp").strip()
    password = str(action.get("password") or "").strip()
    name = str(action.get("name") or "Hausie Support Temp").strip()
    if not password:
        raise RuntimeError("Support user password missing.")
    try:
        ha.delete_auth_user_by_username(username)
    except Exception as exc:
        get_logger("support").warn(f"Could not remove existing support user before create: {exc}")
    ha.create_auth_user(
        name=name,
        username=username,
        password=password,
        is_admin=bool(action.get("is_admin", True)),
        local_only=bool(action.get("local_only", False)),
    )
    get_logger("support").ok(f"HA UI support user ready: {username}")


def _delete_ha_support_user(action: dict[str, Any]) -> None:
    ha = _resolve_ha_client()
    if not ha:
        raise RuntimeError("HA_TOKEN not set.")
    username = str(action.get("username") or "hausie_support_temp").strip()
    deleted = ha.delete_auth_user_by_username(username)
    if deleted:
        get_logger("support").ok(f"HA UI support user removed: {username}")
    else:
        get_logger("support").warn(f"HA UI support user not found: {username}")


_HEARTBEAT_ALLOWED_ACTIONS = {
    "apply_plan",
    "cleanup_base",
    "cleanup_base_assets",
    "cleanup_hausie",
    "cleanup_hausie_assets",
    "create_base",
    "create_hausie",
    "sync_inventory",
    "create_ha_support_user",
    "delete_ha_support_user",
    "rebuild_hausie",
    "refresh_plan",
    "reset_pairing",
    "restart_hausie",
    "sync_label_catalog",
    "sync_help_messages",
    "update_heartbeat_interval",
    "update_plan",
}


def _normalize_heartbeat_action(action: Any) -> dict[str, Any] | None:
    if isinstance(action, str):
        action_type = action.strip().lower()
        if action_type not in _HEARTBEAT_ALLOWED_ACTIONS:
            return None
        return {"type": action_type, "payload": {}, "raw": action}

    if not isinstance(action, dict):
        return None

    action_type = str(action.get("type") or action.get("action") or "").strip().lower()
    if action_type not in _HEARTBEAT_ALLOWED_ACTIONS:
        return None

    try:
        expires_at = int(action.get("expires_at") or 0)
    except Exception:
        expires_at = 0
    if expires_at and expires_at < int(time.time()):
        get_logger("heartbeat").warn(f"Expired heartbeat action skipped: {action_type}")
        return None

    payload = action.get("payload")
    if isinstance(payload, dict):
        merged = dict(payload)
    else:
        merged = {
            str(key): value
            for key, value in action.items()
            if key
            not in {
                "action",
                "expires_at",
                "id",
                "payload",
                "requested_at",
                "schema_version",
                "source",
                "type",
            }
        }
    merged["type"] = action_type
    return {
        "id": action.get("id"),
        "type": action_type,
        "payload": merged,
        "raw": action,
    }


def _handle_heartbeat_actions(actions: list[Any], payload: dict[str, Any] | None = None) -> None:
    global _HEARTBEAT_ACTION_RUNNING
    global _HEARTBEAT
    payload_data = payload if isinstance(payload, dict) else {}
    license_payload = payload_data.get("license") if isinstance(payload_data.get("license"), dict) else None
    log = get_logger("heartbeat")
    if license_payload:
        _store_license_payload(license_payload, log)
    if not actions:
        return
    with _HEARTBEAT_ACTION_LOCK:
        if _HEARTBEAT_ACTION_RUNNING:
            get_logger("heartbeat").warn("Heartbeat actions skipped: already running.")
            return
        _HEARTBEAT_ACTION_RUNNING = True
    try:
        normalized: list[dict[str, Any]] = []
        for action in actions:
            normalized_action = _normalize_heartbeat_action(action)
            if normalized_action:
                normalized.append(normalized_action)
            else:
                log.warn(f"Unknown heartbeat action: {action}")
        if not normalized:
            return
        action_names = [str(action.get("type") or "unknown") for action in normalized]
        log.start(f"Heartbeat actions received: {', '.join(action_names)}")
        lower_actions = [str(action.get("type") or "").strip().lower() for action in normalized]
        if "reset_pairing" in lower_actions:
            with log.script("reset_pairing"):
                state_path = resolve_state_path()
                if state_path.exists():
                    try:
                        state_path.unlink()
                    except Exception:
                        pass
                os.environ.pop("HAUSIE_DEVICE_ID", None)
                os.environ.pop("HAUSIE_CLOUD_TOKEN", None)
                if _HEARTBEAT:
                    _HEARTBEAT.stop()
                    _HEARTBEAT = None
                _auto_register_from_pairing_code()
                _start_heartbeat()
            return
        if "refresh_plan" in lower_actions or "update_plan" in lower_actions:
            with log.script("refresh_plan"):
                target_plan = _normalize_plan_id(license_payload.get("plan"))
                _run_apply_plan(
                    target_plan=target_plan,
                    license_payload=license_payload if license_payload else None,
                    allow_local_free_fallback=False,
                )
            return
        if "apply_plan" in lower_actions:
            with log.script("apply_plan"):
                for action in normalized:
                    if str(action.get("type") or "").strip().lower() != "apply_plan":
                        continue
                    action_payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                    target_plan = _normalize_plan_id(action_payload.get("target_plan"))
                    license_data = action_payload.get("license") if isinstance(action_payload.get("license"), dict) else license_payload
                    _run_apply_plan(target_plan=target_plan, license_payload=license_data, allow_local_free_fallback=False)
            normalized = [action for action in normalized if str(action.get("type") or "").strip().lower() != "apply_plan"]
            if not normalized:
                return
        if "sync_help_messages" in lower_actions:
            with log.script("sync_help_messages"):
                _sync_help_messages_from_cloud(log)
            normalized = [action for action in normalized if str(action.get("type") or "").strip().lower() != "sync_help_messages"]
            if not normalized:
                return
        if "sync_label_catalog" in lower_actions:
            with log.script("sync_label_catalog"):
                settings = Settings()
                ha = HAClient(
                    ha_url_ws=settings.HA_WS_URL,
                    ha_url_rest=settings.HA_REST_URL,
                    token=settings.HA_TOKEN,
                )
                for action in normalized:
                    if str(action.get("type") or "").strip().lower() != "sync_label_catalog":
                        continue
                    payload_data = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                    expected_version = payload_data.get("version")
                    try:
                        expected_version = int(expected_version) if expected_version is not None else None
                    except Exception:
                        expected_version = None
                    _sync_label_catalog_from_cloud(
                        ha,
                        settings,
                        log,
                        force=False,
                        expected_version=expected_version,
                    )
            normalized = [action for action in normalized if str(action.get("type") or "").strip().lower() != "sync_label_catalog"]
            if not normalized:
                return
        if "update_heartbeat_interval" in lower_actions:
            with log.script("update_heartbeat_interval"):
                for action in normalized:
                    if str(action.get("type") or "").strip().lower() == "update_heartbeat_interval":
                        payload_data = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                        _apply_heartbeat_interval_override(payload_data, log)
            normalized = [action for action in normalized if str(action.get("type") or "").strip().lower() != "update_heartbeat_interval"]
            if not normalized:
                return
        for action in normalized:
            action_type = str(action.get("type") or "").strip().lower()
            action_payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
            if action_type == "create_ha_support_user":
                _create_ha_support_user(action_payload)
                continue
            if action_type == "delete_ha_support_user":
                _delete_ha_support_user(action_payload)
                continue
            if action_type in {"cleanup_base", "cleanup_base_assets"}:
                with log.script("cleanup_base"):
                    _cleanup_base_assets()
                continue
            if action_type in {"cleanup_hausie", "cleanup_hausie_assets"}:
                with log.script("cleanup_hausie"):
                    _cleanup_hausie_assets()
                continue
            if action_type == "create_base":
                _run_create_base()
                continue
            if action_type in {"create_hausie", "sync_inventory"}:
                _run_sync_inventory()
                continue
            if action_type == "rebuild_hausie":
                _run_rebuild_hausie()
                continue
            if action_type == "restart_hausie":
                _run_restart_hausie()
                continue
            log.warn(f"Unknown heartbeat action: {action_type or action.get('raw')}")
    finally:
        with _HEARTBEAT_ACTION_LOCK:
            _HEARTBEAT_ACTION_RUNNING = False


def _resolve_pi_dashboard_dir() -> str:
    root = os.getenv("PI_HA_CONFIG_DIR", "/homeassistant")
    for suffix in ("/helpers", "/scripts", "/groups", "/automations", "/dashboards"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    return os.getenv("PI_DASHBOARD_DIR") or f"{root}/dashboards"


def _sync_config_dashboard_to_pi(local_path: Path) -> None:
    target = Path(_resolve_pi_dashboard_dir()) / _CONFIG_DASHBOARD_FILENAME
    if local_path.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            target.write_text(local_path.read_text(encoding="utf-8"), encoding="utf-8")

def _sync_config_dashboard_from_pi(local_path: Path) -> None:
    source = Path(_resolve_pi_dashboard_dir()) / _CONFIG_DASHBOARD_FILENAME
    if not source.exists():
        return
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _ha_config_root() -> Path:
    return Path(os.getenv("PI_HA_CONFIG_DIR", "/homeassistant")).resolve()


def _ensure_bootstrap_config_dashboard() -> bool:
    """Create a local installer dashboard only when Cloud has not generated one yet."""
    target = _ha_config_root() / "dashboards" / _CONFIG_DASHBOARD_FILENAME
    created = not target.exists()
    if not _BOOTSTRAP_CONFIG_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Bootstrap dashboard template not found: {_BOOTSTRAP_CONFIG_TEMPLATE_PATH}")
    content = _BOOTSTRAP_CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")
    content = content.replace("__HAUSIE_SETUP_URL__", _resolve_setup_ingress_path())
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except Exception:
            return False
        # Only refresh our temporary installer dashboard; never overwrite Cloud output.
        if "Complete Hausie setup" not in existing or existing == content:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    get_logger("config").ok("Created local Hausie setup dashboard." if created else "Refreshed local Hausie setup dashboard.")
    return True


def _collect_registry_automation_ids(root: Path) -> set[str]:
    registry_path = root / "data" / "registry.json"
    if not registry_path.exists():
        return set()
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    ids: set[str] = set()
    for area in data.get("areas", []) or []:
        automations = area.get("automations") or []
        for entry in automations:
            if isinstance(entry, str):
                automation_id = entry
            elif isinstance(entry, dict):
                automation_id = entry.get("id") or entry.get("automation_id")
            else:
                continue
            if automation_id:
                ids.add(str(automation_id))
    return ids


def _remove_file(path: Path, removed: list[str]) -> None:
    try:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    except Exception:
        pass


def _filter_config_views(path: Path, keep_predicate) -> bool:
    if not path.exists():
        return False
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    views = doc.get("views")
    if not isinstance(views, list):
        return False
    new_views = [view for view in views if isinstance(view, dict) and keep_predicate(view)]
    if new_views == views:
        return False
    doc["views"] = new_views
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return True


def _cleanup_base_assets() -> dict[str, object]:
    removed: list[str] = []
    updated: list[str] = []
    root = _ha_config_root()

    for input_type, filename in _BASE_HELPER_FILES:
        if (input_type, filename) in _BASE_HELPER_KEEP_FILES:
            continue
        _remove_file(root / "helpers" / input_type / filename, removed)

    for automation_id in _BASE_AUTOMATION_IDS:
        _remove_file(root / "automations" / f"hausie_automation_{automation_id}.yaml", removed)
        _remove_file(root / "automations" / f"automation_{automation_id}.yaml", removed)

    scripts_dir = root / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.glob("*.yaml"):
            if path.name in _BASE_SCRIPT_KEEP_FILES:
                continue
            _remove_file(path, removed)
    _remove_file(root / "switches" / "hausie_switch_general.yaml", removed)
    _remove_file(root / "switches" / "switch_general.yaml", removed)

    config_path = os.getenv("PI_CONFIG_PATH", "/homeassistant/configuration.yaml").strip()
    if config_path:
        try:
            manager = ConfigManager(
                pi_sender=None,
                config_path=config_path,
                require_remote=False,
            )
            if manager.remove_hausie_entries(
                remove_dashboards=False,
                remove_rest_commands=True,
                remove_includes=False,
                remove_shell_commands=False,
                keep_test_assets=True,
            ):
                updated.append(config_path)
        except Exception:
            pass

    return {"removed": removed, "updated": updated}


def _cleanup_hausie_assets(*, destructive_reset: bool = False) -> dict[str, object]:
    removed: list[str] = []
    updated: list[str] = []
    log = get_logger("addon")
    root = _ha_config_root()

    automations_dir = root / "automations"
    if automations_dir.exists():
        for path in automations_dir.glob("hausie_automation_*.yaml"):
            automation_id = path.stem.replace("hausie_automation_", "", 1)
            if automation_id in _KEEP_AUTOMATION_IDS:
                continue
            _remove_file(path, removed)
        legacy_ids = _collect_registry_automation_ids(root)
        for automation_id in sorted(legacy_ids):
            if automation_id in _KEEP_AUTOMATION_IDS:
                continue
            _remove_file(automations_dir / f"automation_{automation_id}.yaml", removed)

    groups_dir = root / "groups"
    if groups_dir.exists():
        for path in groups_dir.glob("*.yaml"):
            _remove_file(path, removed)

    covers_dir = root / "covers"
    if covers_dir.exists():
        for path in covers_dir.glob("*.yaml"):
            _remove_file(path, removed)

    scripts_dir = root / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.glob("*.yaml"):
            if path.name == "hausie_general_scripts.yaml":
                continue
            _remove_file(path, removed)

    switches_dir = root / "switches"
    if switches_dir.exists():
        for path in switches_dir.glob("*.yaml"):
            if path.name == "hausie_switch_general.yaml":
                continue
            _remove_file(path, removed)

    _remove_file(root / "dashboards" / _MAIN_DASHBOARD_FILENAME, removed)

    ui_cleared = False
    if destructive_reset:
        config_dash = root / "dashboards" / _CONFIG_DASHBOARD_FILENAME
        if _filter_config_views(
            config_dash,
            lambda view: (view.get("path") == "main") or (view.get("title") == "Main"),
        ):
            updated.append(str(config_dash))

        config_path = os.getenv("PI_CONFIG_PATH", "/homeassistant/configuration.yaml").strip()
        if config_path:
            try:
                manager = ConfigManager(
                    pi_sender=None,
                    config_path=config_path,
                    require_remote=False,
                )
                if manager.remove_hausie_entries(keep_test_assets=True):
                    updated.append(config_path)
            except Exception:
                pass

        try:
            settings = Settings()
            if settings.HA_UI_USERNAME and settings.HA_UI_PASSWORD:
                dashboard_path = os.getenv("HA_HAUSIE_DASHBOARD_PATH", "dashboard-hausie/0").strip()
                base_url = settings.HA_REST_URL.rsplit("/api", 1)[0]
                headless_flag = os.getenv("HA_PLAYWRIGHT_HEADLESS", "").strip().lower()
                headless = headless_flag not in {"0", "false", "no"}
                autom = DashboardUpdater(
                    base_url=base_url,
                    username=settings.HA_UI_USERNAME,
                    password=settings.HA_UI_PASSWORD,
                    headless=headless,
                    storage_state_path=None,
                )
                try:
                    log.start("Replacing Hausie dashboard via UI.")
                    placeholder_yaml = (
                        "views:\n"
                        "  - type: sections\n"
                        "    sections:\n"
                        "      - type: grid\n"
                        "        cards: []\n"
                        "    header:\n"
                        "      card:\n"
                        "        type: markdown\n"
                        "        content: '### Select a plan in your Hausie account, to get this dashboard'\n"
                        "        title: 'OOOOPS! Update your plan'\n"
                    )
                    autom.write_yaml_to_ui(dashboard_path, placeholder_yaml)
                    ui_cleared = True
                    log.ok("Hausie dashboard replaced via UI.")
                except Exception as exc:
                    log.warn(f"Dashboard UI cleanup failed: {exc}")
                finally:
                    try:
                        autom.close()
                    except Exception:
                        pass
            else:
                log.warn("Dashboard UI cleanup skipped: HA_UI_USERNAME/HA_UI_PASSWORD not set.")
        except Exception as exc:
            log.warn(f"Dashboard UI cleanup skipped: {exc}")

    return {"removed": removed, "updated": updated, "ui_cleared": ui_cleared}


def _get_state_value(states: list[dict], entity_id: str) -> str:
    for state in states:
        if isinstance(state, dict) and state.get("entity_id") == entity_id:
            return state.get("state") or ""
    return ""


def _normalize_select_value(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower() in {"none", "unknown", "unavailable"}:
        return ""
    return cleaned


def _read_new_device_inputs(ha: HAClient) -> dict[str, str]:
    states = ha.get_states()
    return {
        "device_id": _normalize_select_value(_get_state_value(states, "input_text.new_device_device_id")),
        "name": _normalize_select_value(_get_state_value(states, "input_text.new_device_name")),
        "label": _normalize_select_value(_get_state_value(states, "input_select.new_device_label")),
        "area": _normalize_select_value(_get_state_value(states, "input_select.new_device_area")),
    }


class _WSClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.ws = websocket.create_connection(self.url, timeout=10)
        self._next_id = 1
        self._auth()

    def _auth(self) -> None:
        msg = json.loads(self.ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake: {msg}")
        self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_ok = json.loads(self.ws.recv())
        if auth_ok.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, payload: dict) -> dict:
        req_id = self._next_id
        self._next_id += 1
        message = dict(payload)
        message["id"] = req_id
        self.ws.send(json.dumps(message))
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if not msg.get("success", True):
                    raise RuntimeError(f"WS call failed: {msg}")
                return msg.get("result")


def _resolve_device_id(ws: _WSClient, identifier: str) -> str | None:
    if not identifier:
        return None
    devices = ws.call({"type": "config/device_registry/list"}) or []
    for device in devices:
        if device.get("id") == identifier:
            return identifier
    for device in devices:
        identifiers = device.get("identifiers") or []
        for ident in identifiers:
            if isinstance(ident, (list, tuple)) and identifier in ident:
                return device.get("id")
            if isinstance(ident, str) and ident == identifier:
                return device.get("id")
    name_matches = [
        device
        for device in devices
        if identifier
        and (device.get("name_by_user") == identifier or device.get("name") == identifier)
    ]
    if len(name_matches) == 1:
        return name_matches[0].get("id")

    entities = ws.call({"type": "config/entity_registry/list"}) or []
    for entity in entities:
        if entity.get("entity_id") == identifier:
            return entity.get("device_id")
    for entity in entities:
        if entity.get("unique_id") == identifier:
            return entity.get("device_id")
    partial_matches = [
        entity
        for entity in entities
        if isinstance(entity.get("entity_id"), str) and identifier in entity["entity_id"]
    ]
    if len(partial_matches) == 1:
        return partial_matches[0].get("device_id")
    return None


def _update_device_name_area(
    *,
    ha_ws_url: str,
    token: str,
    device_id: str,
    name: str,
    area_name: str,
) -> str:
    ws = _WSClient(ha_ws_url, token)
    try:
        resolved_id = _resolve_device_id(ws, device_id)
        if not resolved_id:
            raise ValueError(f"device_id not found: {device_id}")
        area_id = None
        if area_name:
            areas = ws.call({"type": "config/area_registry/list"}) or []
            match = next((a for a in areas if a.get("name") == area_name), None)
            if match:
                area_id = match.get("area_id")
            else:
                created = ws.call({"type": "config/area_registry/create", "name": area_name})
                if isinstance(created, dict):
                    area_id = created.get("area_id")

        payload = {"type": "config/device_registry/update", "device_id": resolved_id}
        if name:
            payload["name_by_user"] = name
        if area_id:
            payload["area_id"] = area_id
        if len(payload) > 2:
            ws.call(payload)
        return resolved_id
    finally:
        ws.close()


def _update_device_labels(
    *,
    base_url: str,
    username: str,
    password: str,
    device_id: str,
    labels: list[str],
) -> list[str]:
    applied: list[str] = []
    cleaned = []
    for label in labels or []:
        normalized = str(label or "").strip()
        if not normalized or normalized.lower() == "none" or normalized in cleaned:
            continue
        cleaned.append(normalized)
    if not cleaned:
        return applied
    headless_flag = os.getenv("HA_PLAYWRIGHT_HEADLESS", "").strip().lower()
    headless = headless_flag not in {"0", "false", "no"}
    updater = DeviceLabelUpdater(
        base_url=base_url,
        username=username,
        password=password,
        headless=headless,
    )
    try:
        for label in cleaned:
            if updater.update_device_label(device_id, label):
                applied.append(label)
    finally:
        updater.close()
    return applied


def _reload_services(ha: HAClient, log) -> None:
    services = [
        ("homeassistant", "reload_core_config"),
        ("input_text", "reload"),
        ("input_button", "reload"),
        ("input_boolean", "reload"),
        ("input_number", "reload"),
        ("input_select", "reload"),
        ("input_datetime", "reload"),
        ("automation", "reload"),
        ("script", "reload"),
        ("group", "reload"),
        ("template", "reload"),
        ("lovelace", "reload"),
        ("browser_mod", "refresh"),
    ]
    available: set[tuple[str, str]] | None = None
    try:
        service_docs = ha.get_services()
        parsed: set[tuple[str, str]] = set()
        for entry in service_docs:
            if not isinstance(entry, dict):
                continue
            domain = str(entry.get("domain") or "").strip()
            if not domain:
                continue
            services_block = entry.get("services")
            if isinstance(services_block, dict):
                for service_name in services_block.keys():
                    name = str(service_name or "").strip()
                    if name:
                        parsed.add((domain, name))
                continue
            if isinstance(services_block, list):
                for service in services_block:
                    if not isinstance(service, dict):
                        continue
                    name = str(service.get("service") or "").strip()
                    if name:
                        parsed.add((domain, name))
        available = parsed or None
    except Exception as exc:
        log.warn(f"Failed to list Home Assistant services before reload: {exc}")
    reloaded = 0
    skipped = 0
    for domain, service in services:
        if available is not None and (domain, service) not in available:
            skipped += 1
            continue
        try:
            ha.call_service(domain, service, {})
            reloaded += 1
        except Exception as exc:
            log.warn(f"Reload {domain}.{service} failed: {exc}")
    if reloaded or skipped:
        summary = f"Reloaded {reloaded} Home Assistant services."
        if skipped:
            summary += f" Skipped {skipped} unavailable optional services."
        log.info(summary)


def _reload_browser_frontends(ha: HAClient, log) -> None:
    try:
        ha.call_service("browser_mod", "window_reload", {})
        log.info("Requested browser refresh via Browser Mod.")
    except Exception as exc:
        log.warn(f"Browser refresh skipped: {exc}")


def _restart_home_assistant(ha: HAClient, log) -> None:
    try:
        log.start("Requesting Home Assistant restart.")
        ha.call_service("homeassistant", "restart", {})
        log.ok("Home Assistant restart requested.")
    except Exception as exc:
        if _ha_restart_exception_is_expected(exc):
            log.info(f"Home Assistant restart is in progress: {exc}")
            return
        log.warn(f"Home Assistant restart skipped: {exc}")


def _wait_for_home_assistant_ready(ha: HAClient, log, *, timeout_s: int = 180, interval_s: int = 5) -> bool:
    """Wait until Home Assistant responds again after a restart request."""
    deadline = time.time() + max(5, timeout_s)
    while time.time() < deadline:
        try:
            ha.get_states()
            log.ok("Home Assistant is available again.")
            return True
        except Exception as exc:
            log.info(f"Waiting for Home Assistant to come back: {exc}")
            time.sleep(max(1, interval_s))
    log.warn("Timed out waiting for Home Assistant to become available after rebuild.")
    return False


def _wait_for_helper_entities_ready(
    ha: HAClient,
    snapshot: dict[str, dict[str, Any]],
    log,
    *,
    timeout_s: int = 120,
    interval_s: int = 5,
) -> bool:
    """Wait until the helper entities captured before rebuild exist again after restart."""
    target_entities = sorted(
        entity_id
        for entity_id, item in (snapshot or {}).items()
        if isinstance(item, dict) and str(item.get("domain") or item.get("helper_type") or "").strip()
    )
    if not target_entities:
        return True
    remaining = set(target_entities)
    deadline = time.time() + max(5, timeout_s)
    while time.time() < deadline:
        try:
            states = ha.get_states()
            live_entities = {
                str(state.get("entity_id") or "").strip()
                for state in (states or [])
                if isinstance(state, dict) and str(state.get("entity_id") or "").strip()
            }
            remaining = {entity_id for entity_id in target_entities if entity_id not in live_entities}
            if not remaining:
                log.ok(f"All helper entities are available again ({len(target_entities)}).")
                return True
        except Exception as exc:
            log.info(f"Waiting for helper entities after restart: {exc}")
        if remaining:
            preview = ", ".join(sorted(list(remaining))[:5])
            suffix = " ..." if len(remaining) > 5 else ""
            log.info(f"Waiting for helper entities to load: {preview}{suffix}")
        time.sleep(max(1, interval_s))
    if remaining:
        preview = ", ".join(sorted(list(remaining))[:10])
        suffix = " ..." if len(remaining) > 10 else ""
        log.warn(f"Timed out waiting for helper entities after rebuild: {preview}{suffix}")
    return False


def _apply_cloud_artifacts(
    *,
    remote_root: str,
    artifacts: list[dict] | None,
    deletes: list[str] | None,
    log,
) -> dict[str, str]:
    applied: dict[str, str] = {}
    if not artifacts and not deletes:
        log.skip("No cloud artifacts to apply.")
        return applied
    updated_count = 0
    deleted_count = 0
    mirrored_count = 0
    removed_local_count = 0
    local_root = _resolve_local_ha_root()
    root = (remote_root or "").rstrip("/")
    for item in artifacts or []:
        if not isinstance(item, dict):
            continue
        rel_path = (item.get("path") or "").lstrip("/")
        content = item.get("content")
        if not rel_path or content is None:
            continue
        remote_path = rel_path if rel_path.startswith("/") else f"{root}/{rel_path}" if root else rel_path
        path = Path(remote_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        applied[remote_path] = str(content)
        updated_count += 1
        if _mirror_local_artifact(local_root, rel_path, str(content), log):
            mirrored_count += 1

    for rel_path in deletes or []:
        if not rel_path:
            continue
        remote_path = rel_path if rel_path.startswith("/") else f"{root}/{rel_path}" if root else rel_path
        try:
            path = Path(remote_path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            deleted_count += 1
        except Exception as exc:
            log.warn(f"Failed to delete {remote_path}: {exc}")
        if _remove_local_artifact(local_root, rel_path, log):
            removed_local_count += 1
    if updated_count or deleted_count or mirrored_count or removed_local_count:
        parts = [f"Applied cloud artifacts: {updated_count} updated"]
        if deleted_count:
            parts.append(f"{deleted_count} deleted")
        if mirrored_count:
            parts.append(f"{mirrored_count} mirrored locally")
        if removed_local_count:
            parts.append(f"{removed_local_count} local mirrors removed")
        log.info(", ".join(parts) + ".")
    return applied


_VOICE_ASSISTANTS = ("cloud.alexa", "cloud.google_assistant")
_VOICE_STATE_KEY = "managed_voice_entities"


def _normalize_voice_entities(value: Any) -> list[str]:
    """Normalize voice entity ids received from cloud."""
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        entity_id = str(item or "").strip()
        if not entity_id or "." not in entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append(entity_id)
    return normalized


def _sync_voice_exposure(ha: HAClient, response: dict[str, Any] | None, log) -> None:
    """Sync Alexa/Google Assistant exposure for the entities shown in Hausie."""
    voice = response.get("voice") if isinstance(response, dict) else None
    if not isinstance(voice, dict):
        log.info("Voice exposure sync skipped: cloud response missing voice payload.")
        return

    assistants = [
        str(item or "").strip()
        for item in (voice.get("assistants") or list(_VOICE_ASSISTANTS))
        if str(item or "").strip()
    ]
    desired_entities = _normalize_voice_entities(voice.get("entities") or [])
    voice_enabled = bool(voice.get("enabled", True))
    if not assistants:
        log.info("Voice exposure sync skipped: no assistants configured in payload.")
        return

    try:
        config = ha.get_config()
    except Exception as exc:
        log.warn(f"Voice exposure sync skipped: failed to read HA config ({exc}).")
        return

    components = {
        str(item or "").strip().lower()
        for item in (config.get("components") or [])
        if str(item or "").strip()
    }
    if "cloud" not in components:
        log.info("Voice exposure sync skipped: Home Assistant Cloud is not loaded.")
        return

    available_entities: set[str] = set()
    try:
        available_entities = {
            str(item.get("entity_id") or "").strip()
            for item in (ha.get_states() or [])
            if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
        }
    except Exception as exc:
        log.warn(f"Voice exposure sync continuing without state filtering: {exc}")

    missing_entities = [
        entity_id for entity_id in desired_entities
        if available_entities and entity_id not in available_entities
    ]
    desired_entities = [
        entity_id for entity_id in desired_entities
        if not available_entities or entity_id in available_entities
    ]
    if missing_entities:
        log.warn(
            "Voice exposure skipped missing entities: "
            + ", ".join(sorted(missing_entities)[:10])
            + (" ..." if len(missing_entities) > 10 else "")
        )

    try:
        ha.list_exposed_entities()
    except Exception as exc:
        log.warn(f"Voice exposure sync skipped: expose_entity API unavailable ({exc}).")
        return

    state = load_device_state()
    managed = state.get(_VOICE_STATE_KEY)
    if not isinstance(managed, dict):
        managed = {}

    updated_managed: dict[str, list[str]] = {}
    total_exposed = 0
    total_unexposed = 0
    for assistant in assistants:
        previous_entities = {
            str(item or "").strip()
            for item in (managed.get(assistant) or [])
            if str(item or "").strip()
        }
        desired_set = set(desired_entities) if voice_enabled else set()
        to_unexpose = sorted(previous_entities - desired_set)
        if to_unexpose:
            try:
                ha.set_entity_exposure(
                    assistants=[assistant],
                    entity_ids=to_unexpose,
                    should_expose=False,
                )
                total_unexposed += len(to_unexpose)
            except Exception as exc:
                log.warn(f"Voice exposure unexpose failed for {assistant}: {exc}")

        if desired_entities and voice_enabled:
            try:
                ha.set_entity_exposure(
                    assistants=[assistant],
                    entity_ids=desired_entities,
                    should_expose=True,
                )
                total_exposed += len(desired_entities)
            except Exception as exc:
                log.warn(f"Voice exposure update failed for {assistant}: {exc}")
                updated_managed[assistant] = sorted(previous_entities & desired_set)
                continue

        updated_managed[assistant] = sorted(desired_set)

    state[_VOICE_STATE_KEY] = updated_managed
    state["voice_sync_assistants"] = assistants
    state["voice_sync_desired_entities"] = desired_entities if voice_enabled else []
    state["voice_sync_enabled"] = voice_enabled
    state["voice_entities_updated_at"] = int(time.time())
    save_device_state(state)
    if voice_enabled:
        log.ok(
            f"Voice exposure synced: {len(desired_entities)} desired, "
            f"{total_exposed} expose ops, {total_unexposed} unexpose ops."
        )
    else:
        log.ok(f"Voice exposure disabled by plan: {total_unexposed} unexpose ops.")


def _resync_voice_exposure_from_state(log) -> None:
    """Re-apply the last known voice exposure state on add-on startup."""
    state = load_device_state()
    desired_entities = _normalize_voice_entities(state.get("voice_sync_desired_entities") or [])
    assistants = [
        str(item or "").strip()
        for item in (state.get("voice_sync_assistants") or list(_VOICE_ASSISTANTS))
        if str(item or "").strip()
    ]
    if not desired_entities or not assistants:
        return
    ha = _resolve_ha_client()
    if not ha:
        log.warn("Voice exposure startup sync skipped: HA client unavailable.")
        return
    _sync_voice_exposure(
        ha,
        {"voice": {"assistants": assistants, "entities": desired_entities}},
        log,
    )


def _normalize_label_catalog_entries(labels: Any) -> list[dict[str, Any]]:
    if not isinstance(labels, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in labels:
        if not isinstance(entry, dict):
            continue
        label_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or label_id or "").strip()
        if not label_id or not name or label_id in seen_ids:
            continue
        seen_ids.add(label_id)
        normalized.append(
            {
                "id": label_id,
                "name": name,
                "icon": str(entry.get("icon") or "").strip() or None,
                "color": str(entry.get("color") or "").strip() or None,
                "description": str(entry.get("description") or "").strip(),
            }
        )
    return normalized


def _sync_label_catalog_from_cloud(
    ha: HAClient,
    settings: Settings,
    log,
    *,
    force: bool = False,
    expected_version: int | None = None,
) -> bool:
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        return False
    cloud = CloudClient(
        base_url=settings.HAUSIE_CLOUD_URL,
        token=settings.HAUSIE_CLOUD_TOKEN,
        timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
    )
    catalog = cloud.request_label_catalog()
    version = catalog.get("version")
    try:
        version_int = int(version) if version is not None else None
    except Exception:
        version_int = None
    if expected_version is not None:
        version_int = expected_version
    state = load_device_state()
    applied_version = state.get("applied_label_catalog_version")
    try:
        applied_version_int = int(applied_version) if applied_version is not None else None
    except Exception:
        applied_version_int = None
    if not force and version_int is not None and applied_version_int == version_int:
        return False

    desired_labels = _normalize_label_catalog_entries(catalog.get("labels"))
    if not desired_labels:
        return False

    current_labels = ha.fetch_labels()
    current_by_id = {
        str(label.get("id") or "").strip(): label
        for label in current_labels
        if isinstance(label, dict) and str(label.get("id") or "").strip()
    }
    current_by_name = {
        str(label.get("name") or "").strip(): label
        for label in current_labels
        if isinstance(label, dict) and str(label.get("name") or "").strip()
    }

    created = 0
    updated = 0
    for label in desired_labels:
        target = current_by_id.get(label["id"])
        if target is None:
            target = current_by_name.get(label["name"])
        if target is None:
            ha.create_label(name=label["name"], icon=label.get("icon"), color=label.get("color"))
            created += 1
            continue
        current_name = str(target.get("name") or "").strip()
        current_icon = str(target.get("icon") or "").strip() or None
        current_color = str(target.get("color") or "").strip() or None
        if (
            current_name != label["name"]
            or current_icon != label.get("icon")
            or current_color != label.get("color")
        ):
            resolved_label_id = str(target.get("id") or "").strip() or label["id"]
            ha.update_label(
                label_id=resolved_label_id,
                name=label["name"],
                icon=label.get("icon"),
                color=label.get("color"),
            )
            updated += 1

    if version_int is not None:
        state["applied_label_catalog_version"] = version_int
    state["last_label_catalog_sync_at"] = int(time.time())
    save_device_state(state)
    log.ok(
        f"Label catalog synced: {len(desired_labels)} desired, {created} created, {updated} updated."
    )
    return created > 0 or updated > 0


def _run_sync_inventory(
    *,
    force_full: bool = False,
    plan_override: str | None = None,
    manage_activity: bool = True,
) -> None:
    log = get_logger("addon")
    with _workflow_activity("Syncing inventory", manage_lock=manage_activity):
        with log.script("sync_inventory"):
            settings = Settings()
            if not settings.HAUSIE_CLOUD_URL:
                raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar assets en cloud.")
            ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
            try:
                _sync_label_catalog_from_cloud(ha, settings, log, force=True)
            except Exception as exc:
                log.warn(f"Label catalog sync skipped before inventory refresh: {exc}")
            ha.fetch_all(include_users=True)
            raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
            labels = ha.fetch_labels()
            device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
            addon_slug = _resolve_self_addon_slug()
            current_license = _sync_license_state_from_cloud(settings, log, force=True)
            current_plan = _normalize_plan_id(current_license.get("plan"), "") or _resolve_subscription_plan(settings) or ""
            payload = {
                "areas": raw.get("areas", []),
                "devices": raw.get("devices", []),
                "entities": raw.get("entities", []),
                "services": raw.get("services", []),
                "users": raw.get("users", []),
                "labels": labels,
                "current_plan": current_plan,
                "license_status": str(current_license.get("license_status") or "").strip(),
            }
            if addon_slug:
                payload["addon_slug"] = addon_slug
            if force_full:
                payload["force_full"] = True
            if plan_override:
                payload["plan_override"] = _normalize_plan_id(plan_override)
            if device_id:
                payload["device_id"] = device_id
            cloud = CloudClient(
                base_url=settings.HAUSIE_CLOUD_URL,
                token=settings.HAUSIE_CLOUD_TOKEN,
                timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
                create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
            )
            response = cloud.request_sync_inventory(payload)
            if _normalize_plan_id(plan_override, "") == "plan_1":
                _save_free_plan_bundle("create", response if isinstance(response, dict) else {}, log)
            else:
                _refresh_free_plan_cache("create", cloud, payload, log)
            applied = _apply_cloud_artifacts(
                remote_root=settings.PI_HA_CONFIG_DIR,
                artifacts=response.get("artifacts") if isinstance(response, dict) else None,
                deletes=response.get("deletes") if isinstance(response, dict) else None,
                log=log,
            )
            ui_payload = response.get("ui") if isinstance(response, dict) else None
            if isinstance(ui_payload, dict):
                dashboard_yaml = ui_payload.get("main_dashboard_yaml")
                dashboard_path = ui_payload.get("dashboard_path") or "dashboard-hausie/0"
                if not dashboard_yaml:
                    dash_file = ui_payload.get("main_dashboard_file")
                    if dash_file:
                        resolved = dash_file if dash_file.startswith("/") else f"{settings.PI_HA_CONFIG_DIR.rstrip('/')}/{dash_file}"
                        dashboard_yaml = applied.get(resolved)
                        if not dashboard_yaml:
                            log.warn(f"UI update skipped: dashboard YAML not found in applied artifacts ({resolved}).")
                if not settings.HA_UI_USERNAME or not settings.HA_UI_PASSWORD:
                    log.warn("UI update skipped: HA_UI_USERNAME/HA_UI_PASSWORD not set.")
                elif not dashboard_yaml:
                    log.warn("UI update skipped: dashboard YAML content missing.")
                else:
                    log.start("Updating dashboard via UI.")
                    autom = DashboardUpdater(
                        base_url=settings.HA_REST_URL.rsplit("/api", 1)[0],
                        username=settings.HA_UI_USERNAME,
                        password=settings.HA_UI_PASSWORD,
                        headless=True,
                        storage_state_path=None,
                    )
                    try:
                        autom.write_yaml_to_ui(dashboard_path, dashboard_yaml)
                        log.ok("Dashboard UI updated.")
                    except Exception as exc:
                        log.warn(f"UI update failed: {exc}")
                    finally:
                        try:
                            autom.close()
                        except Exception:
                            pass
            else:
                log.warn("UI update skipped: cloud response missing 'ui' payload.")
            _reload_services(ha, log)
            _reload_browser_frontends(ha, log)
            _patch_credentials_shortcut(log)
            _patch_add_device_shortcut(log)
            _patch_hausie_app_shortcut(log)
            _sync_voice_exposure(ha, response if isinstance(response, dict) else None, log)
            _apply_plan_badge(ha, response.get("plan_badge") if isinstance(response, dict) else None)
            _refresh_license_state_from_cloud(settings, log)
            state = load_device_state()
            state["last_inventory_signature"] = _build_inventory_signature(raw, labels)
            state["last_inventory_synced_at"] = int(time.time())
            state.pop("inventory_change_pending", None)
            save_device_state(state)
            enabled = _turn_on_user_helpers(ha)
            if enabled:
                log.ok(f"User helpers enabled: {enabled}.")


def _run_create_hausie(*, force_full: bool = False, plan_override: str | None = None) -> None:
    _run_sync_inventory(force_full=force_full, plan_override=plan_override, manage_activity=True)


def _run_rebuild_hausie() -> None:
    log = get_logger("addon")
    with _workflow_activity("Rebuilding Hausie", manage_lock=True):
        with log.script("rebuild_hausie"):
            settings = Settings()
            ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
            helper_snapshot = _snapshot_rebuild_helper_values(ha, _ha_config_root(), log)
            state = load_device_state()
            last_plan = str(state.get("last_plan") or "").strip().lower()
            last_version = str(state.get("last_addon_version") or "").strip()
            current_license = _sync_license_state_from_cloud(settings, log, force=True)
            current_plan = str(current_license.get("plan") or "").strip() or _resolve_subscription_plan(settings)
            current_version = _resolve_addon_version()
            plan = _resolve_remote_rebuild_plan(
                settings,
                state=state,
                current_plan=current_plan,
                current_version=current_version,
            )
            if plan is None:
                plan = _resolve_local_rebuild_plan(
                    current_plan=current_plan,
                    current_version=current_version,
                    last_plan=last_plan,
                    last_version=last_version,
                )
            steps = _normalize_rebuild_steps(plan.get("execution_plan"))
            source = str(plan.get("source") or "unknown")
            reason = str(plan.get("reason") or "unknown")
            log.start(f"Using {source} rebuild plan ({reason}): {', '.join(steps)}.")
            _execute_rebuild_steps(steps, log)
            final_plan = str(plan.get("plan") or current_plan or "").strip() or None
            _update_rebuild_state(state, plan=final_plan, version=current_version)
            _restart_home_assistant(ha, log)
            if helper_snapshot:
                _wait_for_home_assistant_ready(ha, log)
                _wait_for_helper_entities_ready(ha, helper_snapshot, log)
                _restore_rebuild_helper_values(ha, helper_snapshot, log)


def _run_create_base(
    *,
    force_full: bool = False,
    plan_override: str | None = None,
    manage_activity: bool = True,
) -> None:
    log = get_logger("addon")
    with _workflow_activity("Applying base configuration", manage_lock=manage_activity):
        with log.script("create_base"):
            settings = Settings()
            if not settings.HAUSIE_CLOUD_URL:
                raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar assets en cloud.")
            ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
            try:
                _sync_label_catalog_from_cloud(ha, settings, log, force=True)
            except Exception as exc:
                log.warn(f"Label catalog sync skipped before base rebuild: {exc}")
            log.start("Fetching Home Assistant snapshot.")
            ha.fetch_all(include_users=True)
            raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
            labels = ha.fetch_labels()
            computed_force_full = False
            try:
                states = ha.get_states()
                computed_force_full = not any(
                    isinstance(state, dict) and state.get("entity_id") == "input_text.hausie_plan_text"
                    for state in states or []
                )
            except Exception:
                computed_force_full = False
            device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
            addon_slug = _resolve_self_addon_slug()
            current_license = _sync_license_state_from_cloud(settings, log, force=True)
            current_plan = _normalize_plan_id(current_license.get("plan"), "") or _resolve_subscription_plan(settings) or ""
            payload = {
                "areas": raw.get("areas", []),
                "devices": raw.get("devices", []),
                "entities": raw.get("entities", []),
                "services": raw.get("services", []),
                "users": raw.get("users", []),
                "labels": labels,
                "force_full": bool(force_full or computed_force_full),
                "current_plan": current_plan,
                "license_status": str(current_license.get("license_status") or "").strip(),
            }
            if addon_slug:
                payload["addon_slug"] = addon_slug
            if plan_override:
                payload["plan_override"] = _normalize_plan_id(plan_override)
            if device_id:
                payload["device_id"] = device_id
            log.start("Requesting base assets from cloud.")
            cloud = CloudClient(
                base_url=settings.HAUSIE_CLOUD_URL,
                token=settings.HAUSIE_CLOUD_TOKEN,
                timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
                create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
            )
            response = cloud.request_base_assets(payload)
            if _normalize_plan_id(plan_override, "") == "plan_1":
                _save_free_plan_bundle("base", response if isinstance(response, dict) else {}, log)
            else:
                _refresh_free_plan_cache("base", cloud, payload, log)
            log.start("Applying cloud artifacts to Home Assistant config.")
            _apply_cloud_artifacts(
                remote_root=settings.PI_HA_CONFIG_DIR,
                artifacts=response.get("artifacts") if isinstance(response, dict) else None,
                deletes=response.get("deletes") if isinstance(response, dict) else None,
                log=log,
            )
            root = _ha_config_root()
            _ensure_plan_text_helper(root, response.get("plan_badge") if isinstance(response, dict) else None)
            _ensure_remote_support_helper(root)
            if settings.PI_CONFIG_PATH:
                log.start("Updating configuration.yaml.")
                config = ConfigManager(
                    pi_sender=None,
                    config_path=settings.PI_CONFIG_PATH,
                    require_remote=False,
                )
                config.sync_config_dashboard()
            log.start("Reloading Home Assistant services.")
            _reload_services(ha, log)
            _patch_add_device_shortcut(log)
            _patch_hausie_app_shortcut(log)
            _apply_plan_badge(ha, response.get("plan_badge") if isinstance(response, dict) else None)
            _refresh_license_state_from_cloud(settings, log)
            enabled = _turn_on_user_helpers(ha)
            if enabled:
                log.ok(f"User helpers enabled: {enabled}.")


def _run_initialize_hausie() -> None:
    """Generate the first Hausie configuration after local credentials and Cloud pairing."""
    log = get_logger("setup")
    with _workflow_activity("Initializing Hausie", manage_lock=True):
        with log.script("initialize_hausie"):
            device_id, device_token = resolve_device_credentials()
            if not device_id or not device_token:
                raise RuntimeError("Pair this Home Assistant installation with Hausie before initializing.")
            _set_setup_progress("initializing", "Creating Hausie configuration...")
            try:
                _run_create_base(force_full=True, manage_activity=False)
                _set_setup_progress("initializing", "Creating dashboards and automations...")
                _run_sync_inventory(force_full=True, manage_activity=False)
            except Exception as exc:
                _set_setup_progress("failed", str(exc))
                raise
            _set_setup_progress("complete", "Hausie initialization completed.", initialized=True)
            log.ok("Hausie initialization completed.")


def _run_restart_hausie() -> None:
    log = get_logger("addon")
    with _workflow_activity("Restarting Hausie", manage_lock=True):
        with log.script("restart_hausie"):
            _cleanup_base_assets()
            _cleanup_hausie_assets(destructive_reset=True)
            _run_create_base(force_full=True, manage_activity=False)
            _run_sync_inventory(force_full=True, manage_activity=False)
            settings = Settings()
            state = load_device_state()
            _update_rebuild_state(
                state,
                plan=_resolve_subscription_plan(settings),
                version=_resolve_addon_version(),
            )


def _license_time_expired(license_state: dict[str, Any]) -> bool:
    offline_valid_until = str(license_state.get("offline_valid_until") or "").strip()
    if not offline_valid_until:
        return False
    try:
        expiry = datetime.fromisoformat(offline_valid_until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return now > expiry


def _license_clock_invalid(license_state: dict[str, Any]) -> bool:
    last_sync = str(license_state.get("last_license_sync_at") or "").strip()
    if not last_sync:
        return False
    try:
        sync_dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
    except ValueError:
        return True
    if sync_dt.tzinfo is None:
        sync_dt = sync_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < sync_dt


def _apply_cached_plan_cache(kind: str, log) -> None:
    payload = load_free_plan_cache(kind)
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    deletes = payload.get("deletes") if isinstance(payload.get("deletes"), list) else []
    if not artifacts and not deletes:
        raise RuntimeError(f"Missing cached free-plan payload for {kind}.")
    settings = Settings()
    _apply_cloud_artifacts(
        remote_root=settings.PI_HA_CONFIG_DIR,
        artifacts=artifacts,
        deletes=deletes,
        log=log,
    )
    ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
    _reload_services(ha, log)
    _apply_plan_badge(ha, payload.get("plan_badge") if isinstance(payload.get("plan_badge"), dict) else None)
    _turn_on_user_helpers(ha)


def _run_apply_plan(
    *,
    target_plan: str,
    license_payload: dict[str, Any] | None = None,
    allow_local_free_fallback: bool = False,
) -> None:
    log = get_logger("addon")
    with _workflow_activity("Applying plan", manage_lock=True):
        with log.script("apply_plan"):
            settings = Settings()
            ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
            current_license = load_license_state()
            source_plan = _normalize_plan_id(current_license.get("plan"))
            target = _normalize_plan_id(target_plan)
            _capture_and_persist_helper_snapshot(ha, source_plan=source_plan, target_plan=target, log=log)
            _cleanup_base_assets()
            _cleanup_hausie_assets()
            if target == "plan_1" and allow_local_free_fallback:
                _apply_cached_plan_cache("base", log)
                _apply_cached_plan_cache("create", log)
            elif target == "plan_1":
                _run_create_base(force_full=True, plan_override="plan_1", manage_activity=False)
                _run_sync_inventory(force_full=True, plan_override="plan_1", manage_activity=False)
            else:
                _run_create_base(force_full=True, plan_override=target, manage_activity=False)
                _run_sync_inventory(force_full=True, plan_override=target, manage_activity=False)
            _restore_persisted_helper_snapshot(ha, log)
            if license_payload:
                final_state = _store_license_payload(license_payload, log)
            else:
                final_state = load_license_state()
                final_state["plan"] = target
                save_license_state(final_state)
            rebuild_state = load_device_state()
            _update_rebuild_state(
                rebuild_state,
                plan=_normalize_plan_id(final_state.get("plan"), target),
                version=_resolve_addon_version(),
            )


def _run_create_test() -> None:
    log = get_logger("addon")
    with log.script("create_test"):
        settings = Settings()
        if not settings.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar test assets en cloud.")
        ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
        log.start("Fetching Home Assistant snapshot.")
        ha.fetch_all(include_users=True)
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()
        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
        current_license = _sync_license_state_from_cloud(settings, log, force=True)
        current_plan = str(current_license.get("plan") or "").strip() or _resolve_subscription_plan(settings) or ""
        payload = {
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
            "current_plan": current_plan,
            "license_status": str(current_license.get("license_status") or "").strip(),
        }
        if device_id:
            payload["device_id"] = device_id
        log.start("Requesting test assets from cloud.")
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
            create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
        )
        response = cloud.request_test_assets(payload)
        log.start("Applying cloud artifacts to Home Assistant config.")
        _apply_cloud_artifacts(
            remote_root=settings.PI_HA_CONFIG_DIR,
            artifacts=response.get("artifacts") if isinstance(response, dict) else None,
            deletes=response.get("deletes") if isinstance(response, dict) else None,
            log=log,
        )
        if settings.PI_CONFIG_PATH:
            log.start("Updating configuration.yaml.")
            config = ConfigManager(
                pi_sender=None,
                config_path=settings.PI_CONFIG_PATH,
                require_remote=False,
            )
            config.sync_config_dashboard()
        _patch_credentials_shortcut(log)
        _patch_hausie_app_shortcut(log)
        log.start("Reloading Home Assistant services.")
        _reload_services(ha, log)
        _apply_plan_badge(ha, response.get("plan_badge") if isinstance(response, dict) else None)
        enabled = _turn_on_user_helpers(ha)
        if enabled:
            log.ok(f"User helpers enabled: {enabled}.")


def _pick_entity_id(entities: list[dict]) -> str | None:
    if not entities:
        return None
    priority = [
        "binary_sensor",
        "sensor",
        "switch",
        "light",
        "cover",
        "climate",
        "lock",
        "button",
        "number",
        "select",
    ]
    best = None
    best_rank = len(priority)
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        entity_id = ent.get("entity_id")
        if not entity_id or "." not in entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain in priority:
            rank = priority.index(domain)
            if rank < best_rank:
                best_rank = rank
                best = entity_id
    if best:
        return best
    for ent in entities:
        if isinstance(ent, dict) and ent.get("entity_id"):
            return ent["entity_id"]
    return None


def _fetch_device_info(device_id: str) -> tuple[str | None, str | None]:
    ha = _resolve_ha_client()
    if not ha:
        return None, None
    try:
        data = ha.fetch_device_and_entities_by_id(device_id)
        device = data.get("device") or {}
        name = device.get("name_by_user") or device.get("name")
        entity_id = _pick_entity_id(data.get("entities") or [])
        return name, entity_id
    except Exception:
        return None, None


def _has_device_labels(device: dict) -> bool:
    labels = device.get("labels")
    if labels is None:
        labels = device.get("label_ids") or device.get("label_id")
    if isinstance(labels, str):
        labels = [labels]
    if isinstance(labels, list):
        return len([lab for lab in labels if lab]) > 0
    return bool(labels)


def _resolve_area_map(raw: dict) -> dict[str, str]:
    areas = raw.get("areas") or []
    area_map: dict[str, str] = {}
    for area in areas:
        if not isinstance(area, dict):
            continue
        area_id = area.get("id")
        name = area.get("name")
        if area_id and name:
            area_map[str(area_id)] = str(name)
    return area_map


def _find_device_entities(raw: dict, device_id: str) -> list[dict]:
    entities = raw.get("entities") or []
    return [e for e in entities if isinstance(e, dict) and e.get("device_id") == device_id]


def _scan_unlabeled_devices() -> tuple[int, int]:
    ha = _resolve_ha_client()
    if not ha:
        raise RuntimeError("HA_TOKEN is required")
    ha.fetch_all(include_users=False)
    raw = ha._load_raw()
    devices = raw.get("devices") or []
    area_map = _resolve_area_map(raw)
    updated = 0
    total = 0
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = device.get("id")
        if not device_id:
            continue
        if _has_device_labels(device):
            continue
        area_name = area_map.get(device.get("area_id") or "")
        if area_name and slugify(area_name) == "system":
            continue
        entities = _find_device_entities(raw, device_id)
        entity_id = _pick_entity_id(entities)
        if not entity_id:
            continue
        name = device.get("name_by_user") or device.get("name") or device_id
        total += 1
        if upsert_new_device_button(device_id, entity_id, name):
            updated += 1
    if updated:
        dashboard_path = resolve_config_dashboard_path()
        _sync_config_dashboard_to_pi(dashboard_path)
        try:
            ha.set_input_boolean("input_boolean.new_device_found", "on")
        except Exception:
            pass
    return total, updated


def _apply_help_messages(ha: HAClient, messages: dict[str, str]) -> None:
    for view_key, text in (messages or {}).items():
        entity_id = HelpMessageManager.entity_id_for_view(view_key)
        ha.call_service("input_text", "set_value", {"entity_id": entity_id, "value": text})


def _sync_help_messages_from_cloud(log) -> None:
    settings = Settings()
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        log.warn("Help messages sync skipped: cloud credentials missing.")
        return
    ha = _resolve_ha_client()
    if not ha:
        log.warn("Help messages sync skipped: HA client unavailable.")
        return
    cloud = CloudClient(
        base_url=settings.HAUSIE_CLOUD_URL,
        token=settings.HAUSIE_CLOUD_TOKEN,
        timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        create_hausie_timeout_s=settings.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT,
    )
    payload = cloud.request_help_messages()
    views = payload.get("views") if isinstance(payload, dict) else None
    if not isinstance(views, dict):
        log.warn("Help messages sync skipped: invalid cloud payload.")
        return
    manager = HelpMessageManager(path=_resolve_help_messages_path())
    manager.update_views(views, replace=True)
    updated = manager.rotate(list(views.keys()) if views else None)
    _apply_help_messages(ha, updated)
    log.ok(
        f"Help messages synced from cloud (version {payload.get('version') or 1}, views {len(views)})."
    )


def _apply_heartbeat_interval_override(action_payload: dict[str, Any], log) -> None:
    state = load_device_state()
    changed = False
    interval = action_payload.get("interval_seconds")
    support_interval = action_payload.get("support_interval_seconds")
    if interval is not None:
        try:
            interval_value = max(60, min(900, int(interval)))
            state["heartbeat_interval_override_s"] = interval_value
            changed = True
        except Exception:
            log.warn(f"Invalid heartbeat interval override: {interval}")
    if support_interval is not None:
        try:
            support_value = max(5, min(300, int(support_interval)))
            state["support_heartbeat_interval_override_s"] = support_value
            changed = True
        except Exception:
            log.warn(f"Invalid support heartbeat interval override: {support_interval}")
    if changed:
        save_device_state(state)
        if _HEARTBEAT:
            _HEARTBEAT.update_intervals(
                interval_s=state.get("heartbeat_interval_override_s"),
                support_interval_s=state.get("support_heartbeat_interval_override_s"),
            )
        log.ok(
            "Heartbeat intervals updated "
            f"(normal={state.get('heartbeat_interval_override_s')}, "
            f"support={state.get('support_heartbeat_interval_override_s')})."
        )


def _apply_plan_badge(ha: HAClient, plan_badge: dict | None) -> None:
    if not plan_badge or not isinstance(plan_badge, dict):
        return
    name = plan_badge.get("name")
    details = plan_badge.get("details")
    trial_until = plan_badge.get("trial_until")
    if name:
        ha.call_service(
            "input_text",
            "set_value",
            {"entity_id": "input_text.hausie_plan_text", "value": str(name)},
        )
    if details is not None:
        ha.call_service(
            "input_text",
            "set_value",
            {"entity_id": "input_text.hausie_plan_details", "value": str(details)},
        )
    ha.call_service(
        "input_text",
        "set_value",
        {"entity_id": "input_text.hausie_trial_until", "value": str(trial_until or "")},
    )


def _refresh_license_state_from_cloud(settings: Settings, log) -> None:
    _sync_license_state_from_cloud(settings, log, force=True)


def _ensure_plan_text_helper(root: Path, plan_badge: dict | None) -> None:
    helpers_dir = root / "helpers" / "input_text"
    helpers_dir.mkdir(parents=True, exist_ok=True)
    helper_path = helpers_dir / "hausie_input_text.dashboards.yaml"
    try:
        doc = yaml.safe_load(helper_path.read_text(encoding="utf-8")) if helper_path.exists() else {}
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    plan_name = (plan_badge or {}).get("name")
    plan_details = (plan_badge or {}).get("details")
    trial_until = (plan_badge or {}).get("trial_until")
    updated = False
    if "hausie_plan_text" not in doc:
        doc["hausie_plan_text"] = {
            "name": "Hausie Plan",
            "max": 255,
            "initial": str(plan_name or ""),
        }
        updated = True
    if "hausie_plan_details" not in doc:
        doc["hausie_plan_details"] = {
            "name": "Hausie Plan Details",
            "max": 255,
            "initial": str(plan_details or ""),
        }
        updated = True
    if "hausie_trial_until" not in doc:
        doc["hausie_trial_until"] = {
            "name": "Hausie Trial Until",
            "max": 255,
            "initial": str(trial_until or ""),
        }
        updated = True
    if updated:
        helper_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _ensure_remote_support_helper(root: Path) -> bool:
    helpers_dir = root / "helpers" / "input_boolean"
    helpers_dir.mkdir(parents=True, exist_ok=True)
    helper_path = helpers_dir / "hausie_input_boolean_general.yaml"
    try:
        doc = yaml.safe_load(helper_path.read_text(encoding="utf-8")) if helper_path.exists() else {}
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if "allow_remote_support" in doc:
        return False
    doc["allow_remote_support"] = {
        "name": "Remote Support",
        "initial": "off",
    }
    helper_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return True


def _turn_on_user_helpers(ha: HAClient) -> int:
    try:
        states = ha.get_states()
    except Exception:
        return 0
    user_entities = [
        state.get("entity_id")
        for state in states
        if isinstance(state, dict)
        and isinstance(state.get("entity_id"), str)
        and state["entity_id"].startswith("input_boolean.perm_")
    ]
    user_entities = [ent for ent in user_entities if ent]
    if not user_entities:
        return 0
    try:
        ha.call_service("input_boolean", "turn_on", {"entity_id": user_entities})
        return len(user_entities)
    except Exception:
        return 0


_REBUILD_PERSIST_HELPER_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
)

_REBUILD_PERSIST_EXACT = {
    "input_boolean.new_device_found",
    "input_text.hausie_plan_text",
    "input_text.hausie_plan_details",
    "input_text.hausie_trial_until",
    "input_text.new_device_name",
    "input_text.new_device_device_id",
    "input_select.new_device_label",
    "input_select.new_device_area",
}


def _should_persist_rebuild_helper(domain: str, object_id: str) -> bool:
    entity_id = f"{domain}.{object_id}"
    if domain not in _REBUILD_PERSIST_HELPER_DOMAINS:
        return False
    if entity_id in _REBUILD_PERSIST_EXACT:
        return False
    return True


def _collect_persistable_helper_entities(root: Path) -> list[tuple[str, str]]:
    helpers_root = root / "helpers"
    collected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for domain in _REBUILD_PERSIST_HELPER_DOMAINS:
        domain_root = helpers_root / domain
        if not domain_root.exists():
            continue
        for helper_path in sorted(domain_root.glob("hausie_*.yaml")):
            try:
                doc = yaml.safe_load(helper_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            for object_id in doc.keys():
                if not isinstance(object_id, str):
                    continue
                if not _should_persist_rebuild_helper(domain, object_id):
                    continue
                entity_id = f"{domain}.{object_id}"
                if entity_id in seen:
                    continue
                seen.add(entity_id)
                collected.append((domain, entity_id))
    return collected


def _snapshot_rebuild_helper_values(ha: HAClient, root: Path, log) -> dict[str, dict[str, Any]]:
    helper_entities = _collect_persistable_helper_entities(root)
    if not helper_entities:
        log.info("No persistable helpers found for rebuild snapshot.")
        return {}
    try:
        states = ha.get_states()
    except Exception as exc:
        log.warn(f"Helper snapshot skipped: {exc}")
        return {}
    states_by_entity = {
        state.get("entity_id"): state
        for state in states
        if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
    }
    snapshot: dict[str, dict[str, Any]] = {}
    for domain, entity_id in helper_entities:
        state = states_by_entity.get(entity_id)
        if not isinstance(state, dict):
            continue
        value = state.get("state")
        if value in {None, "unknown", "unavailable"}:
            continue
        snapshot[entity_id] = {
            "domain": domain,
            "state": value,
            "attributes": state.get("attributes") if isinstance(state.get("attributes"), dict) else {},
        }
    log.info(f"Captured {len(snapshot)} helper values for rebuild restore.")
    return snapshot


def _restore_rebuild_helper_values(ha: HAClient, snapshot: dict[str, dict[str, Any]], log) -> int:
    current_states_by_entity: dict[str, dict[str, Any]] = {}
    try:
        current_states = ha.get_states()
        current_states_by_entity = {
            state.get("entity_id"): state
            for state in current_states
            if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
        }
    except Exception:
        current_states_by_entity = {}
    restored = 0
    for entity_id, item in snapshot.items():
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or item.get("helper_type") or "").strip()
        state = item.get("state")
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        if not attributes:
            live_state = current_states_by_entity.get(entity_id)
            if isinstance(live_state, dict) and isinstance(live_state.get("attributes"), dict):
                attributes = live_state.get("attributes") or {}
        if not domain or state in {None, "unknown", "unavailable"}:
            continue
        try:
            if domain == "input_boolean":
                service = "turn_on" if str(state).lower() == "on" else "turn_off"
                ha.call_service("input_boolean", service, {"entity_id": entity_id})
            elif domain == "input_number":
                ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": float(state)})
            elif domain == "input_select":
                ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": str(state)})
            elif domain == "input_text":
                ha.call_service("input_text", "set_value", {"entity_id": entity_id, "value": str(state)})
            elif domain == "input_datetime":
                has_date = bool(attributes.get("has_date"))
                has_time = bool(attributes.get("has_time"))
                payload: dict[str, Any] = {"entity_id": entity_id}
                if has_date and has_time:
                    payload["datetime"] = str(state)
                elif has_date:
                    payload["date"] = str(state)
                elif has_time:
                    payload["time"] = str(state)
                else:
                    continue
                ha.call_service("input_datetime", "set_datetime", payload)
            else:
                continue
            restored += 1
        except Exception as exc:
            log.warn(f"Failed to restore {entity_id}: {exc}")
    if restored:
        log.ok(f"Restored {restored} helper values after rebuild.")
    else:
        log.info("No helper values restored after rebuild.")
    return restored


def _serialize_helper_snapshot(
    snapshot: dict[str, dict[str, Any]],
    *,
    source_plan: str,
    target_plan: str,
) -> dict[str, Any]:
    device_id, _token = resolve_device_credentials()
    license_state = load_license_state()
    helpers: list[dict[str, Any]] = []
    for entity_id, item in sorted(snapshot.items()):
        if not isinstance(item, dict):
            continue
        helper_type = str(item.get("domain") or item.get("helper_type") or "").strip()
        state = item.get("state")
        if not helper_type or state in {None, "unknown", "unavailable"}:
            continue
        helpers.append(
            {
                "entity_id": entity_id,
                "helper_type": helper_type,
                "state": state,
            }
        )
    return {
        "snapshot_version": 1,
        "created_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source_plan": source_plan,
        "target_plan": target_plan,
        "smart_house_id": license_state.get("smart_house_id"),
        "device_id": device_id,
        "addon_version": _resolve_addon_version(),
        "helpers": helpers,
    }


def _deserialize_helper_snapshot(snapshot_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    helpers = snapshot_payload.get("helpers") if isinstance(snapshot_payload, dict) else None
    if not isinstance(helpers, list):
        return {}
    restored: dict[str, dict[str, Any]] = {}
    for item in helpers:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        helper_type = str(item.get("helper_type") or "").strip()
        if not entity_id or not helper_type:
            continue
        restored[entity_id] = {
            "helper_type": helper_type,
            "state": item.get("state"),
        }
    return restored


def _capture_and_persist_helper_snapshot(ha: HAClient, *, source_plan: str, target_plan: str, log) -> dict[str, Any]:
    snapshot = _snapshot_rebuild_helper_values(ha, _ha_config_root(), log)
    payload = _serialize_helper_snapshot(snapshot, source_plan=source_plan, target_plan=target_plan)
    save_helpers_snapshot(payload)
    log.ok(f"Persisted helper snapshot with {len(payload.get('helpers') or [])} values.")
    return payload


def _restore_persisted_helper_snapshot(ha: HAClient, log) -> int:
    payload = load_helpers_snapshot()
    snapshot = _deserialize_helper_snapshot(payload)
    if not snapshot:
        log.info("No persisted helper snapshot available.")
        return 0
    return _restore_rebuild_helper_values(ha, snapshot, log)


def _normalize_license_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    resolved_plan = source.get("plan")
    if not resolved_plan:
        resolved_plan = source.get("tier")
    normalized = {
        "schema_version": 1,
        "plan": _normalize_plan_id(resolved_plan, "") or None,
        "base_plan": _normalize_plan_id(source.get("base_plan"), "") or None,
        "license_status": str(source.get("license_status") or "").strip() or None,
        "subscription_status": str(source.get("subscription_status") or "").strip() or None,
        "billing_cycle": str(source.get("billing_cycle") or "").strip() or None,
        "current_period_end": source.get("current_period_end"),
        "grace_ends_at": source.get("grace_ends_at"),
        "offline_valid_until": source.get("offline_valid_until"),
        "last_license_sync_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "smart_house_id": source.get("smart_house_id"),
        "customer_id": source.get("customer_id"),
        "templates_version": source.get("templates_version"),
        "settings_version": source.get("settings_version"),
        "portal_message": source.get("portal_message") if isinstance(source.get("portal_message"), dict) else {},
        "addon_message": source.get("addon_message") if isinstance(source.get("addon_message"), dict) else {},
    }
    return normalized


def _store_license_payload(payload: dict[str, Any] | None, log) -> dict[str, Any]:
    normalized = _normalize_license_payload(payload)
    current = load_license_state()
    current.update({k: v for k, v in normalized.items() if v is not None or k in {"portal_message", "addon_message"}})
    if not current.get("plan"):
        current["plan"] = "plan_1"
    if not current.get("license_status"):
        current["license_status"] = "active"
    save_license_state(current)
    log.info(
        f"License state updated: plan={current.get('plan')}, "
        f"status={current.get('license_status')}, offline_valid_until={current.get('offline_valid_until')}."
    )
    return current


def _save_free_plan_bundle(kind: str, response: dict[str, Any], log) -> None:
    if not isinstance(response, dict):
        return
    payload = {
        "schema_version": 1,
        "cached_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "kind": kind,
        "artifacts": response.get("artifacts") if isinstance(response.get("artifacts"), list) else [],
        "deletes": response.get("deletes") if isinstance(response.get("deletes"), list) else [],
        "plan_badge": response.get("plan_badge") if isinstance(response.get("plan_badge"), dict) else {},
        "ui": response.get("ui") if isinstance(response.get("ui"), dict) else {},
        "voice": response.get("voice") if isinstance(response.get("voice"), dict) else {},
    }
    save_free_plan_cache(kind, payload)
    log.info(f"Updated local free-plan cache for {kind}.")


def _refresh_free_plan_cache(kind: str, cloud: CloudClient, payload: dict[str, Any], log) -> None:
    free_payload = dict(payload or {})
    free_payload["plan_override"] = "plan_1"
    free_payload["force_full"] = True
    try:
        if kind == "base":
            response = cloud.request_base_assets(free_payload)
        else:
            response = cloud.request_sync_inventory(free_payload)
    except Exception as exc:
        log.warn(f"Free-plan cache refresh failed for {kind}: {exc}")
        return
    _save_free_plan_bundle(kind, response, log)


def _confirm_pairing_device(payload: dict[str, Any]) -> dict[str, Any]:
    if not _pairing_is_unlocked():
        raise RuntimeError("Upgrade Hausie to add new devices by yourself.")
    device_id = str(payload.get("device_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    area = str(payload.get("area") or "").strip()
    labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    normalized_labels = []
    for item in labels:
        label_name = str(item or "").strip()
        if label_name and label_name not in normalized_labels:
            normalized_labels.append(label_name)
    if not device_id:
        raise ValueError("device_id is required.")
    if not name:
        raise ValueError("name is required.")
    if not area:
        raise ValueError("area is required.")
    if not normalized_labels:
        raise ValueError("at least one label is required.")
    ha = _resolve_ha_client()
    if not ha:
        raise RuntimeError("HA_TOKEN is required.")
    with _PAIRING_LOCK:
        _PAIRING_STATE["status"] = "saving"
        _PAIRING_STATE["message"] = "Saving device configuration and refreshing Hausie..."
        _PAIRING_STATE["error"] = ""
    resolved_device_id = _update_device_name_area(
        ha_ws_url=ha.ha_url_ws,
        token=ha.token,
        device_id=device_id,
        name=name,
        area_name=area,
    )
    applied_labels = _update_device_labels(
        base_url=ha.ha_url_rest.rsplit("/api", 1)[0],
        username=resolve_ha_runtime_credentials()[1] or "",
        password=resolve_ha_runtime_credentials()[2] or "",
        device_id=resolved_device_id,
        labels=normalized_labels,
    )
    _run_sync_inventory()
    with _PAIRING_LOCK:
        _PAIRING_STATE["active"] = False
        _PAIRING_STATE["status"] = "completed"
        _PAIRING_STATE["message"] = "Device added successfully."
        _PAIRING_STATE["completed_device_id"] = resolved_device_id
        _PAIRING_STATE["selected_device_id"] = resolved_device_id
        candidate = (_PAIRING_STATE.get("devices") or {}).get(device_id)
        if isinstance(candidate, dict):
            candidate["status"] = "saved"
            candidate["name"] = name
            candidate["area_name"] = area
            candidate["applied_labels"] = applied_labels
    return {
        "ok": True,
        "device_id": resolved_device_id,
        "applied_labels": applied_labels,
    }


def _render_pairing_html(ingress_path: str = "") -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Hausie Device Pairing</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f5f5f5;
        --surface: #ffffff;
        --surface-tint: #e3edef;
        --muted: #637277;
        --text: #0a0a0a;
        --primary: #00434c;
        --primary-dark: #00343b;
        --success: #0d7c1b;
        --warning: #9a6100;
        --danger: #b42318;
        --border: #dfe5e7;
        --shadow: 0 18px 60px rgba(10, 10, 10, 0.08);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: "Poppins", "Avenir Next", "Segoe UI", sans-serif;
        background: radial-gradient(circle at top left, rgba(227, 237, 239, 0.95), transparent 31%), radial-gradient(circle at bottom right, rgba(215, 231, 234, 0.8), transparent 27%), var(--bg);
        color: var(--text);
      }
      .page {
        max-width: 720px;
        margin: 0 auto;
        padding: 26px 16px 42px;
      }
      .eyebrow { margin: 0 0 9px; color: var(--primary); font-size: 12px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
      .header h1 {
        margin: 0 0 8px;
        font-family: "Bai Jamjuree", "Avenir Next", "Segoe UI", sans-serif;
        font-size: clamp(30px, 8vw, 42px);
        letter-spacing: -0.03em;
      }
      .header p {
        margin: 0;
        color: var(--muted);
        line-height: 1.5;
      }
      .panel {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 24px;
        padding: 20px;
        margin-top: 16px;
        box-shadow: var(--shadow);
      }
      .status {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      .status-dot {
        width: 12px;
        height: 12px;
        border-radius: 999px;
        background: var(--primary);
        box-shadow: 0 0 0 0 rgba(0, 67, 76, 0.24);
      }
      .status-dot.searching, .status-dot.configuring { animation: pulse 1.4s infinite; }
      .status-dot.ready { background: var(--success); }
      .status-dot.completed { background: var(--success); }
      .status-dot.error, .status-dot.upgrade_required { background: var(--danger); }
      .actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        margin-top: 14px;
      }
      button {
        border: 0;
        border-radius: 12px;
        min-height: 50px;
        font-size: 15px;
        font-weight: 600;
        padding: 0 16px;
        cursor: pointer;
      }
      .primary { background: var(--primary); color: white; box-shadow: 0 1px 2px rgba(0, 67, 76, 0.3); }
      .primary:hover { background: var(--primary-dark); }
      .secondary { background: var(--surface-tint); color: var(--primary); border: 1px solid #c8d4d8; }
      .ghost { background: var(--surface); color: var(--primary); border: 1px solid #c8d4d8; }
      .upgrade { border-left: 4px solid var(--warning); padding-left: 14px; }
      .device-list { display: grid; gap: 10px; margin-top: 14px; }
      .device-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 14px;
        cursor: pointer;
      }
      .device-card.selected { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(0,67,76,0.12); }
      .device-card h3 { margin: 0 0 6px; font-size: 17px; }
      .meta { color: var(--muted); font-size: 13px; line-height: 1.45; }
      .pill-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
      .pill {
        background: var(--surface-tint);
        border: 1px solid var(--border);
        color: var(--text);
        border-radius: 999px;
        padding: 6px 10px;
        font-size: 12px;
      }
      .pill.ready { background: #e9f7ec; border-color: #b8ddc0; }
      .pill.configuring { background: #fff5df; border-color: #ead39d; }
      .form-grid { display: grid; gap: 12px; margin-top: 14px; }
      label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
      input, select {
        width: 100%;
        min-height: 46px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: var(--surface);
        color: var(--text);
        padding: 0 12px;
        font-size: 15px;
      }
      .checkbox-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }
      .checkbox {
        display: flex;
        gap: 10px;
        align-items: center;
        padding: 12px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: var(--surface);
      }
      .checkbox input { width: 18px; min-height: 18px; }
      .hint, .small { color: var(--muted); font-size: 13px; line-height: 1.5; }
      .hidden { display: none !important; }
      .footer-note { margin-top: 14px; color: var(--muted); font-size: 12px; text-align: center; }
      @keyframes pulse {
        0% { box-shadow: 0 0 0 0 rgba(0, 67, 76, 0.24); }
        70% { box-shadow: 0 0 0 12px rgba(0, 67, 76, 0); }
        100% { box-shadow: 0 0 0 0 rgba(0, 67, 76, 0); }
      }
      @media (max-width: 520px) {
        .actions { grid-template-columns: 1fr; }
        .checkbox-grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <main class="page">
      <section class="header">
        <p class="eyebrow">Hausie local app</p>
        <h1>Add Device</h1>
        <p>Pair a new ZHA device locally, choose its room, add Hausie labels, and refresh your setup without depending on the cloud.</p>
      </section>

      <section class="panel" id="upgradePanel">
        <div class="upgrade">
          <strong>Upgrade required</strong>
          <p class="hint" style="margin:8px 0 0;">Upgrade Hausie to add new devices by yourself.</p>
        </div>
      </section>

      <section class="panel" id="wizardPanel">
        <div class="status">
          <div class="status-dot" id="statusDot"></div>
          <div>
            <div id="statusTitle" style="font-weight:700;">Ready</div>
            <div class="small" id="statusMessage">Ready to start ZHA pairing.</div>
          </div>
        </div>
        <div class="actions">
          <button class="primary" id="startBtn">Start ZHA pairing</button>
          <button class="ghost" id="stopBtn">Stop</button>
        </div>
        <div class="footer-note">Keep the device in pairing mode while Hausie is searching.</div>
      </section>

      <section class="panel hidden" id="devicesPanel">
        <h2 style="margin:0 0 10px; font-size:20px;">New devices found</h2>
        <div class="device-list" id="deviceList"></div>
      </section>

      <section class="panel hidden" id="formPanel">
        <h2 id="formTitle" style="margin:0 0 10px; font-size:20px;">Configure device</h2>
        <p class="hint" id="formHint" style="margin:0 0 12px;"></p>
        <div class="form-grid">
          <div>
            <label for="deviceName">Name</label>
            <input id="deviceName" type="text" maxlength="80"/>
          </div>
          <div>
            <label for="deviceArea">Area</label>
            <select id="deviceArea"></select>
          </div>
          <div>
            <label>Labels</label>
            <div class="checkbox-grid" id="labelGrid"></div>
          </div>
          <button class="primary" id="confirmBtn">Add device</button>
        </div>
      </section>
    </main>
    <script>
      const INGRESS_PATH = __INGRESS_PATH__;
      const CSRF_TOKEN = __CSRF_TOKEN__;
      const state = {
        payload: null,
        selectedId: "",
      };

      const statusDot = document.getElementById("statusDot");
      const statusTitle = document.getElementById("statusTitle");
      const statusMessage = document.getElementById("statusMessage");
      const wizardPanel = document.getElementById("wizardPanel");
      const upgradePanel = document.getElementById("upgradePanel");
      const devicesPanel = document.getElementById("devicesPanel");
      const formPanel = document.getElementById("formPanel");
      const deviceList = document.getElementById("deviceList");
      const formTitle = document.getElementById("formTitle");
      const formHint = document.getElementById("formHint");
      const deviceName = document.getElementById("deviceName");
      const deviceArea = document.getElementById("deviceArea");
      const labelGrid = document.getElementById("labelGrid");
      const confirmBtn = document.getElementById("confirmBtn");

      async function request(path, options = {}) {
        const requestPath = path.startsWith("/") ? path : "/" + path;
        const response = await fetch(INGRESS_PATH + requestPath, {
          ...options,
          headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
            "X-Hausie-CSRF-Token": CSRF_TOKEN
          }
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Request failed");
        return data;
      }

      function humanStatus(status) {
        const map = {
          idle: "Ready",
          searching: "Searching",
          configuring: "Configuring",
          ready: "Ready to configure",
          saving: "Saving",
          completed: "Completed",
          error: "Error",
          upgrade_required: "Upgrade required",
        };
        return map[status] || "Pairing";
      }

      function renderStatus(payload) {
        statusDot.className = `status-dot ${payload.status || "idle"}`;
        statusTitle.textContent = humanStatus(payload.status);
        statusMessage.textContent = payload.message || "";
        const locked = !payload.plan_unlocked;
        upgradePanel.classList.toggle("hidden", !locked);
        wizardPanel.classList.toggle("hidden", locked);
      }

      function ensureAreaOptions(payload) {
        const current = deviceArea.value;
        deviceArea.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Select area";
        deviceArea.appendChild(placeholder);
        for (const area of payload.areas || []) {
          const option = document.createElement("option");
          option.value = area;
          option.textContent = area;
          if (area === current) option.selected = true;
          deviceArea.appendChild(option);
        }
      }

      function renderLabelOptions(payload, selectedLabels) {
        labelGrid.innerHTML = "";
        for (const label of payload.labels || []) {
          const wrapper = document.createElement("label");
          wrapper.className = "checkbox";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = label;
          input.checked = selectedLabels.includes(label);
          const text = document.createElement("span");
          text.textContent = label.replaceAll("_", " ");
          wrapper.appendChild(input);
          wrapper.appendChild(text);
          labelGrid.appendChild(wrapper);
        }
      }

      function renderDevices(payload) {
        const devices = payload.devices || [];
        devicesPanel.classList.toggle("hidden", devices.length === 0);
        deviceList.innerHTML = "";
        for (const device of devices) {
          const card = document.createElement("button");
          card.type = "button";
          card.className = `device-card ${state.selectedId === device.device_id ? "selected" : ""}`;
          card.onclick = () => {
            state.selectedId = device.device_id;
            render(payload);
          };
          const title = document.createElement("h3");
          title.textContent = device.name || device.device_id;
          const meta = document.createElement("div");
          meta.className = "meta";
          const parts = [];
          if (device.manufacturer) parts.push(device.manufacturer);
          if (device.model) parts.push(device.model);
          if (device.entity_count) parts.push(`${device.entity_count} entities`);
          meta.textContent = parts.join(" • ");
          const pills = document.createElement("div");
          pills.className = "pill-row";
          const statePill = document.createElement("span");
          statePill.className = `pill ${device.status || "configuring"}`;
          statePill.textContent = humanStatus(device.status);
          pills.appendChild(statePill);
          for (const label of device.suggested_labels || []) {
            const pill = document.createElement("span");
            pill.className = "pill";
            pill.textContent = label.replaceAll("_", " ");
            pills.appendChild(pill);
          }
          card.appendChild(title);
          card.appendChild(meta);
          card.appendChild(pills);
          deviceList.appendChild(card);
        }
      }

      function renderForm(payload) {
        const devices = payload.devices || [];
        const selected = devices.find((item) => item.device_id === state.selectedId)
          || devices.find((item) => item.status === "ready")
          || null;
        if (!selected || selected.status !== "ready") {
          formPanel.classList.add("hidden");
          return;
        }
        state.selectedId = selected.device_id;
        formPanel.classList.remove("hidden");
        formTitle.textContent = selected.name || selected.device_id;
        const suggested = (selected.suggested_labels || []).map((item) => item.replaceAll("_", " "));
        formHint.textContent = suggested.length
          ? `This device seems like: ${suggested.join(", ")}.`
          : "Select the labels that describe this device.";
        ensureAreaOptions(payload);
        deviceName.value = deviceName.value && deviceName.dataset.deviceId === selected.device_id
          ? deviceName.value
          : (selected.suggested_name || selected.name || "");
        deviceName.dataset.deviceId = selected.device_id;
        if (!deviceArea.value) {
          deviceArea.value = selected.area_name || "";
        }
        const preservedLabels = deviceName.dataset.deviceId === selected.device_id
          ? Array.from(labelGrid.querySelectorAll('input[type="checkbox"]:checked')).map((el) => el.value)
          : [];
        const selectedLabels = preservedLabels.length
          ? preservedLabels
          : Array.from(new Set(selected.suggested_labels || []));
        renderLabelOptions(payload, selectedLabels);
      }

      function render(payload) {
        state.payload = payload;
        renderStatus(payload);
        renderDevices(payload);
        renderForm(payload);
      }

      async function refreshStatus() {
        try {
          const payload = await request("/pairing/status");
          if (!state.selectedId && payload.selected_device_id) {
            state.selectedId = payload.selected_device_id;
          }
          render(payload);
        } catch (error) {
          statusDot.className = "status-dot error";
          statusTitle.textContent = "Error";
          statusMessage.textContent = error.message;
        }
      }

      document.getElementById("startBtn").addEventListener("click", async () => {
        try {
          const payload = await request("/pairing/start", { method: "POST", body: JSON.stringify({ duration: 90 }) });
          state.selectedId = "";
          render(payload);
        } catch (error) {
          alert(error.message);
        }
      });

      document.getElementById("stopBtn").addEventListener("click", async () => {
        try {
          const payload = await request("/pairing/stop", { method: "POST", body: "{}" });
          render(payload);
        } catch (error) {
          alert(error.message);
        }
      });

      confirmBtn.addEventListener("click", async () => {
        if (!state.payload) return;
        const labels = Array.from(labelGrid.querySelectorAll('input[type="checkbox"]:checked')).map((el) => el.value);
        try {
          confirmBtn.disabled = true;
          await request("/pairing/confirm", {
            method: "POST",
            body: JSON.stringify({
              device_id: state.selectedId,
              name: deviceName.value.trim(),
              area: deviceArea.value.trim(),
              labels,
            }),
          });
          await refreshStatus();
        } catch (error) {
          alert(error.message);
        } finally {
          confirmBtn.disabled = false;
        }
      });

      refreshStatus();
      setInterval(refreshStatus, 2000);
    </script>
  </body>
</html>""".replace("__INGRESS_PATH__", json.dumps(_normalize_ingress_path(ingress_path))).replace("__CSRF_TOKEN__", json.dumps(_UI_CSRF_TOKEN))


def _render_setup_html(ingress_path: str = "") -> str:
    status = _setup_status_payload()
    admin_username = html.escape(HAUSIE_ADMIN_USERNAME)
    support_username = html.escape(HAUSIE_SUPPORT_USERNAME)
    credential_fields = ""
    if not status["credentials"]["credentials_valid"]:
        credential_fields = """          <label>Home Assistant token
            <input id="haToken" type="password" autocomplete="off" placeholder="Paste a long-lived access token"/>
            <small>Leave blank only if it is already configured.</small>
          </label>
          <label>Hausie support password
            <input id="supportPassword" type="password" autocomplete="new-password" placeholder="Password for __SUPPORT_USERNAME__"/>
            <small>Used by the local Hausie support user. Leave blank only if already configured.</small>
          </label>
          <label>Hausie administrator password
            <input id="adminPassword" type="password" autocomplete="new-password" placeholder="Password for __ADMIN_USERNAME__"/>
            <small>Used only to create or update the local administrator. Hausie does not retain this password.</small>
          </label>
"""
    return """<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Complete Hausie setup</title>
    <style>
      :root { color-scheme:light; --bg:#f5f5f5; --surface:#ffffff; --tint:#e3edef; --text:#0a0a0a; --muted:#637277; --primary:#00434c; --primary-dark:#00343b; --ok:#0d7c1b; --warn:#9a6100; --danger:#b42318; --border:#dfe5e7; --shadow:0 18px 60px rgba(10,10,10,.08); }
      * { box-sizing:border-box; }
      body { margin:0; min-height:100vh; color:var(--text); font-family:"Poppins","Avenir Next","Segoe UI",sans-serif; background:radial-gradient(circle at top left,rgba(227,237,239,.95),transparent 31%),radial-gradient(circle at bottom right,rgba(215,231,234,.8),transparent 27%),var(--bg); }
      main { width:min(100%,720px); margin:0 auto; padding:28px 16px 44px; }
      .hero,.panel { border:1px solid var(--border); border-radius:28px; background:var(--surface); box-shadow:var(--shadow); }
      .hero { padding:24px; }
      .eyebrow { margin:0 0 9px; color:var(--primary); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
      h1 { margin:0 0 8px; font-family:"Bai Jamjuree","Avenir Next","Segoe UI",sans-serif; font-size:clamp(30px,9vw,44px); letter-spacing:-.03em; line-height:1.05; }
      h2 { margin:0; font-size:19px; }
      p { color:var(--muted); line-height:1.5; margin:0; }
      .panel { margin-top:16px; padding:22px; }
      .steps { display:grid; gap:10px; margin-top:16px; }
      .step { display:flex; align-items:center; gap:12px; padding:14px; border:1px solid var(--border); border-radius:16px; background:#fafcfc; }
      .dot { width:12px; height:12px; border-radius:50%; background:var(--warn); flex:0 0 auto; }
      .step.ok .dot { background:var(--ok); }
      .step.running .dot { background:var(--primary); animation:pulse 1s infinite alternate; }
      .step .copy { min-width:0; }
      .step strong { display:block; font-size:14px; }
      .step span { display:block; color:var(--muted); font-size:13px; margin-top:2px; overflow-wrap:anywhere; }
      form { display:grid; gap:15px; margin-top:16px; }
      label { display:grid; gap:7px; font-size:14px; font-weight:700; }
      small { color:var(--muted); font-weight:400; line-height:1.35; }
      input { min-height:52px; width:100%; border:1px solid var(--border); border-radius:12px; padding:12px 14px; background:var(--surface); color:var(--text); font-size:16px; }
      input:focus { outline:2px solid rgba(0,67,76,.18); border-color:var(--primary); }
      button,a.button { display:inline-flex; width:100%; min-height:52px; align-items:center; justify-content:center; border:0; border-radius:12px; padding:13px 16px; background:var(--primary); color:#fff; font-size:16px; font-weight:800; text-decoration:none; cursor:pointer; box-shadow:0 1px 2px rgba(0,67,76,.3); }
      button:hover,a.button:hover { background:var(--primary-dark); }
      button:disabled { opacity:.58; cursor:wait; }
      .secondary { margin-top:10px; background:var(--surface)!important; border:1px solid #c8d4d8!important; color:var(--primary)!important; box-shadow:none!important; }
      .message { display:none; margin-top:14px; border-radius:13px; padding:12px 14px; line-height:1.45; }
      .message.show { display:block; }
      .message.error { background:rgba(255,123,123,.12); border:1px solid rgba(255,123,123,.38); }
      .message.success { background:rgba(82,214,139,.12); border:1px solid rgba(82,214,139,.38); }
      @keyframes pulse { from { opacity:.35; transform:scale(.82); } to { opacity:1; transform:scale(1.12); } }
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <p class="eyebrow">Hausie local app</p>
        <h1>Complete Hausie setup</h1>
        <p>Connect this Home Assistant installation to its Hausie home, then create the initial dashboards and automations.</p>
        <div class="steps">
          <div class="step" id="credentialsStep"><i class="dot"></i><div class="copy"><strong>Local credentials</strong><span id="credentialsText">Checking...</span></div></div>
          <div class="step" id="pairingStep"><i class="dot"></i><div class="copy"><strong>Hausie pairing</strong><span id="pairingText">Checking...</span></div></div>
          <div class="step" id="initializeStep"><i class="dot"></i><div class="copy"><strong>Initialize Hausie</strong><span id="initializeText">Checking...</span></div></div>
        </div>
      </section>
      <section class="panel">
        <h2>Installation details</h2>
        <form id="setupForm">
__CREDENTIAL_FIELDS__
          <label>Hausie pairing code
            <input id="pairingCode" type="password" autocomplete="off" placeholder="Code for this customer's Hausie home"/>
            <small>Required only when this Pi has not been paired yet. It is not stored after registration.</small>
          </label>
          <button id="initializeButton" type="submit">Initialize Hausie</button>
        </form>
        <div id="message" class="message"></div>
        <a id="openConfig" class="button secondary" href="/config-dashboard/main" hidden>Open Configuration</a>
      </section>
    </main>
    <script>
      const INGRESS_PATH = __INGRESS_PATH__;
      const CSRF_TOKEN = __CSRF_TOKEN__;
      const message = document.getElementById("message");
      const initializeButton = document.getElementById("initializeButton");
      const openConfig = document.getElementById("openConfig");
      let currentStatus = null;

      async function request(path, options = {}) {
        const requestPath = path.startsWith("/") ? path : "/" + path;
        const response = await fetch(INGRESS_PATH + requestPath, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}), "X-Hausie-CSRF-Token": CSRF_TOKEN } });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
        return payload;
      }

      function showMessage(kind, text) {
        message.className = `message show ${kind}`;
        message.textContent = text;
      }

      function setStep(id, complete, running, text) {
        const element = document.getElementById(id);
        element.classList.toggle("ok", complete);
        element.classList.toggle("running", running);
        element.querySelector("span").textContent = text;
      }

      function render(status) {
        currentStatus = status;
        const credentialsReady = status.has_token && status.has_support_password && status.has_admin_password;
        setStep("credentialsStep", credentialsReady, false, credentialsReady ? "Configured" : "Token, support password, and administrator password are required");
        setStep("pairingStep", status.paired, false, status.paired ? `Paired as ${status.device_id}` : "Pairing code is required");
        setStep("initializeStep", status.initialized, status.initializing, status.initialized ? "Completed" : status.message);
        initializeButton.disabled = Boolean(status.initializing || status.initialized);
        initializeButton.textContent = status.initializing ? "Initializing Hausie..." : status.initialized ? "Hausie initialized" : "Initialize Hausie";
        openConfig.hidden = !status.initialized;
        if (status.phase === "failed") showMessage("error", status.message);
      }

      async function refreshStatus() {
        try { render(await request("/setup/status")); } catch (error) { showMessage("error", error.message); }
      }

      document.getElementById("setupForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (currentStatus?.initializing || currentStatus?.initialized) return;
        try {
          initializeButton.disabled = true;
          const response = await request("/setup/initialize", {
            method: "POST",
            body: JSON.stringify({
              ha_token: document.getElementById("haToken")?.value.trim() || "",
              support_password: document.getElementById("supportPassword")?.value || "",
              admin_password: document.getElementById("adminPassword")?.value || "",
              pairing_code: document.getElementById("pairingCode").value.trim()
            })
          });
          document.getElementById("pairingCode").value = "";
          showMessage("success", response.message || "Hausie initialization started.");
          await refreshStatus();
        } catch (error) {
          showMessage("error", error.message);
          initializeButton.disabled = false;
        }
      });

      refreshStatus();
      setInterval(refreshStatus, 2000);
    </script>
  </body>
</html>""".replace("__CREDENTIAL_FIELDS__", credential_fields).replace("__ADMIN_USERNAME__", admin_username).replace("__SUPPORT_USERNAME__", support_username).replace("__INGRESS_PATH__", json.dumps(_normalize_ingress_path(ingress_path))).replace("__CSRF_TOKEN__", json.dumps(_UI_CSRF_TOKEN))


def _render_portal_html() -> str:
    return """<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Hausie App</title>
    <style>
      :root { color-scheme:light; --bg:#f5f5f5; --surface:#ffffff; --text:#0a0a0a; --muted:#637277; --primary:#00434c; --primary-dark:#00343b; --border:#dfe5e7; --shadow:0 18px 60px rgba(10,10,10,.08); }
      * { box-sizing:border-box; }
      body { min-height:100vh; margin:0; display:grid; place-items:center; padding:20px; color:var(--text); font-family:"Poppins","Avenir Next","Segoe UI",sans-serif; background:radial-gradient(circle at top left,rgba(227,237,239,.95),transparent 31%),radial-gradient(circle at bottom right,rgba(215,231,234,.8),transparent 27%),var(--bg); }
      main { width:min(100%,560px); padding:32px 24px; border:1px solid var(--border); border-radius:28px; background:var(--surface); box-shadow:var(--shadow); text-align:center; }
      .eyebrow { margin:0 0 9px; color:var(--primary); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
      h1 { margin:0; font-family:"Bai Jamjuree","Avenir Next","Segoe UI",sans-serif; font-size:clamp(32px,9vw,46px); letter-spacing:-.03em; }
      p { margin:14px 0 24px; color:var(--muted); line-height:1.55; }
      a { display:inline-flex; min-height:52px; width:100%; align-items:center; justify-content:center; border-radius:12px; padding:13px 16px; color:#fff; background:var(--primary); font-weight:800; font-size:16px; text-decoration:none; }
      a:hover { background:var(--primary-dark); }
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">Hausie local app</p>
      <h1>Hausie is ready</h1>
      <p>Manage your home, plan, and support from the Hausie user portal.</p>
      <a href="https://portal.hausiehome.com" target="_blank" rel="noopener noreferrer">Open Hausie Portal</a>
    </main>
  </body>
</html>"""


def _render_credentials_html(ingress_path: str = "") -> str:
    status = _ha_credentials_status_payload()
    admin_username = html.escape(str(status.get("admin_username") or HAUSIE_ADMIN_USERNAME))
    support_username = html.escape(str(status.get("support_username") or HAUSIE_SUPPORT_USERNAME))
    status_json = json.dumps(status)
    ingress_path_json = json.dumps(_normalize_ingress_path(ingress_path))
    csrf_token_json = json.dumps(_UI_CSRF_TOKEN)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Hausie credentials</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f5f5f5;
        --surface: #ffffff;
        --tint: #e3edef;
        --text: #0a0a0a;
        --muted: #637277;
        --primary: #00434c;
        --primary-dark: #00343b;
        --danger: #b42318;
        --ok: #0d7c1b;
        --border: #dfe5e7;
        --shadow: 0 18px 60px rgba(10, 10, 10, 0.08);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Poppins", "Avenir Next", "Segoe UI", sans-serif;
        background: radial-gradient(circle at top left, rgba(227,237,239,.95), transparent 31%), radial-gradient(circle at bottom right, rgba(215,231,234,.8), transparent 27%), var(--bg);
        color: var(--text);
        min-height: 100vh;
      }}
      .shell {{
        max-width: 760px;
        margin: 0 auto;
        padding: 28px 16px 44px;
      }}
      .hero {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 28px;
        padding: 24px;
        box-shadow: var(--shadow);
      }}
      .eyebrow {{ margin: 0 0 9px; color: var(--primary); font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }}
      h1 {{
        margin: 0 0 10px;
        font-family: "Bai Jamjuree", "Avenir Next", "Segoe UI", sans-serif;
        font-size: clamp(30px, 8vw, 44px);
        letter-spacing: -.03em;
      }}
      p {{
        margin: 0;
        color: var(--muted);
        line-height: 1.55;
      }}
      .grid {{
        display: grid;
        gap: 14px;
        margin-top: 18px;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      }}
      .stat, form {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 18px;
      }}
      .stat {{
        padding: 16px;
      }}
      .label {{
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      .value {{
        margin-top: 8px;
        font-size: 18px;
        font-weight: 700;
      }}
      .ok {{ color: var(--ok); }}
      .bad {{ color: var(--danger); }}
      form {{
        margin-top: 18px;
        padding: 18px;
        display: grid;
        gap: 16px;
      }}
      .field {{
        display: grid;
        gap: 8px;
      }}
      .field label {{
        font-size: 14px;
        font-weight: 600;
      }}
      .field small {{
        color: var(--muted);
      }}
      input {{
        width: 100%;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: var(--surface);
        color: var(--text);
        padding: 14px 16px;
        font-size: 16px;
      }}
      .button-row {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }}
      button, .link-btn {{
        border: 0;
        border-radius: 12px;
        padding: 14px 18px;
        font-size: 16px;
        font-weight: 700;
        cursor: pointer;
        text-decoration: none;
      }}
      button {{
        background: var(--primary);
        color: #ffffff;
      }}
      .link-btn {{
        background: var(--surface);
        color: var(--primary);
        border: 1px solid #c8d4d8;
      }}
      .message {{
        margin-top: 14px;
        border-radius: 14px;
        padding: 14px 16px;
        display: none;
      }}
      .message.show {{ display: block; }}
      .message.error {{
        background: rgba(255,123,123,0.12);
        border: 1px solid rgba(255,123,123,0.35);
      }}
      .message.success {{
        background: rgba(73,209,141,0.12);
        border: 1px solid rgba(73,209,141,0.35);
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <p class="eyebrow">Hausie local app</p>
        <h1>Hausie credentials</h1>
        <p>Save the Home Assistant token and the passwords for the local Hausie administrator and support users. Hausie uses <strong>{support_username}</strong> for WebSocket access and UI automation; it does not retain the administrator password after saving it.</p>

        <div class="grid">
          <div class="stat">
            <div class="label">Home Assistant token</div>
            <div class="value" id="tokenStatus"></div>
          </div>
          <div class="stat">
            <div class="label">Support password</div>
            <div class="value" id="passwordStatus"></div>
          </div>
          <div class="stat">
            <div class="label">Administrator password</div>
            <div class="value" id="adminPasswordStatus"></div>
          </div>
          <div class="stat">
            <div class="label">Administrator user</div>
            <div class="value">{admin_username}</div>
          </div>
          <div class="stat">
            <div class="label">Support user</div>
            <div class="value">{support_username}</div>
          </div>
        </div>

        <form id="credentialsForm">
          <div class="field">
            <label for="haToken">Home Assistant token</label>
            <input id="haToken" name="haToken" type="password" autocomplete="off" placeholder="Paste a long-lived access token"/>
            <small>Leave blank to keep the current token if one is already saved.</small>
          </div>
          <div class="field">
            <label for="supportPassword">Support user password</label>
            <input id="supportPassword" name="supportPassword" type="password" autocomplete="new-password" placeholder="Password for {support_username}"/>
            <small>Leave blank to keep the current password if one is already saved.</small>
          </div>
          <div class="field">
            <label for="adminPassword">Administrator password</label>
            <input id="adminPassword" name="adminPassword" type="password" autocomplete="new-password" placeholder="Password for {admin_username}"/>
            <small>Required on first setup. Leave blank later to keep the current administrator password.</small>
          </div>
          <div class="button-row">
            <button id="saveBtn" type="submit">Save credentials</button>
            <a class="link-btn" href="/config-dashboard/main">Back to config</a>
          </div>
        </form>
        <div class="message" id="messageBox"></div>
      </section>
    </div>
    <script>
      const INGRESS_PATH = {ingress_path_json};
      const CSRF_TOKEN = {csrf_token_json};
      const initialStatus = {status_json};
      const messageBox = document.getElementById("messageBox");
      const saveBtn = document.getElementById("saveBtn");
      const tokenStatus = document.getElementById("tokenStatus");
      const passwordStatus = document.getElementById("passwordStatus");
      const adminPasswordStatus = document.getElementById("adminPasswordStatus");

      function renderStatus(status) {{
        tokenStatus.textContent = status.has_token ? "Configured" : "Missing";
        tokenStatus.className = `value ${{status.has_token ? "ok" : "bad"}}`;
        passwordStatus.textContent = status.has_support_password ? "Configured" : "Missing";
        passwordStatus.className = `value ${{status.has_support_password ? "ok" : "bad"}}`;
        adminPasswordStatus.textContent = status.has_admin_password ? "Configured" : "Missing";
        adminPasswordStatus.className = `value ${{status.has_admin_password ? "ok" : "bad"}}`;
      }}

      async function request(path, options = {{}}) {{
        const requestPath = path.startsWith("/") ? path : "/" + path;
        const response = await fetch(INGRESS_PATH + requestPath, {{
          ...options,
          headers: {{
            "Content-Type": "application/json",
            ...(options.headers || {{}}),
            "X-Hausie-CSRF-Token": CSRF_TOKEN
          }}
        }});
        const payload = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          throw new Error(payload.error || `Request failed (${{response.status}})`);
        }}
        return payload;
      }}

      function showMessage(kind, text) {{
        messageBox.className = `message show ${{kind}}`;
        messageBox.textContent = text;
      }}

      document.getElementById("credentialsForm").addEventListener("submit", async (event) => {{
        event.preventDefault();
        const haToken = document.getElementById("haToken").value.trim();
        const supportPassword = document.getElementById("supportPassword").value;
        const adminPassword = document.getElementById("adminPassword").value;
        try {{
          saveBtn.disabled = true;
          const payload = await request("/credentials", {{
            method: "POST",
            body: JSON.stringify({{
              ha_token: haToken,
              support_password: supportPassword,
              admin_password: adminPassword
            }})
          }});
          renderStatus(payload.status || initialStatus);
          showMessage("success", "Credentials saved.");
          if (!(payload.status || {{}}).setup_required) {{
            setTimeout(() => {{
              window.location.href = "/config-dashboard/main";
            }}, 700);
          }}
        }} catch (error) {{
          showMessage("error", error.message);
        }} finally {{
          saveBtn.disabled = false;
        }}
      }});

      renderStatus(initialStatus);
    </script>
  </body>
</html>"""


class _AddonHandler(BaseHTTPRequestHandler):
    def _send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            get_logger("addon").info("Client disconnected before JSON response was delivered.")

    def _send_text(self, code: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            get_logger("addon").info("Client disconnected before text response was delivered.")

    def _authorize(self) -> bool:
        expected = _resolve_notify_api_key()
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
        if not token:
            token = self.headers.get("X-API-Key", "").strip()
        return token == expected

    def _ingress_path(self) -> str:
        return _normalize_ingress_path(self.headers.get("X-Ingress-Path", ""))

    def _is_trusted_ingress(self) -> bool:
        client_ip = self.client_address[0] if self.client_address else ""
        return _is_trusted_ingress_request(client_ip, self.headers)

    def _authorize_ingress_mutation(self) -> bool:
        return self._is_trusted_ingress() and _has_valid_ui_csrf_token(self.headers)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        log = get_logger("addon")
        log.info(f"HTTP POST {path}")
        if path in _INGRESS_MUTATION_PATHS and not self._authorize_ingress_mutation():
            log.warn(f"Rejected unauthorized Ingress mutation: {path}")
            self._send_json(403, {"error": "Authenticated Home Assistant Ingress access is required."})
            return
        if path == "/pairing/start":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            duration = payload.get("duration") if isinstance(payload, dict) else None
            try:
                response = _start_pairing_session(int(duration or 90))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, response)
            return

        if path == "/pairing/stop":
            self._send_json(200, _stop_pairing_session())
            return

        if path == "/pairing/confirm":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            try:
                result = _confirm_pairing_device(payload if isinstance(payload, dict) else {})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        if path == "/credentials":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            try:
                status = _save_ha_credentials(payload if isinstance(payload, dict) else {})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "status": status})
            return

        if path == "/setup/initialize":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            try:
                _save_ha_credentials(payload)
                pairing_code = str(payload.get("pairing_code") or "").strip()
                device_id, device_token = resolve_device_credentials()
                if pairing_code:
                    # A supplied code is an explicit installer request to pair or re-pair
                    # this Home Assistant instance, even if older local credentials exist.
                    _register_with_pairing_code(pairing_code)
                elif not device_id or not device_token:
                    raise ValueError("Hausie pairing code is required.")
                _set_setup_progress("initializing", "Preparing Hausie initialization...")
                _start_background_workflow("initialize_hausie", _run_initialize_hausie)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                _set_setup_progress("failed", str(exc))
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(202, {"ok": True, "message": "Hausie initialization started.", "status": _setup_status_payload()})
            return

        if path == "/new_device":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            device_id = str(data.get("device_id") or "").strip()
            if not device_id:
                self._send_json(400, {"error": "device_id is required"})
                return
            entity_id = str(data.get("entity_id") or "").strip()
            name = None
            if not entity_id:
                name, entity_id = _fetch_device_info(device_id)
            else:
                name, _ = _fetch_device_info(device_id)
            if not name:
                name = device_id
            if not entity_id:
                self._send_json(500, {"error": "entity_id could not be resolved"})
                return
            try:
                dashboard_path = resolve_config_dashboard_path()
                _sync_config_dashboard_from_pi(dashboard_path)
                updated = upsert_new_device_button(device_id, entity_id, name)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            synced = False
            if updated:
                try:
                    dashboard_path = resolve_config_dashboard_path()
                    _sync_config_dashboard_to_pi(dashboard_path)
                    synced = True
                except Exception as exc:
                    self._send_json(500, {"error": f"sync failed: {exc}"})
                    return
            try:
                ha = _resolve_ha_client()
                if ha:
                    ha.set_input_boolean("input_boolean.new_device_found", "on")
            except Exception as exc:
                self._send_json(500, {"error": f"new_device_found update failed: {exc}"})
                return
            self._send_json(
                200,
                {"ok": True, "device_id": device_id, "updated": updated, "synced": synced},
            )
            return

        if path == "/new_devices_scan":
            try:
                total, updated = _scan_unlabeled_devices()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "total": total, "updated": updated})
            return

        if path == "/new_device_save":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            ha = _resolve_ha_client()
            if not ha:
                self._send_json(500, {"error": "HA_TOKEN is required"})
                return
            inputs = _read_new_device_inputs(ha)
            payload_device_id = str(payload.get("device_id") or "").strip()
            payload_name = str(payload.get("name") or "").strip()
            payload_label = str(payload.get("label") or "").strip()
            payload_labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
            payload_area = str(payload.get("area") or "").strip()
            if payload_device_id:
                inputs["device_id"] = payload_device_id
            if payload_name:
                inputs["name"] = payload_name
            if payload_label:
                inputs["label"] = payload_label
            if payload_area:
                inputs["area"] = payload_area
            requested_labels = []
            for item in payload_labels:
                label_name = str(item or "").strip()
                if label_name:
                    requested_labels.append(label_name)
            if not requested_labels and inputs.get("label"):
                requested_labels = [inputs["label"]]

            device_id = inputs.get("device_id") or ""
            if not device_id:
                self._send_json(400, {"error": "device_id is required"})
                return

            try:
                resolved_device_id = _update_device_name_area(
                    ha_ws_url=ha.ha_url_ws,
                    token=ha.token,
                    device_id=device_id,
                    name=inputs.get("name", ""),
                    area_name=inputs.get("area", ""),
                )
            except Exception as exc:
                self._send_json(500, {"error": f"device update failed: {exc}"})
                return

            applied_labels: list[str] = []
            if requested_labels:
                try:
                    base_url = ha.ha_url_rest.rsplit("/api", 1)[0]
                    applied_labels = _update_device_labels(
                        base_url=base_url,
                        username=resolve_ha_runtime_credentials()[1] or "",
                        password=resolve_ha_runtime_credentials()[2] or "",
                        device_id=resolved_device_id,
                        labels=requested_labels,
                    )
                except Exception as exc:
                    self._send_json(500, {"error": f"label update failed: {exc}"})
                    return

            try:
                _run_sync_inventory()
            except Exception as exc:
                self._send_json(500, {"error": f"sync_inventory failed: {exc}"})
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "device_id": device_id,
                    "label_updated": bool(applied_labels),
                    "applied_labels": applied_labels,
                },
            )
            return

        if path == "/help_messages":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            view_key = str(payload.get("view") or "").strip()
            views = payload.get("views") or payload.get("messages") or payload.get("pool") or {}
            if view_key:
                views = {view_key: payload.get("messages") or []}
            if not isinstance(views, dict):
                self._send_json(400, {"error": "views must be a mapping"})
                return
            replace = bool(payload.get("replace"))
            manager = HelpMessageManager(path=_resolve_help_messages_path())
            data = manager.update_views(views, replace=replace)
            try:
                ha = _resolve_ha_client()
                if ha:
                    updated = manager.rotate(list(views.keys()) if views else None)
                    _apply_help_messages(ha, updated)
            except Exception as exc:
                log.warn(f"help_messages apply failed: {exc}")
            self._send_json(200, {"ok": True, "data": data})
            return

        if path == "/help_messages/rotate":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            views = payload.get("views")
            if views is not None and not isinstance(views, list):
                self._send_json(400, {"error": "views must be a list"})
                return
            manager = HelpMessageManager(path=_resolve_help_messages_path())
            updated = manager.rotate(views if isinstance(views, list) else None)
            try:
                ha = _resolve_ha_client()
                if ha:
                    _apply_help_messages(ha, updated)
            except Exception as exc:
                self._send_json(500, {"error": f"apply failed: {exc}"})
                return
            self._send_json(200, {"ok": True, "messages": updated})
            return

        if path == "/cleanup/base":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            with log.script("cleanup_base"):
                result = _cleanup_base_assets()
                ha = _resolve_ha_client()
                if ha:
                    try:
                        _reload_services(ha, log)
                    except Exception:
                        pass
                self._send_json(200, {"ok": True, **result})
            return

        if path == "/cleanup/hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            with log.script("cleanup_hausie"):
                result = _cleanup_hausie_assets()
                ha = _resolve_ha_client()
                if ha:
                    try:
                        _reload_services(ha, log)
                    except Exception:
                        pass
                self._send_json(200, {"ok": True, **result})
            return

        if path == "/run/create_base":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            _start_background_workflow("create_base", _run_create_base)
            self._send_json(202, {"ok": True, "started": True})
            return

        if path == "/run/rebuild_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            _start_background_workflow("rebuild_hausie", _run_rebuild_hausie)
            self._send_json(202, {"ok": True, "started": True})
            return

        if path == "/run/restart_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            _start_background_workflow("restart_hausie", _run_restart_hausie)
            self._send_json(202, {"ok": True, "started": True})
            return

        if path == "/run/create_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            _start_background_workflow("create_hausie", _run_sync_inventory)
            self._send_json(202, {"ok": True, "started": True})
            return

        if path == "/run/sync_inventory":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            if _WORKFLOW_LOCK.locked():
                self._send_json(409, {"error": "Another Hausie action is already in progress."})
                return
            _start_background_workflow("sync_inventory", _run_sync_inventory)
            self._send_json(202, {"ok": True, "started": True})
            return

        if path == "/run/create_test":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            try:
                _run_create_test()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/notify":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            title = str(payload.get("title") or "").strip()
            message = str(payload.get("message") or "").strip()
            if not title or not message:
                self._send_json(400, {"error": "title and message are required"})
                return

            ha = _resolve_ha_client()
            if not ha:
                self._send_json(500, {"error": "HA_TOKEN is required"})
                return

            service = payload.get("service")
            targets = payload.get("targets")
            data = payload.get("data")
            persistent = bool(payload.get("persistent"))
            notification_id = payload.get("notification_id")

            notifier = NotificationManager(ha_client=ha, default_notify_service=os.getenv("HA_DEFAULT_NOTIFY"))
            try:
                notifier.send(
                    title=title,
                    message=message,
                    service=service,
                    targets=targets if isinstance(targets, list) else None,
                    data=data if isinstance(data, dict) else None,
                    persistent=persistent,
                    notification_id=str(notification_id) if notification_id else None,
                )
            except Exception as exc:
                log.error(f"notify failed: {exc}")
                self._send_json(500, {"error": f"notify failed: {exc}"})
                return

            self._send_json(200, {"ok": True})
            return

        if path == "/notify_admins":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Invalid JSON"})
                return

            title = str(payload.get("title") or "").strip()
            message = str(payload.get("message") or "").strip()
            if not title or not message:
                self._send_json(400, {"error": "title and message are required"})
                return

            ha = _resolve_ha_client()
            if not ha:
                self._send_json(500, {"error": "HA_TOKEN is required"})
                return

            data = payload.get("data")
            notifier = NotificationManager(ha_client=ha, default_notify_service=os.getenv("HA_DEFAULT_NOTIFY"))
            try:
                for service in _resolve_admin_notify_services():
                    notifier.send(
                        title=title,
                        message=message,
                        service=service,
                        data=data if isinstance(data, dict) else None,
                    )
            except Exception as exc:
                log.error(f"notify_admins failed: {exc}")
                self._send_json(500, {"error": f"notify_admins failed: {exc}"})
                return

            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "Not found"})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in _INGRESS_UI_PATHS and not self._is_trusted_ingress():
            get_logger("addon").warn(f"Rejected direct access to Ingress UI: {path or '/'}")
            self._send_json(403, {"error": "Open this page through Home Assistant Ingress."})
            return
        if path in {"", "/"}:
            status = _setup_status_payload()
            page = _render_portal_html() if status["initialized"] and status["credentials_valid"] else _render_setup_html(self._ingress_path())
            self._send_text(200, page, "text/html; charset=utf-8")
            return
        if path == "/setup":
            status = _setup_status_payload()
            page = _render_portal_html() if status["initialized"] and status["credentials_valid"] else _render_setup_html(self._ingress_path())
            self._send_text(200, page, "text/html; charset=utf-8")
            return
        if path == "/pairing":
            self._send_text(200, _render_pairing_html(self._ingress_path()), "text/html; charset=utf-8")
            return
        if path == "/credentials":
            status = _ha_credentials_status_payload()
            page = _render_portal_html() if status["credentials_valid"] else _render_credentials_html(self._ingress_path())
            self._send_text(200, page, "text/html; charset=utf-8")
            return
        if path == "/credentials/status":
            self._send_json(200, _ha_credentials_status_payload())
            return
        if path == "/setup/status":
            self._send_json(200, _setup_status_payload())
            return
        if path == "/pairing/status":
            self._send_json(200, _pairing_state_payload())
            return
        if path == "/logs":
            log_path = "/data/hausie_addon.log"
            try:
                text = Path(log_path).read_text(encoding="utf-8")
                lines = text.splitlines()[-300:]
                body = "\n".join(lines)
            except Exception:
                body = "No logs yet."
            page = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta http-equiv="refresh" content="5"/>
    <title>Hausie Add-on Logs</title>
    <style>
      body {{ font-family: monospace; background:#0f1117; color:#e6e6e6; padding:16px; }}
      pre {{ white-space: pre-wrap; }}
    </style>
  </head>
  <body>
    <h2>Hausie Add-on Logs (auto-refresh 5s)</h2>
    <pre>{html.escape(body)}</pre>
  </body>
</html>"""
            self._send_text(200, page, "text/html; charset=utf-8")
            return
        if path == "/help_messages":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            manager = HelpMessageManager(path=_resolve_help_messages_path())
            self._send_json(200, {"ok": True, "data": manager.get_pool()})
            return
        self._send_json(404, {"error": "Not found"})


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    log = get_logger("addon")
    global _ACTIVE_SERVER, _SHUTDOWN_REQUESTED
    log.start("Addon starting.")
    migrate_ha_runtime_credentials_from_env()
    _validate_ha_credentials(log)
    server = HTTPServer((host, port), _AddonHandler)
    _ACTIVE_SERVER = server
    _SHUTDOWN_REQUESTED = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        global _SHUTDOWN_REQUESTED
        if _SHUTDOWN_REQUESTED:
            return
        _SHUTDOWN_REQUESTED = True
        signame = signal.Signals(signum).name
        log.warn(f"Addon stopping ({signame}).")
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.start(f"Listening on http://{host}:{port}")
    _auto_register_from_pairing_code()
    _start_mqtt_listener()
    _start_remote_support_manager()
    _start_heartbeat()
    _start_license_monitor()
    _start_inventory_monitor()
    _sync_local_config()
    try:
        _resync_voice_exposure_from_state(log)
    except Exception as exc:
        log.warn(f"Voice exposure startup sync failed: {exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.warn("Addon stopping (KeyboardInterrupt).")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        _LICENSE_MONITOR_STOP.set()
        _INVENTORY_MONITOR_STOP.set()
        try:
            server.server_close()
        except Exception:
            pass
        _ACTIVE_SERVER = None
        log.ok("Addon stopped.")


if __name__ == "__main__":
    run()
