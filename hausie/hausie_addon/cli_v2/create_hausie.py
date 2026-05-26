import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..settings import Settings
from ..core.clients.ha_client import HAClient
from ..core.cloud_client import CloudClient
from ..core.flow_logger import get_logger
from ..core.inventory.process_inventory import InventoryProcessor
from ..core.io.pi_file_sender import PiFileSender
from ..orchestration.dashboard_updater import DashboardUpdater


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the create_hausie workflow.")
    return parser.parse_args()


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
    return applied


def main() -> None:
    log = get_logger("core")

    _parse_args()
    with log.script("create_hausie"):
        s = Settings()

        if not s.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar assets en cloud.")

        ha = HAClient(
            ha_url_ws=s.HA_WS_URL,
            ha_url_rest=s.HA_REST_URL,
            token=s.HA_TOKEN,
        )
        inv = InventoryProcessor()

        sender = None
        if s.PI_HOST and s.PI_USER:
            sender = PiFileSender(
                host=s.PI_HOST,
                user=s.PI_USER,
                port=s.PI_PORT,
                key_path=s.PI_SSH_KEY,
                use_scp_legacy=s.PI_SCP_LEGACY,
            )
        else:
            log.warn("PI_HOST/PI_USER no definidos; usando modo local.")

        log.start("Fetching Home Assistant snapshot.")
        ha.fetch_all(include_users=True)
        inv.process()
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        inventory = json.loads(Path(inv.inventory_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()

        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or s.HAUSIE_DEVICE_ID
        payload = {
            "inventory": inventory,
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
        }
        if device_id:
            payload["device_id"] = device_id

        log.start("Requesting create_hausie assets from cloud.")
        cloud = CloudClient(
            base_url=s.HAUSIE_CLOUD_URL,
            token=s.HAUSIE_CLOUD_TOKEN,
            timeout_s=s.HAUSIE_CLOUD_TIMEOUT,
        )
        response = cloud.request_create_hausie(payload)

        log.start("Applying cloud artifacts to Home Assistant config.")
        applied = _apply_cloud_artifacts(
            sender,
            remote_root=s.PI_HA_CONFIG_DIR,
            artifacts=response.get("artifacts") if isinstance(response, dict) else None,
            deletes=response.get("deletes") if isinstance(response, dict) else None,
            log=log,
        )

        update_ui = os.getenv("SKIP_UI_DASHBOARD", "").strip().lower() not in {"1", "true", "yes"}
        ui_payload = response.get("ui") if isinstance(response, dict) else None
        if not isinstance(ui_payload, dict):
            log.warn("UI update skipped: cloud response missing 'ui' payload.")
        elif not update_ui:
            log.warn("UI update skipped: SKIP_UI_DASHBOARD enabled.")
        else:
            dashboard_yaml = ui_payload.get("main_dashboard_yaml")
            dashboard_path = ui_payload.get("dashboard_path") or "dashboard-hausie/0"
            if not dashboard_yaml:
                dash_file = ui_payload.get("main_dashboard_file")
                if dash_file:
                    resolved = dash_file if dash_file.startswith("/") else f"{s.PI_HA_CONFIG_DIR.rstrip('/')}/{dash_file}"
                    dashboard_yaml = applied.get(resolved)
                    if not dashboard_yaml:
                        log.warn(f"UI update skipped: dashboard YAML not found in applied artifacts ({resolved}).")
            if not s.HA_UI_USERNAME or not s.HA_UI_PASSWORD:
                log.warn("UI update skipped: HA_UI_USERNAME/HA_UI_PASSWORD not set.")
            elif not dashboard_yaml:
                log.warn("UI update skipped: dashboard YAML content missing.")
            else:
                log.start("Updating dashboard via UI.")
                autom = DashboardUpdater(
                    base_url=s.HA_REST_URL.rsplit("/api", 1)[0],
                    username=s.HA_UI_USERNAME,
                    password=s.HA_UI_PASSWORD,
                    headless=False,
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

        log.start("Reloading Home Assistant services.")
        _reload_services(ha, log)
        try:
            states = ha.get_states()
            user_entities = [
                state.get("entity_id")
                for state in states
                if isinstance(state, dict)
                and isinstance(state.get("entity_id"), str)
                and state["entity_id"].startswith("input_boolean.perm_")
            ]
            user_entities = [ent for ent in user_entities if ent]
            if user_entities:
                ha.call_service("input_boolean", "turn_on", {"entity_id": user_entities})
                log.ok(f"User helpers enabled: {len(user_entities)}.")
        except Exception:
            pass
        log.ok("Create_hausie workflow complete.")


if __name__ == "__main__":
    main()
