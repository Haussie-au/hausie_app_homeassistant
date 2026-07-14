import os
from pathlib import Path

from .core.device_state import resolve_device_credentials, resolve_ha_runtime_credentials

HOME_ASSISTANT_CONFIG_DIR = "/homeassistant"
DEFAULT_HAUSIE_CLOUD_URL = "https://api.hausiehome.com"


def _read_secret_file(path: str | None) -> str | None:
    """Read a secret value from a file path if present."""
    if not path:
        return None
    p = Path(path)
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


class Settings:
    def __init__(self):
        """Load Home Assistant settings from the environment."""
        default_ws = "ws://homeassistant:8123/api/websocket"
        default_rest = "http://homeassistant:8123/api"
        self.HA_WS_URL = os.getenv("HA_WS_URL", default_ws)
        self.HA_REST_URL = os.getenv("HA_REST_URL", default_rest)
        self.HA_TOKEN, self.HA_UI_USERNAME, self.HA_UI_PASSWORD = resolve_ha_runtime_credentials()
        if not self.HA_TOKEN:
            raise RuntimeError("Falta HA_TOKEN (o HA_TOKEN_FILE).")
        self.PLAYWRIGHT_STORAGE_STATE = os.getenv("PLAYWRIGHT_STORAGE_STATE")
        self.HAUSIE_CLOUD_URL = os.getenv("HAUSIE_CLOUD_URL", "").strip() or DEFAULT_HAUSIE_CLOUD_URL
        self.HAUSIE_CLOUD_TOKEN = os.getenv("HAUSIE_CLOUD_TOKEN") or _read_secret_file(
            os.getenv("HAUSIE_CLOUD_TOKEN_FILE")
        )
        device_id, token = resolve_device_credentials()
        if not self.HAUSIE_CLOUD_TOKEN and token:
            self.HAUSIE_CLOUD_TOKEN = token
        self.HAUSIE_DEVICE_ID = device_id
        self.HAUSIE_CLOUD_TIMEOUT = int(os.getenv("HAUSIE_CLOUD_TIMEOUT", "20"))
        self.HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT = int(
            os.getenv(
                "HAUSIE_CLOUD_CREATE_HAUSIE_TIMEOUT",
                str(max(self.HAUSIE_CLOUD_TIMEOUT, 90)),
            )
        )
        self.PI_HOST = os.getenv("PI_HOST")
        self.PI_USER = os.getenv("PI_USER")
        self.PI_PORT = int(os.getenv("PI_PORT", "22"))
        self.PI_SSH_KEY = os.getenv("PI_SSH_KEY")
        self.PI_SCP_LEGACY = os.getenv("PI_SCP_LEGACY", "").strip().lower() in {"1", "true", "yes"}
        self.PI_HA_CONFIG_DIR = os.getenv("PI_HA_CONFIG_DIR", HOME_ASSISTANT_CONFIG_DIR)
        for suffix in ("/helpers", "/scripts", "/groups", "/automations", "/dashboards"):
            if self.PI_HA_CONFIG_DIR.endswith(suffix):
                self.PI_HA_CONFIG_DIR = self.PI_HA_CONFIG_DIR[: -len(suffix)]
        self.PI_DASHBOARD_DIR = os.getenv("PI_DASHBOARD_DIR") or f"{self.PI_HA_CONFIG_DIR}/dashboards"
        self.PI_CONFIG_PATH = os.getenv("PI_CONFIG_PATH") or f"{self.PI_HA_CONFIG_DIR}/configuration.yaml"

        self.LOCAL_MODE = os.getenv("HAUSIE_LOCAL_MODE", "").strip().lower() in {"1", "true", "yes"}
        self.FORCE_REMOTE = os.getenv("HAUSIE_FORCE_SSH", "").strip().lower() in {"1", "true", "yes"}
        if (self.LOCAL_MODE or os.getenv("SUPERVISOR_TOKEN")) and not self.FORCE_REMOTE:
            # In HAOS add-on mode default to local file writes (no SSH/SCP).
            self.PI_HOST = None
            self.PI_USER = None
            self.PI_PORT = 22
            self.PI_SSH_KEY = None
            self.PI_SCP_LEGACY = False
