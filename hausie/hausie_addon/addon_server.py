from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import yaml
import websocket
from dotenv import load_dotenv

from .core.clients.ha_client import HAClient
from .core.cloud_client import CloudClient
from .core.flow_logger import get_logger
from .core.inventory.process_inventory import InventoryProcessor
from .core.io.pi_file_sender import PiFileSender
from .core.managers.notification_manager import NotificationManager
from .core.mqtt_listener import MQTTNotificationListener
from .core.heartbeat import HeartbeatReporter
from .core.remote_support import RemoteSupportManager, _load_public_keys
from .core.managers.config_manager import ConfigManager
from .core.managers.help_message_manager import HelpMessageManager
from .core.utils.naming import slugify
from .core.device_state import (
    resolve_device_credentials,
    persist_device_credentials,
    resolve_state_path,
    load_device_state,
    save_device_state,
)
from .orchestration.device_label_updater import DeviceLabelUpdater
from .orchestration.dashboard_updater import DashboardUpdater
from .orchestration.new_device_dashboard import (
    resolve_config_dashboard_path,
    upsert_new_device_button,
)
from .settings import Settings

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _load_addon_options() -> None:
    option_keys = {
        "ha_token",
        "hausie_cloud_url",
        "pairing_code",
        "log_file",
        "log_to_stdout",
        "log_clear_on_start",
        "log_max_bytes",
    }
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
    mappings = {
        "ha_token": "HA_TOKEN",
        "hausie_cloud_url": "HAUSIE_CLOUD_URL",
        "pairing_code": "HAUSIE_PAIRING_CODE",
        "log_file": "HAUSIE_LOG_FILE",
        "log_to_stdout": "HAUSIE_LOG_TO_STDOUT",
        "log_clear_on_start": "TEST_LOG_CLEAR_ON_START",
        "log_max_bytes": "HAUSIE_LOG_MAX_BYTES",
    }
    for option_key, env_key in mappings.items():
        if option_key in data:
            os.environ[env_key] = str(data.get(option_key) or "").strip()
    if options_path and not os.getenv("HAUSIE_DEVICE_STATE_PATH"):
        os.environ["HAUSIE_DEVICE_STATE_PATH"] = str(options_path.parent / "hausie_device.json")


_load_addon_options()


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
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        return None
    try:
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        )
        data = cloud.request_subscription_status()
    except Exception as exc:
        get_logger("addon").warn(f"Subscription status check failed: {exc}")
        return None
    plan = data.get("tier") or data.get("plan")
    plan_text = str(plan).strip() if plan else ""
    return plan_text or None


def _update_rebuild_state(state: dict, *, plan: str | None, version: str | None) -> None:
    if plan:
        state["last_plan"] = plan
    if version:
        state["last_addon_version"] = version
    state["last_rebuild_at"] = int(time.time())
    save_device_state(state)


_REBUILD_PLAN_STEPS = {"create_base", "create_hausie"}


def _normalize_rebuild_steps(raw_steps: Any) -> list[str]:
    if not isinstance(raw_steps, list):
        return []
    steps: list[str] = []
    for raw in raw_steps:
        step = str(raw or "").strip()
        if step in _REBUILD_PLAN_STEPS and step not in steps:
            steps.append(step)
    return steps


def _resolve_local_rebuild_steps(
    log: Any,
    *,
    state: dict,
    settings: Settings,
    current_version: str,
) -> tuple[list[str], str | None]:
    last_plan = str(state.get("last_plan") or "").strip().lower()
    last_version = str(state.get("last_addon_version") or "").strip()
    current_plan = _resolve_subscription_plan(settings)
    plan_changed = False
    version_changed = False

    if current_plan:
        plan_changed = current_plan.strip().lower() != last_plan if last_plan else True
    elif last_plan:
        log.warn("Plan check unavailable; defaulting to create_base.")
        plan_changed = True

    if current_version:
        version_changed = current_version != last_version if last_version else True
    elif last_version:
        log.warn("Add-on version unavailable; defaulting to create_base.")
        version_changed = True

    if plan_changed or version_changed:
        log.start("Detected plan/add-on change; running create_base + create_hausie.")
        return ["create_base", "create_hausie"], current_plan

    log.start("No plan/add-on changes; running create_hausie only.")
    return ["create_hausie"], current_plan


