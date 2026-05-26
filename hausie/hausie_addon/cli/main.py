import os
import requests

from ..settings import Settings
from ..core.flow_logger import get_logger
from ..orchestration.device_create_watcher import DeviceCreateWatcher


def _addon_base_url() -> str:
    return os.getenv("HAUSIE_ADDON_URL", "http://localhost:8000").rstrip("/")


def _notify_addon_new_device(device: dict) -> None:
    device_id = (device.get("id") or device.get("device_id") or "").strip()
    if not device_id:
        return
    url = f"{_addon_base_url()}/new_device"
    requests.post(url, json={"device_id": device_id}, timeout=10)


def run():
    """Start the device watcher loop."""
    s = Settings()
    log = get_logger("new_device")

    watcher = DeviceCreateWatcher(
        ha_url_ws=s.HA_WS_URL,
        token=s.HA_TOKEN,
        on_device_create=_notify_addon_new_device,
        ha_url_rest=s.HA_REST_URL,
    )

    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        log.warn("Stopping.")


if __name__ == "__main__":
    run()
