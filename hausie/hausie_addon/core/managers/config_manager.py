from __future__ import annotations

import os

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

import yaml

from ..io.pi_file_sender import PiFileSender


@dataclass
class _TaggedValue:
    tag: str
    value: object


class _TaggedLoader(yaml.SafeLoader):
    pass


class _TaggedDumper(yaml.SafeDumper):
    pass


def _construct_tagged(loader, tag_suffix, node):
    tag = f"!{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return _TaggedValue(tag, value)


def _represent_tagged(dumper, data):
    node = dumper.represent_data(data.value)
    node.tag = data.tag
    return node


_TaggedLoader.add_multi_constructor("!", _construct_tagged)
_TaggedDumper.add_representer(_TaggedValue, _represent_tagged)


def _load_yaml_with_tags(text: str) -> dict:
    return yaml.load(text, Loader=_TaggedLoader) if text.strip() else {}


def _dump_yaml_with_tags(doc: dict) -> str:
    return yaml.dump(doc, Dumper=_TaggedDumper, sort_keys=False, width=4096)


class ConfigManager:
    """Manage Home Assistant configuration.yaml updates."""

    _HAUSIE_REST_COMMANDS = {
        "new_device_create",
        "new_device_save",
        "new_devices_scan",
        "notify_admins",
        "ui_help_rotate",
        "cleanup_base",
        "cleanup_hausie",
        "create_base",
        "create_hausie",
        "sync_inventory",
        "rebuild_hausie",
        "restart_hausie",
        "create_test",
        "test_popup_wait",
        "hausie_new_device",
        "hausie_new_device_save",
    }
    _HAUSIE_SHELL_COMMANDS = {
        "hausie_update_new_device",
    }
    _HAUSIE_TEST_REST_COMMANDS = {
        "cleanup_base",
        "cleanup_hausie",
        "create_base",
        "create_hausie",
        "sync_inventory",
        "create_test",
        "test_popup_wait",
    }

    def __init__(
        self,
        *,
        pi_sender: PiFileSender,
        config_path: str,
        backup_suffix: str = ".bak",
        require_remote: bool = True,
        shell_commands: dict[str, str] | None = None,
    ) -> None:
        self.pi_sender = pi_sender
        self.config_path = config_path
        self.backup_suffix = backup_suffix
        self.require_remote = require_remote
        self.shell_commands = shell_commands or {}
        self.local_config_path = self._default_local_config_path()

    @staticmethod
    def _default_local_config_path() -> Path:
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "hausie" / "homeassistant" / "configuration.yaml"

    def _ensure_remote(self) -> None:
        if self.require_remote and (not self.pi_sender or not self.config_path):
            raise RuntimeError("PI sender and config_path are required.")

    @staticmethod
    def _ensure_config_dashboard(doc: dict, *, include_main_dashboard: bool = False) -> dict:
        if not isinstance(doc, dict):
            raise ValueError("configuration.yaml must be a mapping.")
        lovelace = doc.get("lovelace")
        if lovelace is None:
            lovelace = {}
        if not isinstance(lovelace, dict):
            raise ValueError("lovelace section must be a mapping.")
        dashboards = lovelace.get("dashboards")
        if dashboards is None:
            dashboards = {}
        if not isinstance(dashboards, dict):
            raise ValueError("lovelace.dashboards must be a mapping.")

        dashboards.pop("config", None)
        dashboards.pop("hausie-dashboard", None)
        # Retire the old development dashboard instead of exposing it in homes.
        dashboards.pop("test-dashboard", None)
        dashboards["config-dashboard"] = {
            "mode": "yaml",
            "title": "Configuration",
            "icon": "mdi:cog",
            "show_in_sidebar": True,
            "filename": "dashboards/hausie_configuration_dashboard.yaml",
        }
        if include_main_dashboard:
            dashboards["hausie-dashboard"] = {
                "mode": "yaml",
                "title": "Hausie",
                "icon": "mdi:home-assistant",
                "show_in_sidebar": True,
                "filename": "dashboards/hausie_dashboard.yaml",
            }
        lovelace["dashboards"] = dashboards
        doc["lovelace"] = lovelace
        if "input_text" not in doc:
            doc["input_text"] = _TaggedValue("!include_dir_merge_named", "helpers/input_text")
        if "input_button" not in doc:
            doc["input_button"] = _TaggedValue("!include_dir_merge_named", "helpers/input_button")
        if "input_boolean" not in doc:
            doc["input_boolean"] = _TaggedValue("!include_dir_merge_named", "helpers/input_boolean")
        if "input_number" not in doc:
            doc["input_number"] = _TaggedValue("!include_dir_merge_named", "helpers/input_number")
        if "input_select" not in doc:
            doc["input_select"] = _TaggedValue("!include_dir_merge_named", "helpers/input_select")
        if "input_datetime" not in doc:
            doc["input_datetime"] = _TaggedValue("!include_dir_merge_named", "helpers/input_datetime")
        if "switch" in doc:
            current = doc.get("switch")
            if isinstance(current, _TaggedValue) and current.tag == "!include_dir_merge_list" and current.value == "switches":
                doc.pop("switch", None)
        if "template" not in doc:
            doc["template"] = _TaggedValue("!include_dir_merge_list", "switches")
        if not isinstance(doc.get("automation"), _TaggedValue) or doc["automation"].tag != "!include_dir_list" or doc["automation"].value != "automations":
            doc["automation"] = _TaggedValue("!include_dir_list", "automations")
        if not isinstance(doc.get("script"), _TaggedValue) or doc["script"].tag != "!include_dir_merge_named" or doc["script"].value != "scripts":
            doc["script"] = _TaggedValue("!include_dir_merge_named", "scripts")
        if not isinstance(doc.get("group"), _TaggedValue) or doc["group"].tag != "!include_dir_merge_named" or doc["group"].value != "groups":
            doc["group"] = _TaggedValue("!include_dir_merge_named", "groups")
        if not isinstance(doc.get("cover"), _TaggedValue) or doc["cover"].tag != "!include_dir_merge_list" or doc["cover"].value != "covers":
            doc["cover"] = _TaggedValue("!include_dir_merge_list", "covers")
        if "cloud" not in doc or doc.get("cloud") is None:
            doc["cloud"] = {}
        if "recorder" not in doc or doc.get("recorder") is None:
            doc["recorder"] = {}
        if "history" not in doc or doc.get("history") is None:
            doc["history"] = {}
        return doc

    def _ensure_shell_commands(self, doc: dict) -> dict:
        existing = doc.get("shell_command")
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            raise ValueError("shell_command section must be a mapping.")
        if not self.shell_commands:
            if "hausie_update_new_device" in existing:
                existing.pop("hausie_update_new_device", None)
                if existing:
                    doc["shell_command"] = existing
                else:
                    doc.pop("shell_command", None)
            return doc
        for key, cmd in self.shell_commands.items():
            if key and cmd:
                existing[key] = cmd
        doc["shell_command"] = existing
        return doc

    def _ensure_rest_commands(self, doc: dict) -> dict:
        existing = doc.get("rest_command")
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            raise ValueError("rest_command section must be a mapping.")

        addon_url = os.getenv("HAUSIE_ADDON_URL", "").strip()
        if not addon_url:
            addon_host = os.getenv("HAUSIE_ADDON_HOST", "").strip()
            if not addon_host:
                addon_host = (os.getenv("HOSTNAME") or os.getenv("HAUSIE_ADDON_SLUG") or "local_hausie").strip()
            if "_" in addon_host:
                addon_host = addon_host.replace("_", "-")
            addon_url = f"http://{addon_host}:8000"
        if addon_url:
            for legacy in ("hausie_new_device", "hausie_new_device_save"):
                existing.pop(legacy, None)
            url = f"{addon_url.rstrip('/')}/new_device"
            existing["new_device_create"] = {
                "url": url,
                "method": "POST",
                "content_type": "application/json",
                "payload": '{"device_id": "{{ device_id }}"}',
                "timeout": 180,
            }
            save_url = f"{addon_url.rstrip('/')}/new_device_save"
            existing["new_device_save"] = {
                "url": save_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            scan_url = f"{addon_url.rstrip('/')}/new_devices_scan"
            existing["new_devices_scan"] = {
                "url": scan_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            notify_admins_url = f"{addon_url.rstrip('/')}/notify_admins"
            existing["notify_admins"] = {
                "url": notify_admins_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": '{"title":"{{ title }}","message":"{{ message }}"}',
                "timeout": 180,
            }
            rotate_url = f"{addon_url.rstrip('/')}/help_messages/rotate"
            existing["ui_help_rotate"] = {
                "url": rotate_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            cleanup_base_url = f"{addon_url.rstrip('/')}/cleanup/base"
            existing["cleanup_base"] = {
                "url": cleanup_base_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            cleanup_hausie_url = f"{addon_url.rstrip('/')}/cleanup/hausie"
            existing["cleanup_hausie"] = {
                "url": cleanup_hausie_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            create_base_url = f"{addon_url.rstrip('/')}/run/create_base"
            existing["create_base"] = {
                "url": create_base_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            create_hausie_url = f"{addon_url.rstrip('/')}/run/create_hausie"
            existing["create_hausie"] = {
                "url": create_hausie_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            sync_inventory_url = f"{addon_url.rstrip('/')}/run/sync_inventory"
            existing["sync_inventory"] = {
                "url": sync_inventory_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
            rebuild_hausie_url = f"{addon_url.rstrip('/')}/run/rebuild_hausie"
            existing["rebuild_hausie"] = {
                "url": rebuild_hausie_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 300,
            }
            restart_hausie_url = f"{addon_url.rstrip('/')}/run/restart_hausie"
            existing["restart_hausie"] = {
                "url": restart_hausie_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 300,
            }
            create_test_url = f"{addon_url.rstrip('/')}/run/create_test"
            existing["create_test"] = {
                "url": create_test_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 300,
            }
        cloud_url = os.getenv("HAUSIE_CLOUD_URL", "").strip()
        if cloud_url:
            test_popup_url = f"{cloud_url.rstrip('/')}/api/TEST_POPUP/wait"
            existing["test_popup_wait"] = {
                "url": test_popup_url,
                "method": "POST",
                "content_type": "application/json",
                "payload": "{}",
                "timeout": 180,
            }
        if existing:
            doc["rest_command"] = existing
        else:
            doc.pop("rest_command", None)
        return doc

    def _resolve_config_root(self) -> Path:
        if self.config_path:
            try:
                return Path(self.config_path).resolve().parent
            except Exception:
                pass
        return self.local_config_path.parent

    def _storage_main_dashboard_exists(self) -> bool:
        storage_path = self._resolve_config_root() / ".storage" / "lovelace_dashboards"
        if not storage_path.exists():
            return False
        try:
            payload = json.loads(storage_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        items = ((payload or {}).get("data") or {}).get("items") or []
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() == "dashboard_hausie":
                return True
            if str(item.get("url_path") or "").strip() == "dashboard-hausie":
                return True
        return False


    def sync_config_dashboard(self) -> None:
        """Update configuration.yaml to include the config dashboard."""
        if self.require_remote:
            self._ensure_remote()
        config_text = ""
        if self.pi_sender and self.config_path:
            try:
                config_text = self.pi_sender.read_remote_text(self.config_path) or ""
            except Exception:
                config_text = ""
        elif self.config_path:
            path = Path(self.config_path)
            if path.exists():
                config_text = path.read_text(encoding="utf-8")

        config_doc = _load_yaml_with_tags(config_text)
        config_doc = self._ensure_config_dashboard(
            config_doc,
            include_main_dashboard=not self._storage_main_dashboard_exists(),
        )
        config_doc = self._ensure_shell_commands(config_doc)
        config_doc = self._ensure_rest_commands(config_doc)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(_dump_yaml_with_tags(config_doc))
            tmp_path = tmp.name

        rendered_text = _dump_yaml_with_tags(config_doc)
        if config_text and self.config_path:
            if self.pi_sender:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as backup:
                    backup.write(config_text)
                    backup_path = backup.name
                self.pi_sender.send_file(backup_path, f"{self.config_path}{self.backup_suffix}")
                Path(backup_path).unlink(missing_ok=True)
            else:
                Path(f"{self.config_path}{self.backup_suffix}").write_text(config_text, encoding="utf-8")

        if self.pi_sender and self.config_path:
            self.pi_sender.send_file(tmp_path, self.config_path)
        elif self.config_path:
            Path(self.config_path).write_text(rendered_text, encoding="utf-8")
        Path(tmp_path).unlink(missing_ok=True)

        local_path = self.local_config_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(rendered_text, encoding="utf-8")

    def remove_hausie_entries(
        self,
        *,
        remove_dashboards: bool = True,
        remove_rest_commands: bool = True,
        remove_includes: bool = True,
        remove_shell_commands: bool = True,
        keep_test_dashboard: bool = True,
        keep_test_assets: bool = True,
    ) -> bool:
        """Remove Hausie-related entries from configuration.yaml."""
        if self.require_remote:
            self._ensure_remote()
        config_text = ""
        if self.pi_sender and self.config_path:
            try:
                config_text = self.pi_sender.read_remote_text(self.config_path) or ""
            except Exception:
                config_text = ""
        elif self.config_path:
            path = Path(self.config_path)
            if path.exists():
                config_text = path.read_text(encoding="utf-8")

        config_doc = _load_yaml_with_tags(config_text)
        changed = False

        if remove_dashboards:
            lovelace = config_doc.get("lovelace")
            if isinstance(lovelace, dict):
                dashboards = lovelace.get("dashboards")
                if isinstance(dashboards, dict):
                    keys = ["config-dashboard", "hausie-dashboard"]
                    if not keep_test_dashboard:
                        keys.append("test-dashboard")
                    for key in keys:
                        if key in dashboards:
                            dashboards.pop(key, None)
                            changed = True
                    if not dashboards:
                        lovelace.pop("dashboards", None)
                        changed = True
                if not lovelace:
                    config_doc.pop("lovelace", None)
                    changed = True

        if remove_rest_commands:
            rest = config_doc.get("rest_command")
            if isinstance(rest, dict):
                for key in list(rest.keys()):
                    if keep_test_assets and key in self._HAUSIE_TEST_REST_COMMANDS:
                        continue
                    if key in self._HAUSIE_REST_COMMANDS:
                        rest.pop(key, None)
                        changed = True
                if not rest:
                    config_doc.pop("rest_command", None)
                    changed = True

        if remove_shell_commands:
            shell_cmd = config_doc.get("shell_command")
            if isinstance(shell_cmd, dict):
                for key in list(shell_cmd.keys()):
                    if key in self._HAUSIE_SHELL_COMMANDS:
                        shell_cmd.pop(key, None)
                        changed = True
                if not shell_cmd:
                    config_doc.pop("shell_command", None)
                    changed = True

        if remove_includes:
            include_map = {
                "input_text": ("!include_dir_merge_named", "helpers/input_text"),
                "input_button": ("!include_dir_merge_named", "helpers/input_button"),
                "input_boolean": ("!include_dir_merge_named", "helpers/input_boolean"),
                "input_number": ("!include_dir_merge_named", "helpers/input_number"),
                "input_select": ("!include_dir_merge_named", "helpers/input_select"),
                "input_datetime": ("!include_dir_merge_named", "helpers/input_datetime"),
                "automation": ("!include_dir_list", "automations"),
                "script": ("!include_dir_merge_named", "scripts"),
                "group": ("!include_dir_merge_named", "groups"),
                "cover": ("!include_dir_merge_list", "covers"),
                "template": ("!include_dir_merge_list", "switches"),
            }
            if keep_test_assets:
                include_map.pop("input_button", None)
                include_map.pop("automation", None)
            for key, (tag, value) in include_map.items():
                current = config_doc.get(key)
                if isinstance(current, _TaggedValue) and current.tag == tag and current.value == value:
                    config_doc.pop(key, None)
                    changed = True

        if not changed:
            return False

        rendered_text = _dump_yaml_with_tags(config_doc)
        if config_text and self.config_path:
            if self.pi_sender:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as backup:
                    backup.write(config_text)
                    backup_path = backup.name
                self.pi_sender.send_file(backup_path, f"{self.config_path}{self.backup_suffix}")
                Path(backup_path).unlink(missing_ok=True)
            else:
                Path(f"{self.config_path}{self.backup_suffix}").write_text(config_text, encoding="utf-8")

        if self.pi_sender and self.config_path:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp.write(rendered_text)
                tmp_path = tmp.name
            try:
                self.pi_sender.send_file(tmp_path, self.config_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        elif self.config_path:
            Path(self.config_path).write_text(rendered_text, encoding="utf-8")

        local_path = self.local_config_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(rendered_text, encoding="utf-8")
        return True
