from __future__ import annotations

from pathlib import Path
from typing import Iterable
import json
import tempfile
import yaml

from .pi_file_sender import PiFileSender
from ..utils.naming import build_filename, prefix_filename, slugify
from ..flow_logger import get_logger

PKG_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = PKG_DIR.parent

log = get_logger("core")


class HomeAssistantYamlManager:
    """Manage deletion of Home Assistant YAML artifacts."""

    def __init__(
        self,
        homeassistant_root: Path | None = None,
        *,
        pi_sender: PiFileSender | None = None,
        remote_root: str | None = None,
        backup_suffix: str = ".bak",
        require_remote: bool = True,
        allow_helper_delete: bool = False,
    ) -> None:
        self.homeassistant_root = (homeassistant_root or (ROOT_DIR / "hausie" / "homeassistant")).resolve()
        self.automations_dir = (self.homeassistant_root / "automations").resolve()
        self.covers_dir = (self.homeassistant_root / "covers").resolve()
        self.groups_dir = (self.homeassistant_root / "groups").resolve()
        self.scripts_dir = (self.homeassistant_root / "scripts").resolve()
        self.helpers_dir = (self.homeassistant_root / "helpers").resolve()
        self.switches_dir = (self.homeassistant_root / "switches").resolve()
        self.dashboards_dir = (self.homeassistant_root / "dashboards").resolve()
        self.data_dir = (self.homeassistant_root / "data").resolve()
        self.pi_sender = pi_sender
        self.remote_root = remote_root
        self.backup_suffix = backup_suffix
        self.require_remote = require_remote
        self.allow_helper_delete = allow_helper_delete

    def _ensure_remote(self) -> None:
        if self.require_remote and (not self.pi_sender or not self.remote_root):
            raise RuntimeError("PI sender and remote_root are required for YAML deletions.")

    def _remote_path(self, *parts: str) -> str:
        root = (self.remote_root or "").rstrip("/")
        clean = [p.strip("/") for p in parts if p]
        return "/".join([root] + clean) if root else "/".join(clean)

    def _read_remote_text(self, remote_path: str) -> str | None:
        if not self.pi_sender:
            return None
        try:
            return self.pi_sender.read_remote_text(remote_path)
        except Exception:
            return None

    def _sync_remote_to_local(self, local_path: Path, remote_path: str) -> str | None:
        text = self._read_remote_text(remote_path)
        if text is None:
            return None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(text, encoding="utf-8")
        return text

    def _backup_remote_text(self, remote_path: str, text: str | None) -> None:
        if not self.pi_sender or text is None:
            return
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        self.pi_sender.send_file(tmp_path, f"{remote_path}{self.backup_suffix}")
        Path(tmp_path).unlink(missing_ok=True)

    def _send_local(self, local_path: Path, remote_path: str) -> None:
        if not self.pi_sender:
            return
        if local_path.exists():
            self.pi_sender.send_file(local_path, remote_path)
        else:
            self.pi_sender.remove_remote(remote_path)

    def _load_yaml_mapping(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return None
        return data

    def _write_yaml_mapping(self, path: Path, data: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def _delete_key_from_yaml(self, path: Path, key: str, section_key: str | None = None) -> bool:
        data = self._load_yaml_mapping(path)
        if data is None:
            log.warn(f"YAML not found or invalid: {path}")
            return False

        section = None
        if section_key and isinstance(data.get(section_key), dict):
            section = data[section_key]
        elif isinstance(data, dict):
            section = data

        if not isinstance(section, dict):
            log.warn(f"Invalid YAML mapping in: {path}")
            return False

        if key not in section:
            log.warn(f"Key not found in {path}: {key}")
            return False

        section.pop(key, None)
        if section_key and isinstance(data.get(section_key), dict):
            if section:
                data[section_key] = section
            else:
                data.pop(section_key, None)
        elif section is not data:
            data = section

        if data:
            self._write_yaml_mapping(path, data)
        else:
            path.unlink(missing_ok=True)
        return True

    def delete_helper(self, area: str, input_type: str, helper_id: str | None = None) -> bool:
        """Delete a helper entry (or its whole file) from helpers."""
        if not self.allow_helper_delete:
            log.skip("Helpers are not deleted by design.")
            return False
        area_slug = slugify(area)
        if not area_slug:
            log.warn("Invalid area for helper deletion.")
            return False

        self._ensure_remote()
        filename = build_filename(input_type, area_slug)
        dest_file = (self.helpers_dir / input_type / filename).resolve()
        remote_path = self._remote_path("helpers", input_type, filename)
        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            log.warn(f"Helper file not found: {dest_file}")
            return False
        self._backup_remote_text(remote_path, original)

        if not helper_id:
            dest_file.unlink(missing_ok=True)
            self._send_local(dest_file, remote_path)
            return True

        ok = self._delete_key_from_yaml(dest_file, helper_id, section_key=input_type)
        if ok:
            self._send_local(dest_file, remote_path)
        return ok

    def delete_script(self, script_id: str, area: str | None = None, filename: str | None = None) -> bool:
        """Delete a script entry from a scripts YAML file."""
        if not script_id:
            log.warn("Script id is required.")
            return False

        self._ensure_remote()
        if filename:
            prefixed = prefix_filename(filename)
            dest_file = (self.scripts_dir / prefixed).resolve()
            remote_path = self._remote_path("scripts", prefixed)
        else:
            safe_area = slugify(area) if area else "general"
            if not safe_area:
                safe_area = "general"
            file_name = prefix_filename(f"{safe_area}_scripts.yaml")
            dest_file = (self.scripts_dir / file_name).resolve()
            remote_path = self._remote_path("scripts", file_name)

        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            log.warn(f"Script file not found: {dest_file}")
            return False
        self._backup_remote_text(remote_path, original)

        ok = self._delete_key_from_yaml(dest_file, script_id)
        if ok:
            self._send_local(dest_file, remote_path)
        return ok

    def delete_group(self, group_id: str, area: str | None = None, filename: str | None = None) -> bool:
        """Delete a group entry from a groups YAML file."""
        if not group_id:
            log.warn("Group id is required.")
            return False

        self._ensure_remote()
        if filename:
            prefixed = prefix_filename(filename)
            dest_file = (self.groups_dir / prefixed).resolve()
            remote_path = self._remote_path("groups", prefixed)
        elif area:
            filename = build_filename("group", area)
            dest_file = (self.groups_dir / filename).resolve()
            remote_path = self._remote_path("groups", filename)
        else:
            filename = f"{group_id}.yaml"
            dest_file = (self.groups_dir / filename).resolve()
            remote_path = self._remote_path("groups", filename)

        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            log.warn(f"Group file not found: {dest_file}")
            return False
        self._backup_remote_text(remote_path, original)

        ok = self._delete_key_from_yaml(dest_file, group_id)
        if ok:
            self._send_local(dest_file, remote_path)
        return ok

    def delete_cover_file(self, area: str | None = None, filename: str | None = None) -> bool:
        """Delete a grouped cover YAML file."""
        self._ensure_remote()
        if filename:
            prefixed = prefix_filename(filename)
        elif area:
            prefixed = build_filename("cover", area)
        else:
            log.warn("Area or filename is required for cover deletion.")
            return False
        dest_file = (self.covers_dir / prefixed).resolve()
        remote_path = self._remote_path("covers", prefixed)

        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            return False
        self._backup_remote_text(remote_path, original)
        dest_file.unlink(missing_ok=True)
        self._send_local(dest_file, remote_path)
        return True

    def delete_automation(self, automation_id: str | None = None, filename: str | None = None) -> bool:
        """Delete an automation YAML file."""
        self._ensure_remote()
        if filename:
            prefixed = prefix_filename(filename)
            dest_file = (self.automations_dir / prefixed).resolve()
            remote_path = self._remote_path("automations", prefixed)
        elif automation_id:
            filename = build_filename("automation", automation_id)
            dest_file = (self.automations_dir / filename).resolve()
            remote_path = self._remote_path("automations", filename)
        else:
            log.warn("Automation id or filename is required.")
            return False

        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            log.warn(f"Automation file not found: {dest_file}")
            return False
        self._backup_remote_text(remote_path, original)
        dest_file.unlink(missing_ok=True)
        self._send_local(dest_file, remote_path)
        return True

    def delete_dashboard(self, filename: str) -> bool:
        """Delete a dashboard YAML file."""
        if not filename:
            log.warn("Dashboard filename is required.")
            return False
        self._ensure_remote()
        prefixed = prefix_filename(filename)
        dest_file = (self.dashboards_dir / prefixed).resolve()
        remote_path = self._remote_path("dashboards", prefixed)
        original = self._sync_remote_to_local(dest_file, remote_path)
        if original is None and not dest_file.exists():
            log.warn(f"Dashboard file not found: {dest_file}")
            return False
        self._backup_remote_text(remote_path, original)
        dest_file.unlink(missing_ok=True)
        self._send_local(dest_file, remote_path)
        return True

    def delete_all_generated_files(self, include_data: bool = False, include_helpers: bool = False) -> list[Path]:
        """
        Delete all files under automations, groups, scripts, switches, dashboards.

        Set include_data=True to also remove files under homeassistant/data.
        Set include_helpers=True to also remove helper files (not recommended).
        """
        self._ensure_remote()
        deleted: list[Path] = []
        targets: Iterable[Path] = [
            self.automations_dir,
            self.covers_dir,
            self.groups_dir,
            self.scripts_dir,
            self.switches_dir,
            self.dashboards_dir,
        ]
        if include_helpers:
            targets = list(targets) + [self.helpers_dir]
        if include_data:
            targets = list(targets) + [self.data_dir]

        for folder in targets:
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
                    deleted.append(path)
        if self.pi_sender and self.remote_root:
            for folder in targets:
                rel = folder.relative_to(self.homeassistant_root)
                remote_folder = self._remote_path(rel.as_posix())
                self.pi_sender.remove_remote(remote_folder)
        return deleted

    def _empty_content_for_path(self, path: Path) -> str:
        """Return minimal valid content for a file we want to clear."""
        if path.suffix.lower() == ".json":
            if path.name in {"registry.json", "inventory.json"}:
                return json.dumps({"areas": []}, indent=2) + "\n"
            return "{}\n"
        return ""

    def clear_all_generated_files(self, include_data: bool = False, include_helpers: bool = False) -> list[Path]:
        """
        Truncate files under automations, groups, scripts, switches, dashboards (and optionally data/helpers).

        Files are kept (emptied), not removed.
        """
        self._ensure_remote()
        cleared: list[Path] = []
        targets: Iterable[Path] = [
            self.automations_dir,
            self.covers_dir,
            self.groups_dir,
            self.scripts_dir,
            self.switches_dir,
            self.dashboards_dir,
        ]
        if include_helpers:
            targets = list(targets) + [self.helpers_dir]
        if include_data:
            targets = list(targets) + [self.data_dir]

        for folder in targets:
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                path.write_text(self._empty_content_for_path(path), encoding="utf-8")
                cleared.append(path)

        if self.pi_sender and self.remote_root:
            for folder in targets:
                if not folder.exists():
                    continue
                rel = folder.relative_to(self.homeassistant_root)
                remote_folder = self._remote_path(rel.as_posix())
                self.pi_sender.send_dir(folder, remote_folder)

        return cleared
