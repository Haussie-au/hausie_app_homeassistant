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
from ..core.io.pi_file_sender import PiFileSender
from ..core.managers.config_manager import ConfigManager


def _resolve_local_ha_root() -> Path | None:
    env_root = os.getenv("HAUSIE_LOCAL_HA_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    local_root = Path(__file__).resolve().parents[2] / "hausie" / "homeassistant"
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create test Hausie assets.")
    return parser.parse_args()


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


def main() -> None:
    log = get_logger("core")

    _parse_args()
    with log.script("create_test"):
        s = Settings()

        if not s.HAUSIE_CLOUD_URL:
            raise RuntimeError("HAUSIE_CLOUD_URL es requerido para generar test assets en cloud.")

        ha = HAClient(
            ha_url_ws=s.HA_WS_URL,
            ha_url_rest=s.HA_REST_URL,
            token=s.HA_TOKEN,
        )

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
        raw = json.loads(Path(ha.raw_file).read_text(encoding="utf-8"))
        labels = ha.fetch_labels()

        device_id = os.getenv("HAUSIE_DEVICE_ID", "").strip() or s.HAUSIE_DEVICE_ID
        payload = {
            "areas": raw.get("areas", []),
            "users": raw.get("users", []),
            "labels": labels,
        }
        if device_id:
            payload["device_id"] = device_id

        log.start("Requesting test assets from cloud.")
        cloud = CloudClient(
            base_url=s.HAUSIE_CLOUD_URL,
            token=s.HAUSIE_CLOUD_TOKEN,
            timeout_s=s.HAUSIE_CLOUD_TIMEOUT,
        )
        response = cloud.request_test_assets(payload)

        log.start("Applying cloud artifacts to Home Assistant config.")
        _apply_cloud_artifacts(
            sender,
            remote_root=s.PI_HA_CONFIG_DIR,
            artifacts=response.get("artifacts") if isinstance(response, dict) else None,
            deletes=response.get("deletes") if isinstance(response, dict) else None,
            log=log,
        )

        if s.PI_CONFIG_PATH:
            log.start("Updating configuration.yaml.")
            config = ConfigManager(
                pi_sender=sender,
                config_path=s.PI_CONFIG_PATH,
                require_remote=bool(sender),
            )
            config.sync_config_dashboard()

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
        log.ok("Create_test workflow complete.")


if __name__ == "__main__":
    main()