def _resolve_remote_rebuild_steps(
    log: Any,
    *,
    state: dict,
    settings: Settings,
    current_version: str,
) -> tuple[list[str], str | None]:
    if not settings.HAUSIE_CLOUD_URL or not settings.HAUSIE_CLOUD_TOKEN:
        return [], None

    payload = {
        "trigger": "rebuild_hausie",
        "current_addon_version": current_version,
        "last_plan": state.get("last_plan"),
        "last_addon_version": state.get("last_addon_version"),
    }
    if settings.HAUSIE_DEVICE_ID:
        payload["device_id"] = settings.HAUSIE_DEVICE_ID

    try:
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        )
        data = cloud.request_rebuild_plan(payload)
    except Exception as exc:
        log.warn(f"Remote rebuild plan unavailable; using local fallback: {exc}")
        return [], None

    if not isinstance(data, dict):
        log.warn("Remote rebuild plan response invalid; using local fallback.")
        return [], None

    steps = _normalize_rebuild_steps(data.get("execution_plan") or data.get("steps"))
    if not steps:
        log.warn("Remote rebuild plan missing execution_plan; using local fallback.")
        return [], None

    reason = str(data.get("reason") or "").strip()
    if reason:
        log.start(f"Using cloud rebuild plan ({reason}): {', '.join(steps)}.")
    else:
        log.start(f"Using cloud rebuild plan: {', '.join(steps)}.")

    plan = data.get("tier") or data.get("plan")
    plan_text = str(plan).strip() if plan else ""
    return steps, (plan_text or None)


def _execute_rebuild_steps(steps: list[str]) -> None:
    for step in steps:
        if step == "create_base":
            _run_create_base()
        elif step == "create_hausie":
            _run_create_hausie()


def _resolve_mqtt_enabled() -> bool:
    return os.getenv("HAUSIE_MQTT_ENABLE", "").strip().lower() in {"1", "true", "yes"}


def _resolve_mqtt_secret(name: str) -> str | None:
    return os.getenv(name) or _read_secret_file(os.getenv(f"{name}_FILE"))


_MQTT_LISTENER: MQTTNotificationListener | None = None
_SUPPORT_MANAGER: RemoteSupportManager | None = None
_HEARTBEAT: HeartbeatReporter | None = None
_HEARTBEAT_ACTION_LOCK = threading.Lock()
_HEARTBEAT_ACTION_RUNNING = False

