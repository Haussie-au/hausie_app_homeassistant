from __future__ import annotations

from pathlib import Path
import tempfile
import yaml

from ..creators.dashboard_creator import DashboardCreator
from ..inventory.registry_manager import RegistryManager
from ..io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..io.pi_file_sender import PiFileSender


class DashboardManager:
    """CRUD operations for dashboards."""

    def __init__(
        self,
        dashboard_creator: DashboardCreator,
        *,
        pi_sender: PiFileSender | None = None,
        remote_root: str | None = None,
        yaml_manager: HomeAssistantYamlManager | None = None,
        backup_suffix: str = ".bak",
        require_remote: bool = True,
        extra_view_paths: list[str] | None = None,
    ) -> None:
        self.dashboard_creator = dashboard_creator
        self.pi_sender = pi_sender
        self.remote_root = remote_root
        self.yaml_manager = yaml_manager
        self.backup_suffix = backup_suffix
        self.require_remote = require_remote
        self.extra_view_paths = extra_view_paths or []

    def _ensure_remote(self) -> None:
        if self.require_remote and (not self.pi_sender or not self.remote_root):
            raise RuntimeError("PI sender and remote_root are required to update dashboards.")

    def _remote_path(self, filename: str) -> str:
        root = (self.remote_root or "").rstrip("/")
        return f"{root}/dashboards/{filename}" if root else f"dashboards/{filename}"

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
        self.pi_sender.send_file(local_path, remote_path)

    def create_main(self, registry_data: dict) -> None:
        """Create or replace the main dashboard YAML."""
        self._ensure_remote()
        local_path = Path(self.dashboard_creator.output_yaml_path).resolve()
        filename = local_path.name
        remote_path = self._remote_path(filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.dashboard_creator.create_from_registry(registry_data)
        self._send_local(local_path, remote_path)

    def create_config(self, registry_data: dict) -> None:
        """Create or replace the config dashboard YAML."""
        self._ensure_remote()
        local_path = Path(self.dashboard_creator.config_output_yaml_path).resolve()
        filename = local_path.name
        remote_path = self._remote_path(filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.dashboard_creator.create_config_dashboard(registry_data)
        self._send_local(local_path, remote_path)

    def upsert_config_main_view(self) -> None:
        """Upsert the fixed main view into the config dashboard YAML."""
        self._ensure_remote()
        main_view_path = self.dashboard_creator.main_config_view_path
        view_paths = []
        if main_view_path:
            view_paths.append(main_view_path)
        view_paths.extend([p for p in self.extra_view_paths if p])
        views_to_upsert = []
        for path in view_paths:
            view = self.dashboard_creator._load_external_view(path)
            if view:
                views_to_upsert.append(view)
        if not views_to_upsert:
            return
        try:
            registry_data = RegistryManager().data
            if isinstance(registry_data, dict):
                self.dashboard_creator._apply_config_view_visibility(views_to_upsert, registry_data)
                self.dashboard_creator._apply_subscription_button_rules(views_to_upsert)
        except Exception:
            pass

        local_path = Path(self.dashboard_creator.config_output_yaml_path).resolve()
        filename = local_path.name
        remote_path = self._remote_path(filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        if original is None and local_path.exists():
            original = local_path.read_text(encoding="utf-8")

        doc = yaml.safe_load(original) if original else {}
        if not isinstance(doc, dict):
            doc = {}

        views = doc.get("views")
        if not isinstance(views, list):
            views = []
        # Ensure Test view is not part of Configuration dashboard.
        views = [
            v
            for v in views
            if isinstance(v, dict)
            and (v.get("path") not in {"test"} and v.get("title") not in {"Test"})
        ]

        for main_view in views_to_upsert:
            view_path = main_view.get("path")
            view_title = main_view.get("title")
            match_idx = None
            for idx, view in enumerate(views):
                if not isinstance(view, dict):
                    continue
                if view_path and view.get("path") == view_path:
                    match_idx = idx
                    break
                if view_title and view.get("title") == view_title:
                    match_idx = idx
                    break
            if match_idx is None:
                views.insert(0, main_view)
            else:
                views[match_idx] = main_view

        doc["views"] = views
        try:
            registry_data = RegistryManager().data
            if isinstance(registry_data, dict):
                self.dashboard_creator._apply_config_view_visibility(views, registry_data)
                self.dashboard_creator._apply_subscription_button_rules(views)
        except Exception:
            pass
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        self._backup_remote_text(remote_path, original)
        self._send_local(local_path, remote_path)

    def read_remote(self, filename: str) -> str:
        """Read a dashboard YAML from the Pi."""
        self._ensure_remote()
        remote_path = self._remote_path(filename)
        return self.pi_sender.read_remote_text(remote_path)

    def delete(self, filename: str) -> bool:
        """Delete a dashboard YAML file."""
        if self.yaml_manager:
            return self.yaml_manager.delete_dashboard(filename)
        self._ensure_remote()
        remote_path = self._remote_path(filename)
        if self.pi_sender:
            self.pi_sender.remove_remote(remote_path)
            return True
        return False
