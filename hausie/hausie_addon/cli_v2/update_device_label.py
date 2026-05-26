import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ..core.flow_logger import get_logger
from ..orchestration.device_label_updater import DeviceLabelUpdater


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update a device label via Playwright.")
    parser.add_argument("--device-id", required=True, help="Device id or entity id to update.")
    parser.add_argument("--label", required=True, help="Label name to apply.")
    parser.add_argument("--base-url", help="Home Assistant base URL (e.g. http://homeassistant:8123).")
    parser.add_argument("--username", help="HA UI username (defaults to HA_UI_USERNAME).")
    parser.add_argument("--password", help="HA UI password (defaults to HA_UI_PASSWORD).")
    parser.add_argument("--headed", action="store_true", help="Run Playwright with UI visible.")
    return parser.parse_args()


def _resolve_base_url(arg_value: str | None) -> str:
    if arg_value:
        return arg_value.rstrip("/")
    env_base = os.getenv("HA_BASE_URL")
    if env_base:
        return env_base.rstrip("/")
    rest_url = os.getenv("HA_REST_URL", "http://homeassistant:8123/api").rstrip("/")
    if rest_url.endswith("/api"):
        base = rest_url[:-4]
    else:
        base = rest_url
    if base.startswith("http://homeassistant:8123"):
        return "http://homeassistant.local:8123"
    if base.startswith("https://homeassistant:8123"):
        return "https://homeassistant.local:8123"
    return base


def main() -> None:
    log = get_logger("new_device")
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    args = _parse_args()
    base_url = _resolve_base_url(args.base_url)
    username = args.username or os.getenv("HA_UI_USERNAME", "")
    password = args.password or os.getenv("HA_UI_PASSWORD", "")
    updater = DeviceLabelUpdater(
        base_url=base_url,
        username=username,
        password=password,
        headless=not args.headed,
    )
    try:
        updated = updater.update_device_label(args.device_id, args.label)
    finally:
        updater.close()

    if not updated:
        log.warn("No update performed.")
        sys.exit(1)
    log.ok("Label updated.")


if __name__ == "__main__":
    main()