_BASE_AUTOMATION_IDS = {
    "new_device_created",
    "new_device_saved",
    "ui_help_rotate_messages",
    "new_devices_scan_daily",
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

def _resolve_local_ha_root() -> Path | None:
    env_root = os.getenv("HAUSIE_LOCAL_HA_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    local_root = ROOT_DIR / "hausie" / "homeassistant"
    return local_root if local_root.exists() else None


def _mirror_local_artifact(local_root: Path | None, rel_path: str, content: str, log) -> None:
    if not local_root or not rel_path:
        return
    try:
        local_path = (local_root / rel_path).resolve()
        local_path.relative_to(local_root)
    except Exception:
        return
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(str(content), encoding="utf-8")
        log.ok(f"Mirrored {rel_path} to local repo.")
    except Exception as exc:
        log.warn(f"Local mirror failed for {rel_path}: {exc}")


def _remove_local_artifact(local_root: Path | None, rel_path: str, log) -> None:
    if not local_root or not rel_path:
        return
    try:
        local_path = (local_root / rel_path).resolve()
        local_path.relative_to(local_root)
    except Exception:
        return
    try:
        if local_path.is_dir():
            shutil.rmtree(local_path, ignore_errors=True)
        elif local_path.exists():
            local_path.unlink()
        log.ok(f"Removed local mirror {rel_path}.")
    except Exception as exc:
        log.warn(f"Local mirror delete failed for {rel_path}: {exc}")


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
    auth_keys_path = os.getenv("HAUSIE_SUPPORT_AUTH_KEYS_PATH", "/config/ssh/authorized_keys")
    timeout_min = int(os.getenv("HAUSIE_SUPPORT_TIMEOUT_MINUTES", "15"))
    poll_s = int(os.getenv("HAUSIE_SUPPORT_POLL_SECONDS", "10"))
    public_keys = _load_public_keys()
    _device_id, token = resolve_device_credentials()
    base = os.getenv("HAUSIE_CLOUD_URL", "").strip().rstrip("/")
    support_keys_url = os.getenv("HAUSIE_SUPPORT_KEYS_URL", "").strip()
    if not support_keys_url and base:
        support_keys_url = f"{base}/api/device/support-keys"
    manage_ssh = os.getenv("HAUSIE_SUPPORT_MANAGE_SSH", "true").strip().lower() not in {"0", "false", "no"}
    ssh_slug = os.getenv("SSH_ADDON_SLUG", "a0d7b954_ssh").strip()
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


def _auto_register_from_pairing_code() -> None:
    pairing_code = os.getenv("HAUSIE_PAIRING_CODE", "").strip()
    if not pairing_code:
        return
    log = get_logger("register")
    device_id, token = resolve_device_credentials()
    base_url = os.getenv("HAUSIE_CLOUD_URL", "").strip()
    if not base_url:
        log.warn("Pairing skipped: HAUSIE_CLOUD_URL not set.")
        return
    if device_id and token:
        try:
            cloud = CloudClient(base_url=base_url, token=token, timeout_s=20)
            if cloud.has_valid_device_credentials():
                log.ok(f"Pairing skipped: device already registered ({device_id}).")
                return
            log.warn(f"Stored device credentials are no longer valid for {device_id}; relinking with pairing code.")
        except Exception as exc:
            log.warn(f"Pairing validation failed: {exc}")
            return
        state_path = resolve_state_path()
        if state_path.exists():
            try:
                state_path.unlink()
            except Exception:
                pass
        os.environ.pop("HAUSIE_DEVICE_ID", None)
        os.environ.pop("HAUSIE_CLOUD_TOKEN", None)
    log.start("Registering add-on with pairing code.")
    try:
        cloud = CloudClient(base_url=base_url, timeout_s=20)
        response = cloud.register_device({"pairing_code": pairing_code})
    except Exception as exc:
        log.warn(f"Pairing failed: {exc}")
        return
    device_id = str(response.get("hausie_device_id") or "").strip()
    token = str(response.get("device_token") or "").strip()
    if not device_id or not token:
        log.warn("Pairing failed: missing device credentials in response.")
        return
    persist_device_credentials(device_id, token)
    os.environ["HAUSIE_DEVICE_ID"] = device_id
    os.environ["HAUSIE_CLOUD_TOKEN"] = token
    log.ok(f"Pairing completed: {device_id}")


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
    base = os.getenv("HAUSIE_CLOUD_URL", "").strip().rstrip("/")
    endpoint = os.getenv("HAUSIE_HEARTBEAT_URL", "").strip()
    if not endpoint:
        if not base:
            get_logger("heartbeat").warn("Heartbeat disabled: HAUSIE_CLOUD_URL not set.")
            return
        endpoint = f"{base}/api/device/heartbeat"
    interval_s = _resolve_heartbeat_interval()
    _HEARTBEAT = HeartbeatReporter(
        ha_client=ha,
        endpoint_url=endpoint,
        device_id=device_id,
        token=token,
        interval_s=interval_s,
        state_path=os.getenv("HAUSIE_SUPPORT_STATE_PATH", "/data/hausie_support_state.json"),
        on_actions=_handle_heartbeat_actions,
    )
    _HEARTBEAT.start()


def _sync_local_config() -> None:
    if os.getenv("HAUSIE_SYNC_CONFIG_ON_START", "true").strip().lower() in {"0", "false", "no"}:
        return
    config_path = os.getenv("PI_CONFIG_PATH", "/config/configuration.yaml").strip()
    if not config_path:
        return
    try:
        manager = ConfigManager(
            pi_sender=None,
            config_path=config_path,
            require_remote=False,
        )
        manager.sync_config_dashboard()
        get_logger("config").ok("configuration.yaml synced (local).")
    except Exception as exc:
        get_logger("config").warn(f"configuration.yaml sync failed: {exc}")


def _resolve_ha_client() -> HAClient | None:
    token = os.getenv("HA_TOKEN") or _read_secret_file(os.getenv("HA_TOKEN_FILE"))
    if not token:
        return None
    ha_ws_url = os.getenv("HA_WS_URL", "ws://homeassistant:8123/api/websocket")
    ha_rest_url = os.getenv("HA_REST_URL", "http://homeassistant:8123/api")
    return HAClient(ha_url_ws=ha_ws_url, ha_url_rest=ha_rest_url, token=token)


def _resolve_heartbeat_interval() -> int:
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


def _handle_heartbeat_actions(actions: list[Any], payload: dict[str, Any] | None = None) -> None:
    if not actions:
        return
    global _HEARTBEAT_ACTION_RUNNING
    global _HEARTBEAT
    with _HEARTBEAT_ACTION_LOCK:
        if _HEARTBEAT_ACTION_RUNNING:
            get_logger("heartbeat").warn("Heartbeat actions skipped: already running.")
            return
        _HEARTBEAT_ACTION_RUNNING = True
    log = get_logger("heartbeat")
    try:
        normalized: list[Any] = []
        for action in actions:
            if isinstance(action, dict):
                normalized.append(action)
                continue
            value = str(action).strip()
            if value:
                normalized.append(value)
        if not normalized:
            return
        action_names = [
            str(action.get("type") or action.get("action") or "dict")
            if isinstance(action, dict)
            else str(action)
            for action in normalized
        ]
        log.start(f"Heartbeat actions received: {', '.join(action_names)}")
        lower_actions = [
            str(action).strip().lower()
            for action in normalized
            if not isinstance(action, dict)
        ]
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
                _cleanup_base_assets()
                _cleanup_hausie_assets()
                _run_create_base()
                _run_create_hausie()
            return
        for action in normalized:
            if isinstance(action, dict):
                action_type = str(action.get("type") or action.get("action") or "").strip().lower()
                if action_type == "create_ha_support_user":
                    _create_ha_support_user(action)
                    continue
                if action_type == "delete_ha_support_user":
                    _delete_ha_support_user(action)
                    continue
                log.warn(f"Unknown heartbeat action: {action_type or action}")
                continue
            action = str(action).strip().lower()
            if action in {"cleanup_base", "cleanup_base_assets"}:
                with log.script("cleanup_base"):
                    _cleanup_base_assets()
                continue
            if action in {"cleanup_hausie", "cleanup_hausie_assets"}:
                with log.script("cleanup_hausie"):
                    _cleanup_hausie_assets()
                continue
            if action == "create_base":
                _run_create_base()
                continue
            if action == "create_hausie":
                _run_create_hausie()
                continue
            if action == "rebuild_hausie":
                _run_rebuild_hausie()
                continue
            if action == "restart_hausie":
                _run_restart_hausie()
                continue
            log.warn(f"Unknown heartbeat action: {action}")
    finally:
        with _HEARTBEAT_ACTION_LOCK:
            _HEARTBEAT_ACTION_RUNNING = False


def _resolve_pi_dashboard_dir() -> str:
    root = os.getenv("PI_HA_CONFIG_DIR", "/config")
    for suffix in ("/helpers", "/scripts", "/groups", "/automations", "/dashboards"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    return os.getenv("PI_DASHBOARD_DIR") or f"{root}/dashboards"


def _use_remote_sender() -> bool:
    if os.getenv("HAUSIE_LOCAL_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("SUPERVISOR_TOKEN") and os.getenv("HAUSIE_FORCE_SSH", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    return True


def _sync_config_dashboard_to_pi(local_path: Path) -> None:
    if not _use_remote_sender():
        # HAOS local mode: ensure dashboard is under /config/dashboards
        target = Path(os.getenv("PI_DASHBOARD_DIR", "/config/dashboards")) / _CONFIG_DASHBOARD_FILENAME
        if local_path.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            if local_path.exists():
                target.write_text(local_path.read_text(encoding="utf-8"), encoding="utf-8")
        return
    pi_host = os.getenv("PI_HOST")
    pi_user = os.getenv("PI_USER")
    if not pi_host or not pi_user:
        return
    pi_port = int(os.getenv("PI_PORT", "22"))
    pi_key = os.getenv("PI_SSH_KEY") or None
    pi_scp_legacy = os.getenv("PI_SCP_LEGACY", "").strip().lower() in {"1", "true", "yes"}
    sender = PiFileSender(
        host=pi_host,
        user=pi_user,
        port=pi_port,
        key_path=pi_key,
        use_scp_legacy=pi_scp_legacy,
    )
    remote_path = f"{_resolve_pi_dashboard_dir().rstrip('/')}/{_CONFIG_DASHBOARD_FILENAME}"
    sender.send_file(local_path, remote_path)

def _sync_config_dashboard_from_pi(local_path: Path) -> None:
    if not _use_remote_sender():
        return
    pi_host = os.getenv("PI_HOST")
    pi_user = os.getenv("PI_USER")
    if not pi_host or not pi_user:
        return
    pi_port = int(os.getenv("PI_PORT", "22"))
    pi_key = os.getenv("PI_SSH_KEY") or None
    pi_scp_legacy = os.getenv("PI_SCP_LEGACY", "").strip().lower() in {"1", "true", "yes"}
    sender = PiFileSender(
        host=pi_host,
        user=pi_user,
        port=pi_port,
        key_path=pi_key,
        use_scp_legacy=pi_scp_legacy,
    )
    remote_path = f"{_resolve_pi_dashboard_dir().rstrip('/')}/{_CONFIG_DASHBOARD_FILENAME}"
    try:
        text = sender.read_remote_text(remote_path)
    except Exception:
        return
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(text, encoding="utf-8")


def _ha_config_root() -> Path:
    return Path(os.getenv("PI_HA_CONFIG_DIR", "/config")).resolve()


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

    config_path = os.getenv("PI_CONFIG_PATH", "/config/configuration.yaml").strip()
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


def _cleanup_hausie_assets() -> dict[str, object]:
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

    config_dash = root / "dashboards" / _CONFIG_DASHBOARD_FILENAME
    if _filter_config_views(
        config_dash,
        lambda view: (view.get("path") == "main") or (view.get("title") == "Main"),
    ):
        updated.append(str(config_dash))

    config_path = os.getenv("PI_CONFIG_PATH", "/config/configuration.yaml").strip()
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

    ui_cleared = False
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


def _reload_services(ha: HAClient, log) -> None:
    services = [
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
    ]
    for domain, service in services:
        try:
            ha.call_service(domain, service, {})
            log.ok(f"Reloaded {domain}.{service}.")
        except Exception as exc:
            log.warn(f"Reload {domain}.{service} failed: {exc}")


def _apply_cloud_artifacts(
    sender: PiFileSender | None,
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
        if sender:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp.write(str(content))
                tmp_path = tmp.name
            try:
                sender.send_file(tmp_path, remote_path)
                log.ok(f"Updated {remote_path}.")
                applied[remote_path] = str(content)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            path = Path(remote_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
            log.ok(f"Updated {remote_path}.")
            applied[remote_path] = str(content)
        _mirror_local_artifact(local_root, rel_path, str(content), log)

    for rel_path in deletes or []:
        if not rel_path:
            continue
        remote_path = rel_path if rel_path.startswith("/") else f"{root}/{rel_path}" if root else rel_path
        if sender:
            try:
                sender.remove_remote(remote_path)
                log.ok(f"Deleted {remote_path}.")
            except Exception as exc:
                log.warn(f"Failed to delete {remote_path}: {exc}")
        else:
            try:
                path = Path(remote_path)
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
                log.ok(f"Deleted {remote_path}.")
            except Exception as exc:
                log.warn(f"Failed to delete {remote_path}: {exc}")
        _remove_local_artifact(local_root, rel_path, log)
    return applied


def _run_create_hausie() -> None:
    log = get_logger("addon")
    with log.script("create_hausie"):
        settings = Settings()
        if not settings.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar assets en cloud.")
        ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
        inv = InventoryProcessor()
        sender = None
        inv = InventoryProcessor()
        if settings.PI_HOST and settings.PI_USER:
            sender = PiFileSender(
                host=settings.PI_HOST,
                user=settings.PI_USER,
                port=settings.PI_PORT,
                key_path=settings.PI_SSH_KEY,
                use_scp_legacy=settings.PI_SCP_LEGACY,
            )
        else:
            log.warn("PI_HOST/PI_USER no definidos; usando modo local.")
        ha.fetch_all(include_users=True)
        inv.process()
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        inventory = json.loads(Path(inv.inventory_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()
        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
        payload = {
            "inventory": inventory,
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
        }
        if device_id:
            payload["device_id"] = device_id
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        )
        response = cloud.request_create_hausie(payload)
        applied = _apply_cloud_artifacts(
            sender,
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
        _apply_plan_badge(ha, response.get("plan_badge") if isinstance(response, dict) else None)
        enabled = _turn_on_user_helpers(ha)
        if enabled:
            log.ok(f"User helpers enabled: {enabled}.")


def _run_rebuild_hausie() -> None:
    log = get_logger("addon")
    with log.script("rebuild_hausie"):
        settings = Settings()
        state = load_device_state()
        current_version = _resolve_addon_version()
        rebuild_steps, current_plan = _resolve_remote_rebuild_steps(
            log,
            state=state,
            settings=settings,
            current_version=current_version,
        )
        if not rebuild_steps:
            rebuild_steps, current_plan = _resolve_local_rebuild_steps(
                log,
                state=state,
                settings=settings,
                current_version=current_version,
            )

        _execute_rebuild_steps(rebuild_steps)
        _update_rebuild_state(state, plan=current_plan, version=current_version)


def _run_create_base() -> None:
    log = get_logger("addon")
    with log.script("create_base"):
        settings = Settings()
        if not settings.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar assets en cloud.")
        ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
        sender = None
        if settings.PI_HOST and settings.PI_USER:
            sender = PiFileSender(
                host=settings.PI_HOST,
                user=settings.PI_USER,
                port=settings.PI_PORT,
                key_path=settings.PI_SSH_KEY,
                use_scp_legacy=settings.PI_SCP_LEGACY,
            )
        else:
            log.warn("PI_HOST/PI_USER no definidos; usando modo local.")
        log.start("Fetching Home Assistant snapshot.")
        ha.fetch_all(include_users=True)
        inv = InventoryProcessor()
        inv.process()
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        inventory = json.loads(Path(inv.inventory_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()
        force_full = False
        try:
            states = ha.get_states()
            force_full = not any(
                isinstance(state, dict) and state.get("entity_id") == "input_text.hausie_plan_text"
                for state in states or []
            )
        except Exception:
            force_full = False
        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
        payload = {
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
            "inventory": inventory,
            "force_full": force_full,
        }
        if device_id:
            payload["device_id"] = device_id
        log.start("Requesting base assets from cloud.")
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        )
        response = cloud.request_base_assets(payload)
        log.start("Applying cloud artifacts to Home Assistant config.")
        _apply_cloud_artifacts(
            sender,
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
                pi_sender=sender,
                config_path=settings.PI_CONFIG_PATH,
                require_remote=bool(sender),
            )
            config.sync_config_dashboard()
        log.start("Reloading Home Assistant services.")
        _reload_services(ha, log)
        _apply_plan_badge(ha, response.get("plan_badge") if isinstance(response, dict) else None)
        enabled = _turn_on_user_helpers(ha)
        if enabled:
            log.ok(f"User helpers enabled: {enabled}.")


def _run_restart_hausie() -> None:
    log = get_logger("addon")
    with log.script("restart_hausie"):
        _cleanup_base_assets()
        _cleanup_hausie_assets()
        _run_create_base()
        _run_create_hausie()
        settings = Settings()
        state = load_device_state()
        _update_rebuild_state(
            state,
            plan=_resolve_subscription_plan(settings),
            version=_resolve_addon_version(),
        )


def _run_create_test() -> None:
    log = get_logger("addon")
    with log.script("create_test"):
        settings = Settings()
        if not settings.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar test assets en cloud.")
        ha = HAClient(ha_url_ws=settings.HA_WS_URL, ha_url_rest=settings.HA_REST_URL, token=settings.HA_TOKEN)
        sender = None
        if settings.PI_HOST and settings.PI_USER:
            sender = PiFileSender(
                host=settings.PI_HOST,
                user=settings.PI_USER,
                port=settings.PI_PORT,
                key_path=settings.PI_SSH_KEY,
                use_scp_legacy=settings.PI_SCP_LEGACY,
            )
        else:
            log.warn("PI_HOST/PI_USER no definidos; usando modo local.")
        log.start("Fetching Home Assistant snapshot.")
        ha.fetch_all(include_users=True)
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()
        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or settings.HAUSIE_DEVICE_ID
        payload = {
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
        }
        if device_id:
            payload["device_id"] = device_id
        log.start("Requesting test assets from cloud.")
        cloud = CloudClient(
            base_url=settings.HAUSIE_CLOUD_URL,
            token=settings.HAUSIE_CLOUD_TOKEN,
            timeout_s=settings.HAUSIE_CLOUD_TIMEOUT,
        )
        response = cloud.request_test_assets(payload)
        log.start("Applying cloud artifacts to Home Assistant config.")
        _apply_cloud_artifacts(
            sender,
            remote_root=settings.PI_HA_CONFIG_DIR,
            artifacts=response.get("artifacts") if isinstance(response, dict) else None,
            deletes=response.get("deletes") if isinstance(response, dict) else None,
            log=log,
        )
        if settings.PI_CONFIG_PATH:
            log.start("Updating configuration.yaml.")
            config = ConfigManager(
                pi_sender=sender,
                config_path=settings.PI_CONFIG_PATH,
                require_remote=bool(sender),
            )
            config.sync_config_dashboard()
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


def _ensure_remote_support_helper(root: Path) -> None:
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
        return
    doc["allow_remote_support"] = {
        "name": "Remote Support",
        "initial": "off",
    }
    helper_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


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


class _AddonHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, code: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        log = get_logger("addon")
        log.info(f"HTTP POST {path}")
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
            payload_area = str(payload.get("area") or "").strip()
            if payload_device_id:
                inputs["device_id"] = payload_device_id
            if payload_name:
                inputs["name"] = payload_name
            if payload_label:
                inputs["label"] = payload_label
            if payload_area:
                inputs["area"] = payload_area

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

            label_updated = False
            if inputs.get("label"):
                try:
                    base_url = ha.ha_url_rest.rsplit("/api", 1)[0]
                    headless_flag = os.getenv("HA_PLAYWRIGHT_HEADLESS", "").strip().lower()
                    headless = headless_flag not in {"0", "false", "no"}
                    label_updater = DeviceLabelUpdater(
                        base_url=base_url,
                        username=os.getenv("HA_UI_USERNAME", ""),
                        password=os.getenv("HA_UI_PASSWORD", ""),
                        headless=headless,
                    )
                    try:
                        label_updated = label_updater.update_device_label(resolved_device_id, inputs["label"])
                    finally:
                        label_updater.close()
                except Exception as exc:
                    self._send_json(500, {"error": f"label update failed: {exc}"})
                    return

            try:
                _run_create_hausie()
            except Exception as exc:
                self._send_json(500, {"error": f"create_hausie failed: {exc}"})
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "device_id": device_id,
                    "label_updated": label_updated,
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
            try:
                _run_create_base()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/run/rebuild_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            try:
                _run_rebuild_hausie()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/run/restart_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            try:
                _run_restart_hausie()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/run/create_hausie":
            if not self._authorize():
                self._send_json(401, {"error": "Unauthorized"})
                return
            try:
                _run_create_hausie()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
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
        path = self.path.rstrip("/")
        if path in {"", "/"}:
            log_path = os.getenv("HAUSIE_LOG_FILE", "/data/hausie_addon.log")
            try:
                text = Path(log_path).read_text(encoding="utf-8")
                lines = text.splitlines()[-300:]
                body = "\n".join(lines)
            except Exception:
                body = "No logs yet."
            html = f"""<!doctype html>
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
    <pre>{body}</pre>
  </body>
</html>"""
            self._send_text(200, html, "text/html; charset=utf-8")
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
    log.start("Addon starting.")
    server = HTTPServer((host, port), _AddonHandler)
    log.start(f"Listening on http://{host}:{port}")
    _auto_register_from_pairing_code()
    _start_mqtt_listener()
    _start_remote_support_manager()
    _start_heartbeat()
    _sync_local_config()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.warn("Addon stopping (KeyboardInterrupt).")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        log.ok("Addon stopped.")


if __name__ == "__main__":
    run()
