# hausie_app/hausie_orchestrator.py
from __future__ import annotations
import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from ..core.clients.ha_client import HAClient
from ..core.flow_logger import get_logger
from ..core.inventory.process_inventory import InventoryProcessor
from ..core.inventory.registry_manager import RegistryManager
from ..core.inventory.inventory_comparator import InventoryComparator
from ..core.managers.automation_manager import AutomationManager
from ..core.managers.config_manager import ConfigManager
from ..core.managers.cover_manager import CoverManager
from ..core.managers.dashboard_manager import DashboardManager
from ..core.managers.group_manager import GroupManager
from ..core.managers.helper_manager import HelperManager
from ..core.managers.script_manager import ScriptManager
from ..core.managers.switch_manager import SwitchManager
from ..core.managers.user_manager import UserManager
from ..core.managers.help_message_manager import DEFAULT_MESSAGES
from ..core.creators.dashboard_creator import DashboardCreator
from ..core.io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..core.io.homeassistant_yaml_updater import HomeAssistantYamlUpdater
from ..core.io.pi_file_sender import PiFileSender
from ..core.utils.naming import (
    build_alias,
    build_filename,
    build_object_id,
    normalize_option_label,
    slugify,
    titleize,
    unique_slug,
)
from .dashboard_updater import DashboardUpdater
from ..constants import EntityType, InputType, Labels, LABELS

PKG_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = PKG_DIR.parent


class HausieOrchestrator:
    """HausieOrchestrator wires together HA clients and creators."""

    def __init__(
        self,
        ha_client: HAClient,
        inventory_processor: InventoryProcessor,
        automator: DashboardUpdater,
        *,
        pi_sender: PiFileSender | None = None,
        pi_root: str | None = None,
        pi_config_path: str | None = None,
        require_remote_yaml: bool = True,
    ):
        """Initialize orchestration dependencies and helpers."""
        self.ha: HAClient = ha_client
        self.inventory_processor: InventoryProcessor = inventory_processor
        self.automator: DashboardUpdater = automator
        self._log = get_logger("core")

        self.registry = RegistryManager()
        self.user_manager = UserManager(ha_client=self.ha, registry=self.registry)
        self.yaml_updater = HomeAssistantYamlUpdater(
            pi_sender=pi_sender,
            remote_root=pi_root,
            require_remote=require_remote_yaml,
        )
        self.yaml_manager = HomeAssistantYamlManager(
            pi_sender=pi_sender,
            remote_root=pi_root,
            require_remote=require_remote_yaml,
        )
        self.automation_manager = AutomationManager(
            yaml_updater=self.yaml_updater,
            yaml_manager=self.yaml_manager,
        )
        self.helper_manager = HelperManager(yaml_updater=self.yaml_updater)
        self.group_manager = GroupManager(
            yaml_updater=self.yaml_updater,
            yaml_manager=self.yaml_manager,
        )
        self.cover_manager = CoverManager(
            yaml_updater=self.yaml_updater,
            yaml_manager=self.yaml_manager,
        )
        self.script_manager = ScriptManager(
            yaml_updater=self.yaml_updater,
            yaml_manager=self.yaml_manager,
            script_creator=self.yaml_updater.script_creator,
        )
        self.switch_manager = SwitchManager(yaml_updater=self.yaml_updater)
        self.button_styles = self._load_button_styles()

        # Ruta al view externo (opcional) ahora relativa al paquete
        main_view_path = PKG_DIR / "templates" / "UI" / "config_main_view.yaml"
        test_view_path = PKG_DIR / "templates" / "UI" / "config_test_view.yaml"
        user_names = self._get_user_names()
        self.dashboard_creator = DashboardCreator(
            button_styles=self.button_styles,
            main_config_view_path=str(main_view_path),
            user_names=user_names,
        )
        self.dashboard_manager = DashboardManager(
            self.dashboard_creator,
            pi_sender=pi_sender,
            remote_root=pi_root,
            yaml_manager=self.yaml_manager,
            require_remote=require_remote_yaml,
            extra_view_paths=[],
        )
        config_path = pi_config_path or (f"{pi_root}/configuration.yaml" if pi_root else None)
        self.pi_repo_root = self._resolve_pi_repo_root(pi_root)
        shell_commands = self._build_shell_commands(self.pi_repo_root)
        self.config_manager = (
            ConfigManager(
                pi_sender=pi_sender,
                config_path=config_path,
                shell_commands=shell_commands,
            )
            if pi_sender and config_path
            else None
        )

        # hausie/homeassistant/data donde guardas inventarios (ajusta si usas otra ruta)
        self.cleaned_inventory_path = self.inventory_processor.inventory_file
        self._users_changed = False

    # ---------------------------
    # Helpers de archivos
    # ---------------------------
    def _load_button_styles(self) -> dict:
        """Load button style YAML for dashboard cards."""
        path = PKG_DIR / "templates" / "UI" / "button_style_config_device.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Button styles YAML not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_plan_badge_config(self) -> dict[str, Any]:
        path = PKG_DIR / "config" / "plan_badge_content.yaml"
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _normalize_plan_badge_list(items: Any) -> list[str]:
        if items is None:
            return []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            return []
        cleaned: list[str] = []
        for item in items:
            text = str(item).strip()
            if text:
                cleaned.append(text)
        return cleaned

    @classmethod
    def _build_plan_badge_details(cls, entry: dict[str, Any], *, max_len: int = 255) -> str:
        includes = cls._normalize_plan_badge_list(entry.get("includes"))
        excludes = cls._normalize_plan_badge_list(entry.get("excludes"))
        lines: list[str] = []
        for item in includes:
            lines.append(f"- Included: {item}")
        for item in excludes:
            lines.append(f"- Not included: {item}")
        if not lines:
            return ""
        text = "\n".join(lines)
        if len(text) <= max_len:
            return text
        trimmed: list[str] = []
        total = 0
        for line in lines:
            extra = line + "\n"
            if total + len(extra) > max_len:
                break
            trimmed.append(line)
            total += len(extra)
        if not trimmed:
            return text[:max_len].rstrip()
        return "\n".join(trimmed)

    def _resolve_plan_badge_content(self) -> tuple[str, str]:
        plan_value = (self.dashboard_creator.subscription_plan or "").strip()
        plan_key = plan_value.lower() or "plan 1"
        config = self._load_plan_badge_config()
        plan_order = config.get("plan_order") or []
        if isinstance(plan_order, str):
            plan_order = [plan_order]
        plan_order = [str(item).strip().lower() for item in plan_order if str(item).strip()]
        if not plan_order:
            plan_order = ["plan 1", "plan 2", "plan 3", "plan 4"]
        plans = config.get("plans") or {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in plans.items():
            if isinstance(value, dict):
                normalized[str(key).strip().lower()] = value
        entry = normalized.get(plan_key)
        if not entry:
            for fallback in plan_order:
                entry = normalized.get(fallback)
                if entry:
                    break
        entry = entry or {}
        name = str(entry.get("name") or "").strip() or titleize(plan_key)
        details = self._build_plan_badge_details(entry)
        return name, details

    @staticmethod
    def _escape_input_text_initial(value: str) -> str:
        if value is None:
            return ""
        text = str(value).replace('"', "'")
        return text.replace("\n", "\\n")

    @staticmethod
    def _get_user_names() -> List[str]:
        raw = os.getenv("HA_USER_NAMES", "").strip()
        if raw:
            names = [name.strip() for name in raw.split(",") if name.strip()]
        else:
            names = ["Monica", "Mateo", "Guest"]
        return names

    @staticmethod
    def _filter_user_names(names: List[str]) -> List[str]:
        blocked = ("hausie", "supervisor", "home assistant content")
        return [
            name
            for name in names
            if not any(token in name.lower() for token in blocked)
        ]

    @staticmethod
    def _is_users_area_excluded(area_name: str) -> bool:
        return slugify(area_name or "") in {"general", "system", "configuration"}

    @staticmethod
    def _resolve_pi_repo_root(pi_root: str | None) -> str | None:
        env_root = os.getenv("PI_REPO_ROOT", "").strip()
        if env_root:
            return env_root
        if not pi_root:
            return None
        try:
            root_path = Path(pi_root).resolve()
        except Exception:
            return None
        parent = root_path.parent
        if str(parent) in {"", "/"}:
            return None
        return str(parent)

    @staticmethod
    def _build_shell_commands(pi_repo_root: str | None) -> dict[str, str]:
        return {}

    @staticmethod
    def _prefixed_id(prefix: str, area_name: str, subject: str) -> str:
        """Build an id with a fixed prefix."""
        base = build_object_id(area_name, subject)
        return f"{prefix}_{base}"

    # ---------------------------
    # 1) Fetch + Clean
    # ---------------------------
    def fetch_and_clean(self):
        """Fetch raw data from HA and generate cleaned inventory JSON."""
        self._log.start("Fetching raw data from Home Assistant.")
        self.ha.fetch_all(include_users=False)
        self._users_changed = self.user_manager.sync_users()
        self._sync_label_registry()

        self._log.start("Cleaning inventory.")
        self.inventory_processor.process()
        self._log.ok(f"Cleaned inventory saved to {self.cleaned_inventory_path}")

    def refresh_inventory_if_changed(self) -> dict | None:
        """
        Fetch raw data, rebuild inventory, compare with the current inventory,
        and return the diff when changes are detected.
        """
        if self.cleaned_inventory_path.exists():
            with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
                current_inventory = json.load(f)
        else:
            current_inventory = {"areas": []}

        self.ha.fetch_all(include_users=False)
        self._users_changed = self.user_manager.sync_users()
        self._sync_label_registry()
        self.inventory_processor.process()

        if not self.cleaned_inventory_path.exists():
            raise FileNotFoundError("Inventory not found after processing.")
        with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
            new_inventory = json.load(f)

        comparator = InventoryComparator()
        diff = comparator.compare(current_inventory, new_inventory)

        if not diff["areas_added"] and not diff["areas_removed"] and not diff["areas_changed"]:
            self._log.skip("No inventory changes detected; stopping.")
            return None

        self._log.info("Inventory changes detected.")
        return diff

    def ensure_fixed_assets(self) -> None:
        """Ensure fixed assets that do not depend on device inventory exist and are up to date."""
        self._ensure_registry_area("general", "General")
        self.user_manager.sync_users()
        self._ensure_cover_directory()
        self._ensure_fixed_helpers()
        self._ensure_fixed_scripts()
        self._ensure_fixed_automations()
        self._ensure_fixed_switches()
        self._sync_brand_assets()
        if self.dashboard_manager:
            self.dashboard_manager.upsert_config_main_view()

    def _sync_label_registry(self) -> None:
        """Fetch labels from HA and store them in the registry."""
        try:
            labels = self.ha.fetch_labels()
        except Exception as exc:
            self._log.warn(f"Failed to fetch labels: {exc}")
            return
        self.registry.set_labels(labels)

    def _ensure_registry_area(self, area_id: str, name: str) -> None:
        """Ensure a registry area exists for fixed assets."""
        area = self.registry.get_area(area_id)
        if area:
            if name and area.get("name") != name:
                self.registry.update_area(area_id, name=name)
            return
        self.registry.add_area(area_id, name, [])

    def _sync_brand_assets(self) -> None:
        asset_root = PKG_DIR / "assets"
        brand_dir = asset_root / "brand"
        fonts_dir = asset_root / "fonts"
        subdirs = [path for path in (brand_dir, fonts_dir) if path.exists()]
        if not subdirs:
            return

        if self.yaml_manager.pi_sender and self.yaml_manager.remote_root:
            remote_root = self.yaml_manager.remote_root.rstrip("/")
            try:
                for subdir in subdirs:
                    remote_dir = f"{remote_root}/www/hausie/{subdir.name}"
                    self.yaml_manager.pi_sender.send_dir(subdir, remote_dir)
                self._log.ok("Brand assets synced to Home Assistant.")
            except Exception as exc:
                self._log.warn(f"Brand assets sync failed: {exc}")
            return

        target_root = self.yaml_manager.homeassistant_root / "www" / "hausie"
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            for subdir in subdirs:
                dest = target_root / subdir.name
                dest.mkdir(parents=True, exist_ok=True)
                for asset in subdir.iterdir():
                    if asset.is_file():
                        shutil.copy2(asset, dest / asset.name)
            self._log.ok("Brand assets synced locally.")
        except Exception as exc:
            self._log.warn(f"Brand assets local sync failed: {exc}")

    def _ensure_cover_directory(self) -> None:
        """Ensure the grouped covers directory exists locally and remotely."""
        try:
            self.yaml_manager.covers_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._log.warn(f"Failed to prepare local covers directory: {exc}")
        if self.yaml_manager.pi_sender and self.yaml_manager.remote_root:
            remote_dir = f"{self.yaml_manager.remote_root.rstrip('/')}/covers"
            try:
                self.yaml_manager.pi_sender.ensure_remote_dir(remote_dir)
            except Exception as exc:
                self._log.warn(f"Failed to prepare remote covers directory: {exc}")

    def _upsert_registry_input(self, area_id: str, input_id: str, name: str, input_type: str) -> None:
        """Add or update an input entry in the registry."""
        existing = self.registry.get_input(area_id, input_id)
        if existing:
            self.registry.update_input(area_id, input_id, name=name, input_type=input_type)
        else:
            self.registry.add_input(area_id, input_id, name, input_type)

    def _upsert_registry_switch(self, area_id: str, switch_id: str, name: str, switch_type: str, data: Dict[str, Any]) -> None:
        """Add or update a switch entry in the registry."""
        existing = self.registry.get_switch(area_id, switch_id)
        if existing:
            self.registry.update_switch(area_id, switch_id, name=name, switch_type=switch_type, data=data)
        else:
            self.registry.add_switch(area_id, switch_id, name, switch_type, data)

    def _ensure_fixed_scripts(self) -> None:
        help_entities = [
            f"{InputType.INPUT_BOOLEAN}.ui_help_main",
            f"{InputType.INPUT_BOOLEAN}.ui_help_devices",
            f"{InputType.INPUT_BOOLEAN}.ui_help_buttons",
            f"{InputType.INPUT_BOOLEAN}.ui_help_battery",
            f"{InputType.INPUT_BOOLEAN}.ui_help_light_config",
            f"{InputType.INPUT_BOOLEAN}.ui_help_blinds_config",
            f"{InputType.INPUT_BOOLEAN}.ui_help_temperature_config",
            f"{InputType.INPUT_BOOLEAN}.ui_help_notify_automation",
            f"{InputType.INPUT_BOOLEAN}.ui_help_new_devices",
            f"{InputType.INPUT_BOOLEAN}.ui_help_users",
            f"{InputType.INPUT_BOOLEAN}.ui_help_automations",
            f"{InputType.INPUT_BOOLEAN}.ui_help_hausie",
        ]
        self.script_manager.create_help_boxes_reset_script(
            script_id="ui_help_reset",
            script_name="Reactivate Help Boxes",
            entity_ids=help_entities,
        )
        self.registry.add_script("general", "ui_help_reset", "Reactivate Help Boxes", help_entities)
        self.script_manager.create_upgrade_plan_popup_script(
            script_id="hausie_upgrade_popup",
            script_name="Hausie Upgrade Popup",
        )
        self.registry.add_script("general", "hausie_upgrade_popup", "Hausie Upgrade Popup", [])

    def _sync_env_file(self) -> None:
        if not self.yaml_manager.pi_sender or not self.pi_repo_root:
            return
        local_env = (ROOT_DIR / ".env").resolve()
        if not local_env.exists():
            return
        remote_env = f"{self.pi_repo_root.rstrip('/')}/.env"
        self.yaml_manager.pi_sender.send_file(local_env, remote_env)

    def _ensure_fixed_automations(self) -> None:
        """Ensure core automations for fixed flows exist."""
        automation_id = "new_device_created"
        context = {
            "automation_alias": "Hausie - Nuevo dispositivo creado",
            "rest_command_id": "new_device_create",
        }
        self.automation_manager.create(
            {"id": automation_id},
            context=context,
            template_file="hausie_new_device_created.yaml",
        )
        save_automation_id = "new_device_saved"
        save_context = {
            "automation_alias": "Hausie - Guardar dispositivo nuevo",
            "rest_command_id": "new_device_save",
            "save_entity": f"{InputType.INPUT_BUTTON}.new_device_save",
        }
        self.automation_manager.create(
            {"id": save_automation_id},
            context=save_context,
            template_file="hausie_new_device_saved.yaml",
        )
        rotate_minutes = os.getenv("HAUSIE_HELP_ROTATE_MINUTES", "10").strip()
        if not rotate_minutes.isdigit() or int(rotate_minutes) < 1:
            rotate_minutes = "10"
        help_rotate_id = "ui_help_rotate_messages"
        help_rotate_context = {
            "automation_alias": "Hausie - Rotate Help Messages",
            "rest_command_id": "ui_help_rotate",
            "rotate_minutes": rotate_minutes,
        }
        self.automation_manager.create(
            {"id": help_rotate_id},
            context=help_rotate_context,
            template_file="ui_help_rotate_messages.yaml",
        )
        scan_time = os.getenv("HAUSIE_NEW_DEVICE_SCAN_TIME", "03:00:00").strip()
        scan_id = "new_devices_scan_daily"
        scan_context = {
            "automation_alias": "Hausie - Scan Unlabeled Devices",
            "rest_command_id": "new_devices_scan",
            "scan_time": scan_time or "03:00:00",
        }
        self.automation_manager.create(
            {"id": scan_id},
            context=scan_context,
            template_file="hausie_new_devices_scan_daily.yaml",
        )
        rebuild_id = "core_rebuild_hausie"
        rebuild_context = {
            "automation_alias": "Hausie - Rebuild Hausie",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.core_rebuild_hausie",
            "rest_command_id": "rebuild_hausie",
        }
        self.automation_manager.create(
            {"id": rebuild_id},
            context=rebuild_context,
            template_file="hausie_cleanup_button.yaml",
        )
        restart_id = "core_restart_hausie"
        restart_context = {
            "automation_alias": "Hausie - Restart Hausie",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.core_restart_hausie",
            "rest_command_id": "restart_hausie",
        }
        self.automation_manager.create(
            {"id": restart_id},
            context=restart_context,
            template_file="hausie_cleanup_button.yaml",
        )
        self._ensure_test_automations()

    def _ensure_fixed_switches(self) -> None:
        """Ensure core switches for add-ons exist."""
        self._ensure_automation_switches()
        self.create_switches_from_registry()

    def _ensure_test_automations(self) -> None:
        """Ensure test automations exist (not stored in registry)."""
        cleanup_base_id = "cleanup_base_assets"
        cleanup_base_context = {
            "automation_alias": "Hausie - Clean Base Assets",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.cleanup_base_assets",
            "rest_command_id": "cleanup_base",
        }
        self.automation_manager.create(
            {"id": cleanup_base_id},
            context=cleanup_base_context,
            template_file="hausie_cleanup_button.yaml",
        )
        cleanup_hausie_id = "cleanup_hausie_assets"
        cleanup_hausie_context = {
            "automation_alias": "Hausie - Clean Hausie Assets",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.cleanup_hausie_assets",
            "rest_command_id": "cleanup_hausie",
        }
        self.automation_manager.create(
            {"id": cleanup_hausie_id},
            context=cleanup_hausie_context,
            template_file="hausie_cleanup_button.yaml",
        )
        test_create_base_id = "test_create_base"
        test_create_base_context = {
            "automation_alias": "Hausie - Test Create Base",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.test_create_base",
            "rest_command_id": "create_base",
        }
        self.automation_manager.create(
            {"id": test_create_base_id},
            context=test_create_base_context,
            template_file="hausie_cleanup_button.yaml",
        )
        test_create_hausie_id = "test_create_hausie"
        test_create_hausie_context = {
            "automation_alias": "Hausie - Test Create Hausie",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.test_create_hausie",
            "rest_command_id": "create_hausie",
        }
        self.automation_manager.create(
            {"id": test_create_hausie_id},
            context=test_create_hausie_context,
            template_file="hausie_cleanup_button.yaml",
        )
        test_rebuild_all_id = "test_rebuild_all"
        test_rebuild_all_context = {
            "automation_alias": "Hausie - Test Rebuild All",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.test_rebuild_all",
        }
        self.automation_manager.create(
            {"id": test_rebuild_all_id},
            context=test_rebuild_all_context,
            template_file="hausie_test_rebuild_all.yaml",
        )
        test_popup_update_id = "test_popup_update"
        test_popup_update_context = {
            "automation_alias": "TEST_POPUP - Update",
            "trigger_entity": f"{InputType.INPUT_BUTTON}.test_popup_update",
            "rest_command_id": "test_popup_wait",
            "loading_title": "TEST_POPUP",
            "loading_message": "Loading...",
            "success_title": "TEST_POPUP",
            "success_message": "TU Hasuie esta actualizada",
        }
        self.automation_manager.create(
            {"id": test_popup_update_id},
            context=test_popup_update_context,
            template_file="hausie_test_popup_wait.yaml",
        )

    def _ensure_automation_switches(self) -> None:
        """Ensure switches exist for all registered automations."""
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            for automation in area.get("automations", []) or []:
                automation_id = automation.get("id")
                if not automation_id:
                    continue
                name = automation.get("name") or automation_id
                self._upsert_registry_switch(
                    area_id,
                    f"{automation_id}_switch",
                    name,
                    "automation",
                    {"automation_id": automation_id},
                )

    def _ensure_fixed_helpers(self) -> None:
        """Ensure core helpers used by configuration flows exist."""
        self.helper_manager.create(
            "general",
            {"id": "core_rebuild_hausie", "type": InputType.INPUT_BUTTON, "name": "Rebuild Hausie"},
            context={"helper_name": "Rebuild Hausie"},
            template_file="simple_button_template.yaml",
        )
        self._upsert_registry_input("general", "core_rebuild_hausie", "Rebuild Hausie", InputType.INPUT_BUTTON)
        self.helper_manager.create(
            "general",
            {"id": "core_restart_hausie", "type": InputType.INPUT_BUTTON, "name": "Restart Hausie"},
            context={"helper_name": "Restart Hausie"},
            template_file="simple_button_template.yaml",
        )
        self._upsert_registry_input("general", "core_restart_hausie", "Restart Hausie", InputType.INPUT_BUTTON)
        self._ensure_test_helpers()
        self.helper_manager.create(
            "general",
            {"id": "new_device_start", "type": InputType.INPUT_BUTTON, "name": "New Device"},
            context={"helper_name": "New Device"},
            template_file="simple_button_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_start", "New Device", InputType.INPUT_BUTTON)
        self.helper_manager.create(
            "general",
            {"id": "perm_manage_users", "type": InputType.INPUT_BUTTON, "name": "Manage Users"},
            context={"helper_name": "Manage Users"},
            template_file="simple_button_template.yaml",
        )
        self._upsert_registry_input("general", "perm_manage_users", "Manage Users", InputType.INPUT_BUTTON)
        self.helper_manager.create(
            "general",
            {"id": "new_device_save", "type": InputType.INPUT_BUTTON, "name": "New Device Save"},
            context={"helper_name": "New Device Save"},
            template_file="simple_button_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_save", "New Device Save", InputType.INPUT_BUTTON)
        self.helper_manager.create(
            "general",
            {"id": "new_device_found", "type": InputType.INPUT_BOOLEAN, "name": "New Device Found"},
            context={"helper_name": "New Device Found", "initial_state": "off"},
            template_file="simple_boolean_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_found", "New Device Found", InputType.INPUT_BOOLEAN)
        plan_name, plan_details = self._resolve_plan_badge_content()
        self.helper_manager.create(
            "general",
            {"id": "hausie_plan_text", "type": InputType.INPUT_TEXT, "name": "Hausie Plan"},
            context={"helper_name": "Hausie Plan", "initial": self._escape_input_text_initial(plan_name)},
            template_file="new_device_text_template.yaml",
            output_filename="input_text.dashboards.yaml",
        )
        self._upsert_registry_input("general", "hausie_plan_text", "Hausie Plan", InputType.INPUT_TEXT)
        self.helper_manager.create(
            "general",
            {"id": "hausie_plan_details", "type": InputType.INPUT_TEXT, "name": "Hausie Plan Details"},
            context={"helper_name": "Hausie Plan Details", "initial": self._escape_input_text_initial(plan_details)},
            template_file="new_device_text_template.yaml",
            output_filename="input_text.dashboards.yaml",
        )
        self._upsert_registry_input(
            "general",
            "hausie_plan_details",
            "Hausie Plan Details",
            InputType.INPUT_TEXT,
        )
        self.helper_manager.create(
            "general",
            {"id": "hausie_trial_until", "type": InputType.INPUT_TEXT, "name": "Hausie Trial Until"},
            context={"helper_name": "Hausie Trial Until", "initial": ""},
            template_file="new_device_text_template.yaml",
            output_filename="input_text.dashboards.yaml",
        )
        self._upsert_registry_input(
            "general",
            "hausie_trial_until",
            "Hausie Trial Until",
            InputType.INPUT_TEXT,
        )
        label_options = []
        registry_labels = [
            label.get("name")
            for label in self.registry.list_labels()
            if isinstance(label, dict) and label.get("name")
        ]
        if registry_labels:
            cleaned_labels = [name for name in registry_labels if name and name.lower() != "system"]
            label_options.extend(sorted(set(cleaned_labels), key=str.lower))
        else:
            label_options.extend([label for label in LABELS if label.lower() != "system"])
        area_options = []
        for area in self.registry.list_areas():
            name = area.get("name") or area.get("area_id")
            if name and name.lower() not in {"system", "none"}:
                area_options.append(name)
        area_options = sorted(set(area_options), key=str.lower)
        self.helper_manager.create(
            "general",
            {"id": "new_device_name", "type": InputType.INPUT_TEXT, "name": "New Device Name"},
            context={"helper_name": "New Device Name"},
            template_file="new_device_text_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_name", "New Device Name", InputType.INPUT_TEXT)
        self.helper_manager.create(
            "general",
            {"id": "new_device_device_id", "type": InputType.INPUT_TEXT, "name": "New Device Id"},
            context={"helper_name": "New Device Id"},
            template_file="new_device_text_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_device_id", "New Device Id", InputType.INPUT_TEXT)
        self.helper_manager.create(
            "general",
            {"id": "new_device_label", "type": InputType.INPUT_SELECT, "name": "New Device Label"},
            context={"helper_name": "New Device Label", "options": label_options},
            template_file="options_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_label", "New Device Label", InputType.INPUT_SELECT)
        self.helper_manager.create(
            "general",
            {"id": "new_device_area", "type": InputType.INPUT_SELECT, "name": "New Device Area"},
            context={"helper_name": "New Device Area", "options": area_options},
            template_file="options_template.yaml",
        )
        self._upsert_registry_input("general", "new_device_area", "New Device Area", InputType.INPUT_SELECT)
        help_helpers = [
            (f"ui_help_{slugify(view_key)}", f"Help - {titleize(slugify(view_key))} View", "on")
            for view_key in DEFAULT_MESSAGES.keys()
        ]
        for helper_id, helper_name, initial_state in help_helpers:
            self.helper_manager.create(
                "general",
                {"id": helper_id, "type": InputType.INPUT_BOOLEAN, "name": helper_name},
                context={"helper_name": helper_name, "initial_state": initial_state},
                template_file="simple_boolean_template.yaml",
                output_filename="input_boolean.dashboards.yaml",
            )
            self._upsert_registry_input("general", helper_id, helper_name, InputType.INPUT_BOOLEAN)
        for view_key, messages in DEFAULT_MESSAGES.items():
            message_id = f"ui_help_message_{slugify(view_key)}"
            default_text = ""
            if isinstance(messages, list) and messages:
                default_entry = messages[0]
                if isinstance(default_entry, dict):
                    default_text = str(default_entry.get("text") or "")
                elif isinstance(default_entry, str):
                    default_text = default_entry
            helper_name = f"Help Message - {titleize(slugify(view_key))}"
            self.helper_manager.create(
                "general",
                {"id": message_id, "type": InputType.INPUT_TEXT, "name": helper_name},
                context={"helper_name": helper_name, "initial": default_text},
                template_file="new_device_text_template.yaml",
                output_filename="input_text.dashboards.yaml",
            )
            self._upsert_registry_input("general", message_id, helper_name, InputType.INPUT_TEXT)
        self.helper_manager.create(
            "general",
            {"id": "allow_remote_support", "type": InputType.INPUT_BOOLEAN, "name": "Remote Support"},
            context={"helper_name": "Remote Support", "initial_state": "off"},
            template_file="simple_boolean_template.yaml",
        )
        self._upsert_registry_input("general", "allow_remote_support", "Remote Support", InputType.INPUT_BOOLEAN)
        user_records = self.registry.list_users()
        user_names = []
        seen = set()
        for user in user_records:
            name = user.get("name") if isinstance(user, dict) else None
            if not name or name in seen:
                continue
            seen.add(name)
            user_names.append(name)
        if not user_names:
            user_names = self._get_user_names()
        user_names = self._filter_user_names(user_names)

        for area in self.registry.list_areas():
            area_name = area.get("name") or area.get("area_id")
            if not area_name or self._is_users_area_excluded(area_name):
                continue
            user_entities = []
            all_users_id = self._prefixed_id("perm", area_name, "all_users")
            self.helper_manager.create(
                "general",
                {"id": all_users_id, "type": InputType.INPUT_BOOLEAN, "name": "All users"},
                context={"helper_name": "All users", "initial_state": "on"},
                template_file="simple_boolean_template.yaml",
                output_filename="input_boolean.users.yaml",
            )
            self._upsert_registry_input("general", all_users_id, "All users", InputType.INPUT_BOOLEAN)
            for user_name in user_names:
                user_id = self._prefixed_id("perm", area_name, user_name)
                self.helper_manager.create(
                    "general",
                    {"id": user_id, "type": InputType.INPUT_BOOLEAN, "name": user_name},
                    context={"helper_name": user_name, "initial_state": "on"},
                    template_file="simple_boolean_template.yaml",
                    output_filename="input_boolean.users.yaml",
                )
                self._upsert_registry_input("general", user_id, user_name, InputType.INPUT_BOOLEAN)
                user_entities.append(f"{InputType.INPUT_BOOLEAN}.{user_id}")
            if user_entities:
                self.group_manager.update_boolean_group(
                    area_name,
                    group_id=all_users_id,
                    group_name="All users",
                    entities=user_entities,
                    all_state=True,
                )

    def _ensure_test_helpers(self) -> None:
        """Create test helpers without registering them in the registry."""
        test_helpers = [
            ("cleanup_base_assets", "Test - Clean Base"),
            ("cleanup_hausie_assets", "Test - Clean Hausie"),
            ("test_create_base", "Test - Create Base"),
            ("test_create_hausie", "Test - Create Hausie"),
            ("test_rebuild_all", "Test - Rebuild All"),
            ("test_popup_update", "TEST_POPUP - Update"),
        ]
        for helper_id, helper_name in test_helpers:
            self.helper_manager.create(
                "general",
                {"id": helper_id, "type": InputType.INPUT_BUTTON, "name": helper_name},
                context={"helper_name": helper_name},
                template_file="simple_button_template.yaml",
                output_filename="hausie_input_button.test.yaml",
            )
            if self.registry.get_input("general", helper_id):
                self.registry.delete_input("general", helper_id)
        for helper_id in ["hausie_pairing_browser_id", "hausie_pairing_active", "hausie_pairing_started_at"]:
            if self.registry.get_input("general", helper_id):
                self.registry.delete_input("general", helper_id)

    def _ensure_automation_buttons(self) -> None:
        """Ensure automation section buttons exist (non-base)."""
        self._ensure_registry_area("general", "General")
        buttons = [
            ("ui_lights_automations", "Lights Automations"),
            ("ui_button_automations", "Button Automations"),
            ("ui_blinds_automations", "Blinds Automations"),
            ("ui_temperature_automations", "Temperature Automations"),
            ("ui_notify_automations", "Notify Automations"),
        ]
        for helper_id, helper_name in buttons:
            self.helper_manager.create(
                "general",
                {"id": helper_id, "type": InputType.INPUT_BUTTON, "name": helper_name},
                context={"helper_name": helper_name},
                template_file="simple_button_template.yaml",
            )
            self._upsert_registry_input("general", helper_id, helper_name, InputType.INPUT_BUTTON)

    def create_hausie(
        self,
        *,
        dashboard_path: str = "dashboard-hausie/0",
        main_dashboard_yaml: str | Path = "hausie/homeassistant/dashboards/hausie_dashboard.yaml",
        update_ui: bool | None = None,
        update_fixed_assets: bool = True,
    ) -> dict | None:
        """
        Unified flow: build inventory, compute diff, and apply the required updates.
        """
        previous_automations = self._collect_automation_catalog()
        had_inventory = self.cleaned_inventory_path.exists()
        had_registry = bool(self.registry.list_areas())

        if had_inventory:
            with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
                current_inventory = json.load(f)
        else:
            current_inventory = {"areas": []}

        self.fetch_and_clean()

        if self.cleaned_inventory_path.exists():
            with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
                new_inventory = json.load(f)
            diff = InventoryComparator().compare(current_inventory, new_inventory)
        else:
            diff = None
        has_changes = bool(
            diff
            and (diff.get("areas_added") or diff.get("areas_removed") or diff.get("areas_changed"))
        )
        users_changed = self._users_changed

        ran_variable = False
        if has_changes:
            self.reconcile_inventory_diff(diff)
            ran_variable = True
        elif users_changed:
            self.create_dashboards()
            ran_variable = True

        if update_fixed_assets:
            self.ensure_fixed_assets()
            if self.config_manager:
                self._log.start("Updating configuration.yaml.")
                self.config_manager.sync_config_dashboard()
                self._log.ok("configuration.yaml updated.")
        self._ensure_automation_buttons()

        if not ran_variable and had_registry:
            has_cover_groups = any(self._area_has_covers(area) for area in self.registry.list_areas())
            if has_cover_groups:
                self._sync_cover_groups_from_registry()
                self._sync_global_cover_group()
                self.create_dashboards()
                ran_variable = True

        if not ran_variable:
            self._log.skip("No inventory changes detected; stopping.")
            self._log_automation_summary(previous_automations)
            return diff

        if update_ui is None:
            skip_ui = os.getenv("SKIP_UI_DASHBOARD", "").strip().lower() in {"1", "true", "yes"}
            update_ui = not skip_ui
        if update_ui:
            try:
                self.update_dashboard_via_playwright(
                    dashboard_path=dashboard_path,
                    yaml_path=str(main_dashboard_yaml),
                )
            except Exception as exc:
                self._log.warn(f"Skipping main dashboard UI update due to error: {exc}")

        self._log_automation_summary(previous_automations)
        return diff

    def restart_hausie(
        self,
        *,
        dashboard_path: str = "dashboard-hausie/0",
        main_dashboard_yaml: str | Path = "hausie/homeassistant/dashboards/hausie_dashboard.yaml",
        update_ui: bool | None = None,
    ) -> dict | None:
        """
        Full restart: delete generated files, recreate base assets, then run create_hausie.
        """
        self._log.start("Deleting generated files.")
        self.yaml_manager.delete_all_generated_files(include_data=True, include_helpers=True)
        self.registry.reset()
        self._log.ok("Generated files deleted.")

        self._log.start("Creating base assets.")
        self.ensure_fixed_assets()
        if self.config_manager:
            self._log.start("Updating configuration.yaml.")
            self.config_manager.sync_config_dashboard()
            self._log.ok("configuration.yaml updated.")

        self._log.start("Running create_hausie workflow.")
        return self.create_hausie(
            dashboard_path=dashboard_path,
            main_dashboard_yaml=main_dashboard_yaml,
            update_ui=update_ui,
            update_fixed_assets=False,
        )

    def _collect_automation_catalog(self) -> dict[str, dict[str, str]]:
        """Collect automation ids and names by area id."""
        catalog: dict[str, dict[str, str]] = {}
        for area in self.registry.list_areas():
            if not isinstance(area, dict):
                continue
            area_id = area.get("area_id")
            if not area_id:
                continue
            automations = area.get("automations", []) or []
            entries: dict[str, str] = {}
            for auto in automations:
                if not isinstance(auto, dict):
                    continue
                auto_id = auto.get("id")
                if not auto_id:
                    continue
                entries[auto_id] = auto.get("name") or auto_id
            if entries:
                catalog[area_id] = entries
        return catalog

    def _log_automation_summary(self, previous: dict[str, dict[str, str]] | None = None) -> None:
        """Log a summary of newly created automations."""
        current = self._collect_automation_catalog()
        previous = previous or {}
        added_total = 0
        lines: list[str] = []
        for area in self.registry.list_areas():
            if not isinstance(area, dict):
                continue
            area_id = area.get("area_id")
            if not area_id:
                continue
            current_autos = current.get(area_id, {})
            previous_autos = previous.get(area_id, {})
            added_ids = [auto_id for auto_id in current_autos.keys() if auto_id not in previous_autos]
            if not added_ids:
                continue
            added_total += len(added_ids)
            area_name = area.get("name") or area_id
            labels = [f"{current_autos[auto_id]} ({auto_id})" for auto_id in sorted(added_ids)]
            lines.append(f"{area_name}: {', '.join(labels)}")

        if not lines:
            self._log.info("Automation summary: no new automations created.")
            return

        self._log.ok(f"Automation summary: created {added_total} automation(s).")
        for line in lines:
            self._log.info(line)

    def reconfigure_hausie(
        self,
        *,
        dashboard_path: str = "dashboard-hausie/0",
        main_dashboard_yaml: str | Path = "hausie/homeassistant/dashboards/hausie_dashboard.yaml",
        update_ui: bool | None = None,
        update_fixed_assets: bool = True,
    ) -> dict | None:
        """Clear generated artifacts, then run the full create_hausie flow."""
        self._log.start("Clearing generated files before reconfigure.")
        self.yaml_manager.clear_all_generated_files(include_data=True, include_helpers=True)
        self.registry.reset()
        return self.create_hausie(
            dashboard_path=dashboard_path,
            main_dashboard_yaml=main_dashboard_yaml,
            update_ui=update_ui,
            update_fixed_assets=update_fixed_assets,
        )

    def reconcile_inventory_diff(self, diff: dict | None) -> dict | None:
        """
        Apply inventory differences to the registry and regenerate artifacts when needed.
        """
        if not diff:
            self._log.skip("No diff provided; stopping.")
            return None

        old_registry = copy.deepcopy(self.registry.data)
        old_area_map = {
            a.get("area_id"): a
            for a in old_registry.get("areas", [])
            if a.get("area_id")
        }

        if not self.cleaned_inventory_path.exists():
            raise FileNotFoundError("Inventory not found. Run refresh_inventory_if_changed() first.")

        with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
            inventory = json.load(f)

        inventory_areas = {a.get("area_id"): a for a in inventory.get("areas", []) if a.get("area_id")}

        removed_area_ids = diff.get("areas_removed", [])
        added_area_ids = diff.get("areas_added", [])
        changed_area_ids = [c.get("area_id") for c in diff.get("areas_changed", []) if c.get("area_id")]
        affected_area_ids = set(added_area_ids + changed_area_ids)

        for area_id in removed_area_ids:
            old_area = old_area_map.get(area_id)
            if old_area:
                self._delete_removed_area_yaml(old_area, None)
            self.registry.delete_area(area_id)

        for area_id in affected_area_ids:
            old_area = old_area_map.get(area_id)
            area = inventory_areas.get(area_id)
            if not area:
                continue
            if self.registry.get_area(area_id):
                self.registry.update_area(area_id, area.get("name"), area.get("labels", []))
            else:
                self.registry.add_area(area_id, area.get("name"), area.get("labels", []))

            self.registry.set_devices(area_id, area.get("devices", []))

            area_entities = []
            for device in area.get("devices", []):
                self.inventory_processor.add_entities_by_device(device, area_entities)
            self.registry.set_entities(area_id, area_entities)

            self._sync_groups_for_area(area_id)
            self._sync_cover_group_for_area(area_id)
            created_script_ids = self._sync_group_scripts_for_area(area_id)
            self._sync_entity_scripts_for_area(area_id, created_script_ids)
            old_area_name = old_area.get("name") if old_area else None
            self._sync_rules_for_area(area_id, old_area_name=old_area_name)
            self._update_area_inputs_yaml(area_id)
            self._update_area_automations_yaml(area_id)
            self._update_area_switches_yaml(area_id)
            new_area = self.registry.get_area(area_id)
            if old_area:
                self._delete_removed_area_yaml(old_area, new_area)

        if affected_area_ids or removed_area_ids:
            self._sync_global_light_group()
            self._sync_global_cover_group()
            presence_ok, motion_ok = self._ensure_presence_groups()
            self.check_rules_notify_automations(presence_ok=presence_ok, motion_ok=motion_ok)
            self._update_area_inputs_yaml("general")
            self._update_area_automations_yaml("general")
            self._update_area_switches_yaml("general")
            self.create_dashboards()

        self._log.ok("Registry updated from inventory diff.")
        return diff

    # ---------------------------
    # 2) Process + Update Registry
    # ---------------------------
    def process_and_update_registry(self):
        """Process cleaned inventory and update the registry."""
        self._log.start("Processing and updating registry.")

        if not self.cleaned_inventory_path.exists():
            raise FileNotFoundError("Cleaned inventory not found. Run fetch_and_clean() first.")

        detected_entities = self.inventory_processor.populate_entities_registry()

        with open(self.cleaned_inventory_path, "r", encoding="utf-8") as f:
            inventory = json.load(f)

        existing_area_ids = self.registry.get_area_ids()

        for area_id, entities in detected_entities.items():
            area_name, labels = None, []
            for area in inventory.get("areas", []):
                if area["area_id"] == area_id:
                    area_name = area.get("name")
                    labels = area.get("labels", [])
                    break

            if area_id not in existing_area_ids:
                self._log.info(f"New area detected: {area_name}")
                self.registry.add_area(area_id, area_name, labels)
                for e in entities:
                    self.registry.add_entity(area_id, e["entity_id"], e["device"], e["device_id"], e["types"], e["labels"])
            else:
                existing_entities = {e["entity_id"]: e for e in self.registry.list_entities(area_id)}
                detected_map = {e["entity_id"]: e for e in entities}

                if set(existing_entities.keys()) == set(detected_map.keys()):
                    self._log.skip(f"Area {area_name} unchanged; skipping entity update.")
                else:
                    self._log.info(f"Area {area_name} entities changed; updating registry.")
                    for eid, e in detected_map.items():
                        if eid not in existing_entities:
                            self.registry.add_entity(area_id, e["entity_id"], e["device"], e["device_id"], e["types"], e["labels"])
                    for eid in list(existing_entities.keys()):
                        if eid not in detected_map:
                            self.registry.delete_entity(area_id, eid)

        self._log.ok("Registry updated (areas + entities).")

    # ---------------------------
    # 3) Crear YAMLs desde registry
    # ---------------------------
    def create_input_context(self, input_entry: dict, area: dict) -> dict:
        """Build the template context for an input entry."""
        if input_entry["type"] == InputType.INPUT_NUMBER and "setpoint" in input_entry["id"]:
            area_name = area.get("name") or area.get("area_id") or "unknown"
            helper_id = input_entry.get("id") or build_object_id(area_name, "setpoint")
            helper_name = input_entry.get("name") or build_alias("Input Number", area_name, "Setpoint")
            try:
                min_temp = float(os.getenv("HAUSIE_CLIMATE_MIN_TEMP", "16"))
            except ValueError:
                min_temp = 16.0
            try:
                max_temp = float(os.getenv("HAUSIE_CLIMATE_MAX_TEMP", "30"))
            except ValueError:
                max_temp = 30.0
            initial = 22.0
            if "heat" in helper_id:
                try:
                    initial = float(os.getenv("HAUSIE_CLIMATE_HEAT_SETPOINT", "20"))
                except ValueError:
                    initial = 20.0
            elif "cool" in helper_id:
                try:
                    initial = float(os.getenv("HAUSIE_CLIMATE_COOL_SETPOINT", "24"))
                except ValueError:
                    initial = 24.0
            return {
                "helper_id": helper_id,
                "helper_name": helper_name,
                "min_temp": min_temp,
                "max_temp": max_temp,
                "step": 0.5,
                "unit": "C",
                "initial": initial,
            }
        if input_entry["type"] == InputType.INPUT_NUMBER and "notify" in (input_entry.get("id") or ""):
            area_name = area.get("name") or area.get("area_id") or "unknown"
            helper_id = input_entry.get("id") or build_object_id(area_name, "notify_value")
            helper_name = input_entry.get("name") or build_alias("Input Number", area_name, "Notify")
            unit = ""
            min_value = 0
            max_value = 100
            step = 1
            initial = 0
            if "battery_threshold" in helper_id:
                unit = "%"
                min_value = 0
                max_value = 100
                step = 1
                try:
                    initial = float(os.getenv("HAUSIE_NOTIFY_BATTERY_THRESHOLD", "20"))
                except ValueError:
                    initial = 20
            elif "door_open_minutes" in helper_id:
                unit = "min"
                min_value = 0
                max_value = 120
                step = 1
                try:
                    initial = float(os.getenv("HAUSIE_NOTIFY_DOOR_OPEN_MINUTES", "5"))
                except ValueError:
                    initial = 5
            elif "device_on_minutes" in helper_id:
                unit = "min"
                min_value = 0
                max_value = 120
                step = 1
                try:
                    initial = float(os.getenv("HAUSIE_NOTIFY_DEVICE_ON_MINUTES", "10"))
                except ValueError:
                    initial = 10
            return {
                "helper_id": helper_id,
                "helper_name": helper_name,
                "min_value": min_value,
                "max_value": max_value,
                "step": step,
                "unit": unit,
                "initial": initial,
            }
        if input_entry["type"] == InputType.INPUT_BOOLEAN:
            return {"automation_name": input_entry["name"], "initial_state": "on"}
        if input_entry["type"] == InputType.CLIMATE:
            return {"automation_name": input_entry["name"], "min_temp": 16, "max_temp": 28, "initial": 22, "step": 0.5}
        if input_entry["type"] == InputType.INPUT_SELECT:
            options = self._get_script_device_options(area["area_id"])
            return {"automation_name": input_entry["name"], "toggle_options": options}
        return {"automation_name": input_entry["name"]}

    def create_switch_context(self, switch_entry: dict, area: dict) -> dict:
        """Build the template context for a switch entry."""
        switch_id = switch_entry.get("id")
        name = switch_entry.get("name") or switch_id or "Switch"
        switch_type = switch_entry.get("type") or switch_id
        data = switch_entry.get("data") if isinstance(switch_entry.get("data"), dict) else {}

        if switch_type == "automation":
            automation_id = (data.get("automation_id") or "").strip()
            if not automation_id and switch_id:
                automation_id = switch_id
                if automation_id.endswith("_switch"):
                    automation_id = automation_id[:-7]
            return {
                "switch_id": switch_id,
                "switch_name": titleize(switch_id or ""),
                "friendly_name": name,
                "icon_template": "mdi:robot",
                "automation_id": automation_id,
            }

        if switch_type == "tailscale":
            addon_slug = (data.get("addon_slug") or os.getenv("TAILSCALE_ADDON_SLUG", "a0d7b954_tailscale")).strip()
            secondary_addon_slug = (data.get("secondary_addon_slug") or "").strip()
            icon_template = (data.get("icon_template") or "mdi:vpn").strip() or "mdi:vpn"
            return {
                "switch_id": switch_id,
                "switch_name": titleize(switch_id or ""),
                "friendly_name": name,
                "icon_template": icon_template,
                "addon_slug": addon_slug,
                "secondary_addon_slug": secondary_addon_slug,
            }

        return {
            "switch_id": switch_id,
            "switch_name": titleize(switch_id or ""),
            "friendly_name": name,
            "icon_template": "mdi:toggle-switch",
        }

    def _get_script_device_options(self, area_id: str) -> List[str]:
        """Return device/group names for options tied to scripts in the registry."""
        area = self.registry.get_area(area_id)
        allowed_group_names = self._get_allowed_group_names(area) if area else None
        options = set()
        entities = self.registry.list_entities(area_id)
        entities_by_id = {e.get("entity_id"): e for e in entities if e.get("entity_id")}
        for script in self.registry.list_scripts(area_id):
            device_name = script.get("device_name")
            if device_name:
                normalized = normalize_option_label(device_name)
                if normalized:
                    options.add(normalized)
                continue
            for entity_id in script.get("entities", []) or []:
                ent = entities_by_id.get(entity_id)
                fallback_name = ent.get("device") if ent else None
                if fallback_name:
                    normalized = normalize_option_label(fallback_name)
                    if normalized:
                        options.add(normalized)
        for group in self.registry.list_groups(area_id):
            group_name = group.get("name")
            if group_name and (allowed_group_names is None or group_name in allowed_group_names):
                normalized = normalize_option_label(group_name)
                if normalized:
                    options.add(normalized)
        return sorted(options, key=str.lower)

    def _get_allowed_group_names(self, area: dict | None) -> set[str]:
        """Return allowed group names, applying the lights-group rule."""
        if not area:
            return set()
        groups = [g for g in area.get("groups", []) if g.get("id") and g.get("name")]
        if not groups:
            return set()

        area_name = area.get("name") or area.get("area_id") or "unknown"
        all_id = self._prefixed_id("core", area_name, "lights")
        primary_id = self._prefixed_id("core", area_name, "primary_lights")
        secondary_id = self._prefixed_id("core", area_name, "secondary_lights")

        has_all = any(g["id"] == all_id for g in groups)
        has_primary = any(g["id"] == primary_id for g in groups)
        has_secondary = any(g["id"] == secondary_id for g in groups)

        allowed = set()
        for group in groups:
            group_id = group["id"]
            group_name = group["name"]
            if group_id in {all_id, primary_id, secondary_id}:
                if not has_all:
                    continue
                if has_primary and has_secondary:
                    allowed.add(group_name)
                elif group_id == all_id:
                    allowed.add(group_name)
            else:
                allowed.add(group_name)
        return allowed

    def create_automation_context(self, automation_entry: dict, area: dict) -> dict:
        """Build the template context for an automation entry."""
        area_name = slugify(area["name"])
        area_id = area["area_id"]
        automation_id = automation_entry.get("id") or ""

        if automation_entry["id"].endswith("_light"):
            motion_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.MOTION)]
            lights = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LIGHT)]
            lux_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LUX)]
            motion_to_lux = {m: lux_sensors[0] for m in motion_sensors if lux_sensors}
            return {
                "automation_id": automation_id,
                "area_name": area_name,
                "motion_sensors": motion_sensors,
                "lights": lights,
                "motion_to_lux": motion_to_lux,
                "automation_alias": build_alias("Auto", area["name"], "Light"),
            }

        if automation_entry["id"].endswith("_blinds_weekday") or automation_entry["id"].endswith("_blinds_weekend"):
            schedule_type = "weekday" if automation_entry["id"].endswith("_blinds_weekday") else "weekend"
            blinds = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COVER)]
            return {
                "automation_id": automation_id,
                "area_name": area_name,
                "blinds": blinds,
                "schedule_type": schedule_type,
                "days": ["mon", "tue", "wed", "thu", "fri"] if schedule_type == "weekday" else ["sat", "sun"],
                  "blinds_up": f"{InputType.INPUT_DATETIME}.{self._prefixed_id('auto', area['name'], f'blinds_up_{schedule_type}')}",
                  "blinds_down": f"{InputType.INPUT_DATETIME}.{self._prefixed_id('auto', area['name'], f'blinds_down_{schedule_type}')}",
                "automation_alias": build_alias(
                    "Auto",
                    area["name"],
                    "Blinds Weekday" if schedule_type == "weekday" else "Blinds Weekend",
                ),
            }

        if automation_entry["id"].endswith("_climate"):
            temp_sensor = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.TEMPERATURE)), None)
            heat_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.HEATING)), None)
            cool_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COOLING)), None)
            try:
                min_temp = float(os.getenv("HAUSIE_CLIMATE_MIN_TEMP", "20"))
            except ValueError:
                min_temp = 20.0
            try:
                max_temp = float(os.getenv("HAUSIE_CLIMATE_MAX_TEMP", "24"))
            except ValueError:
                max_temp = 24.0
            try:
                heat_initial = float(os.getenv("HAUSIE_CLIMATE_HEAT_SETPOINT", "20"))
            except ValueError:
                heat_initial = 20.0
            try:
                cool_initial = float(os.getenv("HAUSIE_CLIMATE_COOL_SETPOINT", "24"))
            except ValueError:
                cool_initial = 24.0
            heat_setpoint = f"{InputType.INPUT_NUMBER}.{self._prefixed_id('auto', area['name'], 'heat_setpoint')}"
            cool_setpoint = f"{InputType.INPUT_NUMBER}.{self._prefixed_id('auto', area['name'], 'cool_setpoint')}"
            return {
                "automation_id": automation_id,
                "area_name": area_name,
                "temp_sensor": temp_sensor,
                "heat_switch": heat_switch,
                "cool_switch": cool_switch,
                "min_temp": min_temp,
                "max_temp": max_temp,
                "heat_setpoint_initial": heat_initial,
                "cool_setpoint_initial": cool_initial,
                "heat_setpoint": heat_setpoint,
                "cool_setpoint": cool_setpoint,
                "automation_alias": build_alias("Auto", area["name"], "Climate"),
            }

        if automation_entry["id"].endswith("_button"):
            button_entities = self.registry.get_entities_by_type(area_id, EntityType.BUTTON)
            if not button_entities:
                button_entities = [
                    e for e in self.registry.list_entities(area_id)
                    if Labels.BUTTON in (e.get("labels") or [])
                ]
            button_entity = next((e.get("entity_id") for e in button_entities), None)
            button_device_id = next((e.get("device_id") for e in button_entities if e.get("device_id")), None)
            button_subtype = next((e.get("button_subtype") for e in button_entities if e.get("button_subtype")), None)
            if not button_subtype:
                button_subtype = "button_1"
            script_map = {}
            allowed_group_names = self._get_allowed_group_names(area)
            for script in self.registry.list_scripts(area_id):
                device_name = script.get("device_name")
                script_id = script.get("id")
                if device_name and script_id:
                    normalized = normalize_option_label(device_name)
                    if normalized:
                        script_map[normalized] = f"script.{script_id}"
            for group in self.registry.list_groups(area_id):
                group_name = group.get("name")
                group_id = group.get("id")
                if group_name and group_id and group_name in allowed_group_names:
                    group_subject = group_id
                    area_slug = slugify(area["name"])
                    core_prefix = f"core_{area_slug}_"
                    prefix = f"{area_slug}_"
                    if group_subject.startswith(core_prefix):
                        group_subject = group_subject[len(core_prefix):]
                    elif group_subject.startswith(prefix):
                        group_subject = group_subject[len(prefix):]
                    script_id = self._prefixed_id("core", area["name"], f"group_{group_subject}")
                    normalized = normalize_option_label(group_name)
                    if normalized:
                        script_map.setdefault(normalized, f"script.{script_id}")
            return {
                "automation_id": automation_id,
                "area_name": area_name,
                "button_action_entity": button_entity,
                "button_device_id": button_device_id,
                "button_subtype": button_subtype,
                "script_map_json": json.dumps(script_map, ensure_ascii=False),
                "automation_alias": build_alias("Auto", area["name"], "Button"),
            }

        if "notify_low_battery" in automation_entry["id"]:
            battery_entities = []
            for area_entry in self.registry.list_areas():
                area_entry_id = area_entry.get("area_id")
                area_entry_name = area_entry.get("name") or area_entry_id or ""
                if slugify(area_entry_name) == "system":
                    continue
                if not area_entry_id:
                    continue
                battery_entities.extend(
                    [
                        e.get("entity_id")
                        for e in self.registry.get_entities_by_type(area_entry_id, EntityType.BATTERY)
                        if isinstance(e, dict) and e.get("entity_id")
                    ]
                )
            toggle_entity = f"{InputType.INPUT_BOOLEAN}.auto_notify_low_battery_toggle"
            triggers = []
            for entity_id in battery_entities:
                triggers.append(
                    {
                        "platform": "state",
                        "entity_id": entity_id,
                    }
                )
            conditions = [
                {"condition": "state", "entity_id": toggle_entity, "state": "on"},
                {
                    "condition": "template",
                    "value_template": (
                        "{{ trigger.to_state is not none and "
                        "trigger.to_state.state not in ['unavailable','unknown','none','None',''] }}"
                    ),
                },
                {
                    "condition": "template",
                    "value_template": (
                        "{{ trigger.to_state.state | float(0) < 15 }}"
                    ),
                },
            ]
            actions = [
                {
                    "service": "persistent_notification.create",
                    "data": {
                        "title": "Battery low",
                        "notification_id": (
                            "{{ 'low_battery_' ~ (trigger.entity_id | replace('.', '_')) }}"
                        ),
                        "message": (
                            "{{ trigger.to_state.name or trigger.to_state.entity_id }} "
                            "in {{ area_name(trigger.entity_id) or 'Unknown area' }} "
                            "is below 15% battery ({{ trigger.to_state.state }}%)"
                        ),
                    },
                }
            ]
            return {
                "automation_id": automation_id,
                "automation_alias": "Auto - Notify Low Battery",
                "triggers_yaml": yaml.safe_dump(triggers, sort_keys=False, allow_unicode=True).rstrip(),
                "conditions_yaml": yaml.safe_dump(conditions, sort_keys=False, allow_unicode=True).rstrip(),
                "actions_yaml": yaml.safe_dump(actions, sort_keys=False, allow_unicode=True).rstrip(),
            }

        if "notify_unavailable_devices" in automation_entry["id"]:
            entity_device_map: dict[str, str] = {}
            entities_by_device: dict[str, list[str]] = {}
            for area_entry in self.registry.list_areas():
                area_entry_id = area_entry.get("area_id")
                area_entry_name = area_entry.get("name") or area_entry_id or ""
                if not area_entry_id or slugify(area_entry_name) == "system":
                    continue
                for ent in self.registry.list_entities(area_entry_id):
                    if not isinstance(ent, dict):
                        continue
                    entity_id = ent.get("entity_id")
                    device_id = ent.get("device_id")
                    if not entity_id or not device_id:
                        continue
                    entity_device_map[entity_id] = device_id
                    entities_by_device.setdefault(device_id, []).append(entity_id)

            main_entities = {
                min(entity_ids, key=len)
                for entity_ids in entities_by_device.values()
                if entity_ids
            }
            main_entities_json = json.dumps(sorted(main_entities), ensure_ascii=False)
            entity_device_map_json = json.dumps(entity_device_map, ensure_ascii=False)

            toggle_entity = f"{InputType.INPUT_BOOLEAN}.auto_notify_unavailable_devices_toggle"
            triggers = [
                {
                    "platform": "event",
                    "event_type": "state_changed",
                }
            ]
            conditions = [
                {"condition": "state", "entity_id": toggle_entity, "state": "on"},
                {
                    "condition": "template",
                    "value_template": (
                        f"{{% set main_entities = {main_entities_json} %}}\n"
                        "{% set ns = trigger.event.data.new_state %}\n"
                        "{% set os = trigger.event.data.old_state %}\n"
                        "{% set entity_id = trigger.event.data.entity_id %}\n"
                        "{% if not ns or not os %}\n"
                        "false\n"
                        "{% else %}\n"
                        "{% set domain = entity_id.split('.')[0] %}\n"
                        "{{ entity_id in main_entities and "
                        "ns.state in ['unavailable','unknown'] and os.state not in ['unavailable','unknown'] "
                        "and domain not in ['automation','script','input_boolean','input_button','input_number',"
                        "'input_select','input_text','input_datetime','scene','group'] }}\n"
                        "{% endif %}"
                    ),
                },
            ]
            actions = [
                {
                    "service": "persistent_notification.create",
                    "data": {
                        "title": "Device unavailable",
                        "notification_id": (
                            f"{{% set entity_device_map = {entity_device_map_json} %}}\n"
                            "{% set entity_id = trigger.event.data.entity_id %}\n"
                            "{% set device_id = entity_device_map.get(entity_id, entity_id) %}\n"
                            "{{ 'device_unavailable_' ~ (device_id | replace('.', '_')) }}"
                        ),
                        "message": (
                            "{% set entity_id = trigger.event.data.entity_id %}\n"
                            "{% set entity_name = trigger.event.data.new_state.name or entity_id %}\n"
                            "{% set area = area_name(entity_id) %}\n"
                            "{{ entity_name }} of {{ area or 'Unknown area' }} is {{ trigger.event.data.new_state.state }}."
                        ),
                    },
                }
            ]
            return {
                "automation_id": automation_id,
                "automation_alias": "Auto - Notify Unavailable Devices",
                "triggers_yaml": yaml.safe_dump(triggers, sort_keys=False, allow_unicode=True).rstrip(),
                "conditions_yaml": yaml.safe_dump(conditions, sort_keys=False, allow_unicode=True).rstrip(),
                "actions_yaml": yaml.safe_dump(actions, sort_keys=False, allow_unicode=True).rstrip(),
            }

        if "notify_away_entities" in automation_entry["id"]:
            door_entities = []
            device_entities = []
            for area_entry in self.registry.list_areas():
                area_entry_id = area_entry.get("area_id")
                area_entry_name = area_entry.get("name") or area_entry_id or ""
                if slugify(area_entry_name) == "system":
                    continue
                if not area_entry_id:
                    continue
                device_entities.extend(self._get_left_on_device_entities(area_entry_id))
            device_minutes_entity = f"{InputType.INPUT_NUMBER}.auto_notify_device_on_minutes"
            triggers = []
            for entity_id in device_entities:
                triggers.append(
                    {
                        "platform": "state",
                        "entity_id": entity_id,
                        "to": "on",
                        "for": {"minutes": f"{{{{ states('{device_minutes_entity}') | int(10) }}}}"},
                    }
                )
            toggle_entity = f"{InputType.INPUT_BOOLEAN}.auto_notify_away_toggle"
            conditions = [
                {"condition": "state", "entity_id": toggle_entity, "state": "on"},
                {"condition": "template", "value_template": "{{ states('group.core_presence_devices') != 'home' }}"},
                {"condition": "template", "value_template": "{{ states('group.core_motion_sensors') == 'off' }}"},
            ]
            actions = [
                {
                    "service": "rest_command.notify_admins",
                    "data": {
                        "title": "Hausie Alert",
                        "message": (
                            "{{ trigger.to_state.name or trigger.to_state.entity_id }} "
                            "is on while nobody is home."
                        ),
                    },
                }
            ]
            return {
                "automation_id": automation_id,
                "automation_alias": "Auto - Notify Away Entities",
                "triggers_yaml": yaml.safe_dump(triggers, sort_keys=False, allow_unicode=True).rstrip(),
                "conditions_yaml": yaml.safe_dump(conditions, sort_keys=False, allow_unicode=True).rstrip(),
                "actions_yaml": yaml.safe_dump(actions, sort_keys=False, allow_unicode=True).rstrip(),
            }

        if "notify_open_door_window" in automation_entry["id"]:
            door_entities = []
            for area_entry in self.registry.list_areas():
                area_entry_id = area_entry.get("area_id")
                area_entry_name = area_entry.get("name") or area_entry_id or ""
                if slugify(area_entry_name) == "system":
                    continue
                if not area_entry_id:
                    continue
                door_entities.extend(self._get_door_entities(area_entry_id))
            door_minutes_entity = f"{InputType.INPUT_NUMBER}.auto_notify_door_open_minutes"
            triggers = []
            for entity_id in door_entities:
                triggers.append(
                    {
                        "platform": "state",
                        "entity_id": entity_id,
                        "to": "on",
                        "for": {"minutes": f"{{{{ states('{door_minutes_entity}') | int(5) }}}}"},
                    }
                )
            toggle_entity = f"{InputType.INPUT_BOOLEAN}.auto_notify_open_door_window_toggle"
            conditions = [
                {"condition": "state", "entity_id": toggle_entity, "state": "on"},
                {"condition": "template", "value_template": "{{ states('group.core_presence_devices') != 'home' }}"},
                {"condition": "template", "value_template": "{{ states('group.core_motion_sensors') == 'off' }}"},
            ]
            actions = [
                {
                    "service": "rest_command.notify_admins",
                    "data": {
                        "title": "Hausie Alert",
                        "message": (
                            "{{ trigger.to_state.name or trigger.to_state.entity_id }} "
                            "is open while nobody is home."
                        ),
                    },
                }
            ]
            return {
                "automation_id": automation_id,
                "automation_alias": "Auto - Notify Open Door Window",
                "triggers_yaml": yaml.safe_dump(triggers, sort_keys=False, allow_unicode=True).rstrip(),
                "conditions_yaml": yaml.safe_dump(conditions, sort_keys=False, allow_unicode=True).rstrip(),
                "actions_yaml": yaml.safe_dump(actions, sort_keys=False, allow_unicode=True).rstrip(),
            }

        return {
            "automation_id": automation_id,
            "area_name": area_name,
            "automation_alias": build_alias("Auto", area["name"], "Automation"),
        }

    def create_from_registry(self):
        """Generate inputs and automations from the registry."""
        self._log.start("Creating YAMLs from registry.")
        for area in self.registry.list_areas():
            area_id = area["area_id"]
            area_name = area.get("name") or area_id or "unknown"
            for i in self.registry.list_inputs(area_id):
                context = self.create_input_context(i, area)
                self.helper_manager.create(
                    area_name,
                    i,
                    context=context,
                    template_file=self._resolve_input_template(i),
                )
            for a in self.registry.list_automations(area_id):
                context = self.create_automation_context(a, area)
                self.automation_manager.create(
                    a,
                    context=context,
                    template_file=self._resolve_automation_template(a),
                )
            for s in self.registry.list_switches(area_id):
                context = self.create_switch_context(s, area)
                self.switch_manager.create(
                    area_name,
                    s,
                    context=context,
                    template_file=self._resolve_switch_template(s),
                )
        self._log.ok("YAMLs created.")

    def create_switches_from_registry(self) -> None:
        """Generate switch YAMLs from the registry."""
        self._log.start("Creating switches from registry.")
        for area in self.registry.list_areas():
            area_id = area["area_id"]
            area_name = area.get("name") or area_id or "unknown"
            for s in self.registry.list_switches(area_id):
                context = self.create_switch_context(s, area)
                self.switch_manager.create(
                    area_name,
                    s,
                    context=context,
                    template_file=self._resolve_switch_template(s),
                )

    def create_scripts_from_registry(self):
        """Create scripts for actionable entities and light groups."""
        self.create_groups_from_registry()
        self.create_group_scripts_from_registry()
        self.create_entity_scripts_from_registry()

    def create_group_scripts_from_registry(self):
        """Create scripts for each group in the registry."""
        created = set()
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            area_name = area.get("name") or area_id or "unknown"
            area_slug = slugify(area_name)
            for group in self.registry.list_groups(area_id):
                group_id = group.get("id")
                group_name = group.get("name")
                if not group_id or not group_name:
                    continue
                group_subject = group_id
                core_prefix = f"core_{area_slug}_"
                prefix = f"{area_slug}_"
                if group_subject.startswith(core_prefix):
                    group_subject = group_subject[len(core_prefix):]
                elif group_subject.startswith(prefix):
                    group_subject = group_subject[len(prefix):]
                subject = f"group_{group_subject}"
                script_id = unique_slug(self._prefixed_id("core", area_name, subject), created)
                script_name = build_alias("Script", area_name, subject)
                group_entity = f"group.{group_id}"
                self.registry.add_script(area_id, script_id, script_name, [group_entity], group_name)
                self.script_manager.create_toggle_script(area_name, script_id, script_name, [group_entity])
                created.add(script_id)

        global_group_id = self._prefixed_id("core", "general", "lights")
        global_group_name = build_alias("Group", "General", "Lights")
        global_group_file = self.yaml_updater.group_creator.groups_dir / build_filename("group", "general")
        if global_group_file.exists():
            data = yaml.safe_load(global_group_file.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and global_group_id in data:
                subject = "group_lights"
                script_id = unique_slug(self._prefixed_id("core", "general", subject), created)
                if script_id not in created:
                    script_name = build_alias("Script", "General", subject)
                    group_entity = f"group.{global_group_id}"
                    self.script_manager.create_toggle_script(None, script_id, script_name, [group_entity])

    def create_entity_scripts_from_registry(self):
        """Create scripts for entities that match the configured label rules."""
        script_label_pool: list[str] = ["light","cooling","heating"]
        allowed_labels = set(script_label_pool) - {"main"}
        if not allowed_labels:
            self._log.skip("No script labels configured; skipping script creation.")
            return

        created = set()
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            area_name = area.get("name") or area_id or "unknown"
            area_slug = slugify(area_name)
            entities = self.registry.list_entities(area_id)
            for ent in entities:
                labels = ent.get("labels", [])
                types = ent.get("types", [])
                if not isinstance(labels, list):
                    continue
                if not isinstance(types, list):
                    continue
                label_set = set(labels)
                type_set = set(types)
                if EntityType.MAIN not in type_set:
                    continue
                if not (label_set.intersection(allowed_labels) or type_set.intersection(allowed_labels)):
                    continue
                entity_id = ent.get("entity_id")
                if not entity_id:
                    continue
                device_name = ent.get("device") or entity_id
                base_id = self._prefixed_id("core", area_name, device_name)
                script_id = unique_slug(base_id, created)
                script_name = build_alias("Script", area_name, device_name)
                self.registry.add_script(area_id, script_id, script_name, [entity_id], device_name)
                self.script_manager.create_toggle_script(area_name, script_id, script_name, [entity_id])
                created.add(script_id)

    def _get_entities_by_labels(self, area_entities: list[dict], labels: set[str]) -> list[str]:
        """Return entity ids that include any of the labels."""
        entity_ids = []
        for ent in area_entities:
            ent_labels = ent.get("labels", [])
            if isinstance(ent_labels, list) and labels.intersection(ent_labels):
                entity_id = ent.get("entity_id")
                if entity_id:
                    entity_ids.append(entity_id)
        return entity_ids

    def _get_light_entities_for_area(self, area_id: str) -> list[str]:
        """Return light entity ids for an area (fallback when no labels exist)."""
        lights = [
            e.get("entity_id")
            for e in self.registry.get_entities_by_type(area_id, EntityType.LIGHT)
            if isinstance(e, dict) and e.get("entity_id")
        ]
        if lights:
            return lights
        return [
            e.get("entity_id")
            for e in self.registry.list_entities(area_id)
            if isinstance(e, dict)
            and isinstance(e.get("entity_id"), str)
            and e["entity_id"].startswith("light.")
        ]

    def _get_all_light_entities(self) -> list[str]:
        """Return light entity ids across all areas."""
        all_lights: list[str] = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            all_lights.extend(self._get_light_entities_for_area(area_id))
        return all_lights

    def _get_cover_entities_for_area(self, area_id: str) -> list[str]:
        """Return main cover entity ids for an area."""
        covers = [
            e.get("entity_id")
            for e in self.registry.get_entities_by_type(area_id, EntityType.COVER)
            if isinstance(e, dict)
            and e.get("entity_id")
            and EntityType.MAIN in (e.get("types") or [])
        ]
        return sorted(dict.fromkeys(covers))

    @staticmethod
    def _area_has_covers(area: dict | None) -> bool:
        """Return whether an area contains any main cover entity."""
        if not isinstance(area, dict):
            return False
        for ent in area.get("entities", []) or []:
            if not isinstance(ent, dict):
                continue
            entity_id = str(ent.get("entity_id") or "")
            types = ent.get("types") or []
            if entity_id.startswith("cover.") and EntityType.MAIN in types and EntityType.COVER in types:
                return True
        return False

    def _sync_cover_group_for_area(self, area_id: str) -> None:
        """Create or delete the grouped cover file for one area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"
        blinds = self._get_cover_entities_for_area(area_id)
        if not blinds:
            self.cover_manager.delete(area_name)
            return

        unique_id = self._prefixed_id("core", area_name, "blinds")
        self.cover_manager.update_group(
            area_name,
            unique_id=unique_id,
            group_name=titleize(unique_id),
            entities=blinds,
        )

    def _sync_cover_groups_from_registry(self) -> None:
        """Sync grouped cover files for all areas that contain blinds."""
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id or not self._area_has_covers(area):
                continue
            self._sync_cover_group_for_area(area_id)

    def _sync_global_cover_group(self) -> None:
        """Sync the global grouped cover file."""
        global_blinds: list[str] = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            global_blinds.extend(self._get_cover_entities_for_area(area_id))
        global_blinds = sorted(dict.fromkeys(global_blinds))
        if global_blinds:
            self.cover_manager.update_group(
                "general",
                unique_id=self._prefixed_id("core", "general", "blinds"),
                group_name="All Blinds",
                entities=global_blinds,
                output_filename=build_filename("cover", "general"),
            )
        else:
            self.cover_manager.delete("general")

    def create_groups_from_registry(self):
        """Generate light groups per area using labels."""
        self._log.start("Creating groups from registry.")
        all_primary = []
        all_secondary = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            area_name = area.get("name") or area_id or "unknown"
            area_slug = slugify(area_name)

            entities = self.registry.list_entities(area_id)

            primary_label = Labels.PRIMARY_LIGHT
            secondary_label = "secondary_lights"
            primary_group_id = self._prefixed_id("core", area_name, "primary_lights")
            secondary_group_id = self._prefixed_id("core", area_name, "secondary_lights")
            all_group_id = self._prefixed_id("core", area_name, "lights")

            primary_lights = self._get_entities_by_labels(
                entities,
                {Labels.PRIMARY_LIGHT, "primary_lights"},
            )
            secondary_lights = self._get_entities_by_labels(
                entities,
                {Labels.SECONDARY_LIGHT, "secondary_lights"},
            )
            all_lights = sorted(set(primary_lights + secondary_lights))
            if not all_lights:
                all_lights = sorted(set(self._get_light_entities_for_area(area_id)))
            all_primary.extend(primary_lights)
            all_secondary.extend(secondary_lights)

            if primary_lights:
                self.registry.add_group(
                    area_id,
                    primary_group_id,
                    build_alias("Group", area_name, "Primary Lights"),
                    sorted(set(primary_lights)),
                )
                self.group_manager.create_light_group(
                    area_name,
                    group_id=primary_group_id,
                    group_name=build_alias("Group", area_name, "Primary Lights"),
                    entities=sorted(set(primary_lights)),
                )
            else:
                self._log.skip(f"No primary_light labels found for area: {area_name}")

            if secondary_lights:
                self.registry.add_group(
                    area_id,
                    secondary_group_id,
                    build_alias("Group", area_name, "Secondary Lights"),
                    sorted(set(secondary_lights)),
                )
                self.group_manager.create_light_group(
                    area_name,
                    group_id=secondary_group_id,
                    group_name=build_alias("Group", area_name, "Secondary Lights"),
                    entities=sorted(set(secondary_lights)),
                )
            else:
                self._log.skip(f"No secondary_light(s) labels found for area: {area_name}")

            if all_lights:
                self.registry.add_group(
                    area_id,
                    all_group_id,
                    build_alias("Group", area_name, "Lights"),
                    all_lights,
                )
                self.group_manager.create_light_group(
                    area_name,
                    group_id=all_group_id,
                    group_name=build_alias("Group", area_name, "Lights"),
                    entities=all_lights,
                )
            else:
                self._log.skip(f"No labeled lights found for area: {area_name}")

        global_lights = sorted(set(all_primary + all_secondary))
        if not global_lights:
            global_lights = sorted(set(self._get_all_light_entities()))
        if global_lights:
            self.group_manager.create_light_group(
                "general",
                group_id=self._prefixed_id("core", "general", "lights"),
                group_name=build_alias("Group", "General", "Lights"),
                entities=global_lights,
                output_filename=build_filename("group", "general"),
            )
        else:
            self._log.skip("No labeled lights found for global group.")

    def _sync_groups_for_area(self, area_id: str) -> None:
        """Sync light groups for a single area without wiping the area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"
        entities = self.registry.list_entities(area_id)

        primary_lights = self._get_entities_by_labels(
            entities,
            {Labels.PRIMARY_LIGHT, "primary_lights"},
        )
        secondary_lights = self._get_entities_by_labels(
            entities,
            {Labels.SECONDARY_LIGHT, "secondary_lights"},
        )
        all_lights = sorted(set(primary_lights + secondary_lights))
        if not all_lights:
            all_lights = sorted(set(self._get_light_entities_for_area(area_id)))

        desired_groups = []
        if primary_lights:
            desired_groups.append({
                "id": self._prefixed_id("core", area_name, "primary_lights"),
                "name": build_alias("Group", area_name, "Primary Lights"),
                "entities": sorted(set(primary_lights)),
            })
        if secondary_lights:
            desired_groups.append({
                "id": self._prefixed_id("core", area_name, "secondary_lights"),
                "name": build_alias("Group", area_name, "Secondary Lights"),
                "entities": sorted(set(secondary_lights)),
            })
        if all_lights:
            desired_groups.append({
                "id": self._prefixed_id("core", area_name, "lights"),
                "name": build_alias("Group", area_name, "Lights"),
                "entities": all_lights,
            })

        desired_ids = {g["id"] for g in desired_groups}
        existing_ids = {
            g.get("id")
            for g in area.get("groups", [])
            if g.get("id")
        }

        for group in desired_groups:
            if group["id"] in existing_ids:
                self.registry.update_group(
                    area_id,
                    group["id"],
                    name=group["name"],
                    entities=group["entities"],
                )
            else:
                self.registry.add_group(
                    area_id,
                    group["id"],
                    group["name"],
                    group["entities"],
                )
            self.group_manager.update_light_group(
                area_name,
                group["id"],
                group["name"],
                group["entities"],
            )

        for group_id in existing_ids - desired_ids:
            self.registry.delete_group(area_id, group_id)

    def _is_group_script(self, script: dict) -> bool:
        entities = script.get("entities") or []
        return len(entities) == 1 and str(entities[0]).startswith("group.")

    def _should_create_entity_script(self, ent: dict) -> bool:
        labels = ent.get("labels", [])
        types = ent.get("types", [])
        if not isinstance(labels, list):
            return False
        if not isinstance(types, list):
            return False
        if EntityType.MAIN not in types:
            return False
        allowed = {"light", "cooling", "heating"}
        return bool(set(labels).intersection(allowed) or set(types).intersection(allowed))

    def _sync_group_scripts_for_area(self, area_id: str) -> set[str]:
        """Sync group scripts for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return set()
        area_name = area.get("name") or area_id or "unknown"
        area_slug = slugify(area_name)

        existing_scripts = list(area.get("scripts", []))
        existing_by_group = {}
        for script in existing_scripts:
            if not self._is_group_script(script):
                continue
            entities = script.get("entities") or []
            group_entity = entities[0] if entities else ""
            if "." in group_entity:
                group_id = group_entity.split(".", 1)[1]
                existing_by_group[group_id] = script

        created = {s.get("id") for s in existing_scripts if s.get("id")}
        desired_ids = set()

        for group in area.get("groups", []):
            group_id = group.get("id")
            group_name = group.get("name")
            if not group_id or not group_name:
                continue

            group_subject = group_id
            core_prefix = f"core_{area_slug}_"
            prefix = f"{area_slug}_"
            if group_subject.startswith(core_prefix):
                group_subject = group_subject[len(core_prefix):]
            elif group_subject.startswith(prefix):
                group_subject = group_subject[len(prefix):]
            subject = f"group_{group_subject}"

            existing = existing_by_group.get(group_id)
            if existing:
                script_id = existing.get("id")
            else:
                script_id = unique_slug(self._prefixed_id("core", area_name, subject), created)
            if not script_id:
                script_id = unique_slug(self._prefixed_id("core", area_name, subject), created)

            script_name = build_alias("Script", area_name, subject)
            group_entity = f"group.{group_id}"

            if script_id and script_id in created:
                self.registry.update_script(
                    area_id,
                    script_id,
                    name=script_name,
                    entities=[group_entity],
                    device_name=group_name,
                )
            else:
                self.registry.add_script(
                    area_id,
                    script_id,
                    script_name,
                    [group_entity],
                    group_name,
                )
                created.add(script_id)

            self.script_manager.update_toggle_script(
                area_name,
                script_id,
                script_name,
                [group_entity],
            )
            desired_ids.add(script_id)

        for script in existing_scripts:
            script_id = script.get("id")
            if not script_id:
                continue
            if self._is_group_script(script) and script_id not in desired_ids:
                self.registry.delete_script(area_id, script_id)

        return created

    def _sync_entity_scripts_for_area(self, area_id: str, created: set[str] | None = None) -> None:
        """Sync entity scripts for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"

        existing_scripts = list(area.get("scripts", []))
        existing_by_entity = {}
        for script in existing_scripts:
            if self._is_group_script(script):
                continue
            entities = script.get("entities") or []
            if len(entities) == 1:
                existing_by_entity[entities[0]] = script

        created = set(created or {s.get("id") for s in existing_scripts if s.get("id")})
        desired_ids = set()

        for ent in self.registry.list_entities(area_id):
            if not self._should_create_entity_script(ent):
                continue
            entity_id = ent.get("entity_id")
            if not entity_id:
                continue
            device_name = ent.get("device") or entity_id
            existing = existing_by_entity.get(entity_id)
            if existing:
                script_id = existing.get("id")
            else:
                script_id = unique_slug(self._prefixed_id("core", area_name, device_name), created)
            if not script_id:
                script_id = unique_slug(self._prefixed_id("core", area_name, device_name), created)

            script_name = build_alias("Script", area_name, device_name)
            if script_id and script_id in created:
                self.registry.update_script(
                    area_id,
                    script_id,
                    name=script_name,
                    entities=[entity_id],
                    device_name=device_name,
                )
            else:
                self.registry.add_script(
                    area_id,
                    script_id,
                    script_name,
                    [entity_id],
                    device_name,
                )
                created.add(script_id)

            self.script_manager.update_toggle_script(
                area_name,
                script_id,
                script_name,
                [entity_id],
            )
            desired_ids.add(script_id)

        for script in existing_scripts:
            script_id = script.get("id")
            if not script_id:
                continue
            if self._is_group_script(script):
                continue
            entities = script.get("entities") or []
            if len(entities) != 1:
                continue
            if script_id not in desired_ids:
                self.registry.delete_script(area_id, script_id)

    def _sync_rules_for_area(self, area_id: str, *, old_area_name: str | None = None) -> None:
        """Sync automations and inputs for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"

        def upsert_automation(automation_id: str, name: str) -> None:
            if self.registry.get_automation(area_id, automation_id):
                self.registry.update_automation(area_id, automation_id, name=name)
            else:
                self.registry.add_automation(area_id, automation_id, name)
            self._upsert_registry_switch(
                area_id,
                f"{automation_id}_switch",
                name,
                "automation",
                {"automation_id": automation_id},
            )

        def upsert_input(input_id: str, name: str, input_type: str) -> None:
            if self.registry.get_input(area_id, input_id):
                self.registry.update_input(area_id, input_id, name=name, input_type=input_type)
            else:
                self.registry.add_input(area_id, input_id, name, input_type)

        def delete_automation(automation_id: str) -> None:
            if self.registry.get_automation(area_id, automation_id):
                self.registry.delete_automation(area_id, automation_id)
            switch_id = f"{automation_id}_switch"
            if self.registry.get_switch(area_id, switch_id):
                self.registry.delete_switch(area_id, switch_id)

        def delete_input(input_id: str) -> None:
            if self.registry.get_input(area_id, input_id):
                self.registry.delete_input(area_id, input_id)

        motion_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.MOTION)]
        lights = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LIGHT)]
        lux_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LUX)]
        has_light_rule = bool(motion_sensors and lights and lux_sensors)

        light_auto_id = self._prefixed_id("auto", area_name, "light")
        light_inputs = [
            (self._prefixed_id("auto", area_name, "primary_lights_toggle"), "Input Boolean", "Primary Lights Toggle", InputType.INPUT_BOOLEAN),
            (self._prefixed_id("auto", area_name, "secondary_lights_toggle"), "Input Boolean", "Secondary Lights Toggle", InputType.INPUT_BOOLEAN),
            (self._prefixed_id("auto", area_name, "lux_threshold"), "Input Number", "Lux Threshold", InputType.INPUT_NUMBER),
            (self._prefixed_id("auto", area_name, "lights_delay"), "Input Number", "Lights Delay", InputType.INPUT_NUMBER),
        ]

        if has_light_rule:
            upsert_automation(light_auto_id, build_alias("Automation", area_name, "Light"))
            for input_id, type_name, subject, input_type in light_inputs:
                upsert_input(input_id, build_alias(type_name, area_name, subject), input_type)
        else:
            delete_automation(light_auto_id)
            for input_id, _, _, _ in light_inputs:
                delete_input(input_id)

        blinds = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COVER)]
        has_blinds_rule = bool(blinds)
        blinds_weekend_id = self._prefixed_id("auto", area_name, "blinds_weekend")
        blinds_weekday_id = self._prefixed_id("auto", area_name, "blinds_weekday")
        blinds_inputs = [
            (self._prefixed_id("auto", area_name, "blinds_times"), "Input Datetime", "Blinds Times", InputType.INPUT_DATETIME),
        ]

        if has_blinds_rule:
            upsert_automation(blinds_weekend_id, build_alias("Automation", area_name, "Blinds Weekend"))
            upsert_automation(blinds_weekday_id, build_alias("Automation", area_name, "Blinds Weekday"))
            for input_id, type_name, subject, input_type in blinds_inputs:
                upsert_input(input_id, build_alias(type_name, area_name, subject), input_type)
        else:
            delete_automation(blinds_weekend_id)
            delete_automation(blinds_weekday_id)
            for input_id, _, _, _ in blinds_inputs:
                delete_input(input_id)

        temp_sensor = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.TEMPERATURE)), None)
        heat_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.HEATING)), None)
        cool_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COOLING)), None)
        has_climate_rule = bool(temp_sensor and (heat_switch or cool_switch))

        climate_auto_id = self._prefixed_id("auto", area_name, "climate")
        climate_inputs = [
            (self._prefixed_id("auto", area_name, "cooling_thermostat"), "Climate", "Cooling Thermostat", InputType.CLIMATE),
            (self._prefixed_id("auto", area_name, "heating_thermostat"), "Climate", "Heating Thermostat", InputType.CLIMATE),
        ]

        if has_climate_rule:
            upsert_automation(climate_auto_id, build_alias("Automation", area_name, "Climate"))
            for input_id, type_name, subject, input_type in climate_inputs:
                upsert_input(input_id, build_alias(type_name, area_name, subject), input_type)
        else:
            delete_automation(climate_auto_id)
            for input_id, _, _, _ in climate_inputs:
                delete_input(input_id)

        button = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.BUTTON)), None)
        has_button_rule = bool(button)
        button_auto_id = self._prefixed_id("auto", area_name, "button")
        button_inputs = [
            (self._prefixed_id("auto", area_name, "button_single"), "Input Select", "Button Single", InputType.INPUT_SELECT),
            (self._prefixed_id("auto", area_name, "button_double"), "Input Select", "Button Double", InputType.INPUT_SELECT),
            (self._prefixed_id("auto", area_name, "button_long"), "Input Select", "Button Long", InputType.INPUT_SELECT),
        ]

        if has_button_rule:
            upsert_automation(button_auto_id, build_alias("Automation", area_name, "Button"))
            for input_id, type_name, subject, input_type in button_inputs:
                upsert_input(input_id, build_alias(type_name, area_name, subject), input_type)
        else:
            delete_automation(button_auto_id)
            for input_id, _, _, _ in button_inputs:
                delete_input(input_id)

        obsolete_inputs = [
            self._prefixed_id("auto", area_name, "light_toggle"),
            self._prefixed_id("auto", area_name, "blinds_weekday_toggle"),
            self._prefixed_id("auto", area_name, "blinds_weekend_toggle"),
            self._prefixed_id("auto", area_name, "climate_toggle"),
            self._prefixed_id("auto", area_name, "button_toggle"),
        ]
        for input_id in obsolete_inputs:
            delete_input(input_id)

        if old_area_name and old_area_name != area_name:
            old_ids = [
                self._prefixed_id("auto", old_area_name, "light"),
                self._prefixed_id("auto", old_area_name, "blinds_weekend"),
                self._prefixed_id("auto", old_area_name, "blinds_weekday"),
                self._prefixed_id("auto", old_area_name, "climate"),
                self._prefixed_id("auto", old_area_name, "button"),
                self._prefixed_id("auto", old_area_name, "light_toggle"),
                self._prefixed_id("auto", old_area_name, "primary_lights_toggle"),
                self._prefixed_id("auto", old_area_name, "secondary_lights_toggle"),
                self._prefixed_id("auto", old_area_name, "lux_threshold"),
                self._prefixed_id("auto", old_area_name, "lights_delay"),
                self._prefixed_id("auto", old_area_name, "blinds_weekday_toggle"),
                self._prefixed_id("auto", old_area_name, "blinds_weekend_toggle"),
                self._prefixed_id("auto", old_area_name, "blinds_times"),
                self._prefixed_id("auto", old_area_name, "climate_toggle"),
                self._prefixed_id("auto", old_area_name, "cooling_thermostat"),
                self._prefixed_id("auto", old_area_name, "heating_thermostat"),
                self._prefixed_id("auto", old_area_name, "button_toggle"),
                self._prefixed_id("auto", old_area_name, "button_single"),
                self._prefixed_id("auto", old_area_name, "button_double"),
                self._prefixed_id("auto", old_area_name, "button_long"),
            ]
            for automation_id in old_ids[:5]:
                delete_automation(automation_id)
            for input_id in old_ids[5:]:
                delete_input(input_id)

    def _update_area_inputs_yaml(self, area_id: str) -> None:
        """Update helper YAML for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"
        for input_entry in self.registry.list_inputs(area_id):
            context = self.create_input_context(input_entry, area)
            template_file = self._resolve_input_template(input_entry)
            self.helper_manager.update(area_name, input_entry, context=context, template_file=template_file)

    def _update_area_automations_yaml(self, area_id: str) -> None:
        """Update automation YAML for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        for automation_entry in self.registry.list_automations(area_id):
            context = self.create_automation_context(automation_entry, area)
            template_file = self._resolve_automation_template(automation_entry)
            self.automation_manager.update(automation_entry, context=context, template_file=template_file)

    def _update_area_switches_yaml(self, area_id: str) -> None:
        """Update switches YAML for a single area."""
        area = self.registry.get_area(area_id)
        if not area:
            return
        area_name = area.get("name") or area_id or "unknown"
        for switch_entry in self.registry.list_switches(area_id):
            context = self.create_switch_context(switch_entry, area)
            template_file = self._resolve_switch_template(switch_entry)
            self.switch_manager.update(area_name, switch_entry, context=context, template_file=template_file)

    def _delete_removed_area_yaml(self, old_area: dict, new_area: dict | None) -> None:
        """Delete YAML entries that no longer exist in the registry."""
        old_name = old_area.get("name") or old_area.get("area_id") or "unknown"
        new_name = new_area.get("name") if new_area else None
        area_name = old_name or new_name or "unknown"

        old_groups = {g.get("id") for g in old_area.get("groups", []) if g.get("id")}
        new_groups = {g.get("id") for g in (new_area or {}).get("groups", []) if g.get("id")}
        for group_id in old_groups - new_groups:
            self.yaml_manager.delete_group(group_id, area_name)

        if self._area_has_covers(old_area) and (old_name != new_name or not self._area_has_covers(new_area)):
            self.cover_manager.delete(old_name)

        old_scripts = {s.get("id") for s in old_area.get("scripts", []) if s.get("id")}
        new_scripts = {s.get("id") for s in (new_area or {}).get("scripts", []) if s.get("id")}
        for script_id in old_scripts - new_scripts:
            self.yaml_manager.delete_script(script_id, area_name)

        old_autos = {a.get("id") for a in old_area.get("automations", []) if a.get("id")}
        new_autos = {a.get("id") for a in (new_area or {}).get("automations", []) if a.get("id")}
        for automation_id in old_autos - new_autos:
            self.yaml_manager.delete_automation(automation_id=automation_id)

    def _sync_global_light_group(self) -> None:
        """Sync the global lights group file."""
        all_primary = []
        all_secondary = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            entities = self.registry.list_entities(area_id)
            primary_lights = self._get_entities_by_labels(
                entities,
                {Labels.PRIMARY_LIGHT, "primary_lights"},
            )
            secondary_lights = self._get_entities_by_labels(
                entities,
                {Labels.SECONDARY_LIGHT, "secondary_lights"},
            )
            all_primary.extend(primary_lights)
            all_secondary.extend(secondary_lights)

        global_lights = sorted(set(all_primary + all_secondary))
        if not global_lights:
            global_lights = sorted(set(self._get_all_light_entities()))
        group_id = self._prefixed_id("core", "general", "lights")
        group_name = build_alias("Group", "General", "Lights")
        if global_lights:
            self.group_manager.update_light_group(
                "general",
                group_id,
                group_name,
                global_lights,
                output_filename=build_filename("group", "general"),
            )
        else:
            self.group_manager.delete(group_id, area_name="general")

    # ---------------------------
    # 4) Reglas
    # ---------------------------
    def check_rules_light_automation(self, area: dict):
        """Evaluate light automation rules for an area."""
        area_id = area["area_id"]
        area_name = area.get("name", "unknown")
        area_slug = slugify(area_name)
        motion_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.MOTION)]
        lights = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LIGHT)]
        lux_sensors = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.LUX)]
        if motion_sensors and lights and lux_sensors:
            automation_id = self._prefixed_id("auto", area_name, "light")
            automation_name = build_alias("Automation", area_name, "Light")
            self.registry.add_automation(area_id, automation_id, automation_name)
            self._upsert_registry_switch(
                area_id,
                f"{automation_id}_switch",
                automation_name,
                "automation",
                {"automation_id": automation_id},
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "primary_lights_toggle"),
                build_alias("Input Boolean", area_name, "Primary Lights Toggle"),
                InputType.INPUT_BOOLEAN,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "secondary_lights_toggle"),
                build_alias("Input Boolean", area_name, "Secondary Lights Toggle"),
                InputType.INPUT_BOOLEAN,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "lux_threshold"),
                build_alias("Input Number", area_name, "Lux Threshold"),
                InputType.INPUT_NUMBER,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "lights_delay"),
                build_alias("Input Number", area_name, "Lights Delay"),
                InputType.INPUT_NUMBER,
            )
            self._log.ok(f"Light rule OK: {area_name}")
        else:
            self._log.skip(f"Light rule skipped for {area_name} - missing motion or lights")

    def check_rules_blinds_automation(self, area: dict):
        """Evaluate blinds automation rules for an area."""
        area_id = area["area_id"]
        area_name = area.get("name", "unknown")
        area_slug = slugify(area_name)
        blinds = [e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COVER)]
        if blinds:
            weekend_id = self._prefixed_id("auto", area_name, "blinds_weekend")
            weekend_name = build_alias("Automation", area_name, "Blinds Weekend")
            self.registry.add_automation(area_id, weekend_id, weekend_name)
            self._upsert_registry_switch(
                area_id,
                f"{weekend_id}_switch",
                weekend_name,
                "automation",
                {"automation_id": weekend_id},
            )
            weekday_id = self._prefixed_id("auto", area_name, "blinds_weekday")
            weekday_name = build_alias("Automation", area_name, "Blinds Weekday")
            self.registry.add_automation(area_id, weekday_id, weekday_name)
            self._upsert_registry_switch(
                area_id,
                f"{weekday_id}_switch",
                weekday_name,
                "automation",
                {"automation_id": weekday_id},
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "blinds_times"),
                build_alias("Input Datetime", area_name, "Blinds Times"),
                InputType.INPUT_DATETIME,
            )
            self._log.ok(f"Blinds rule OK: {area_slug}")

    def check_rules_climate_automation(self, area: dict):
        """Evaluate climate automation rules for an area."""
        area_id = area["area_id"]
        area_name = area.get("name", "unknown")
        area_slug = slugify(area_name)
        temp_sensor = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.TEMPERATURE)), None)
        heat_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.HEATING)), None)
        cool_switch = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.COOLING)), None)
        if temp_sensor and (heat_switch or cool_switch):
            automation_id = self._prefixed_id("auto", area_name, "climate")
            automation_name = build_alias("Automation", area_name, "Climate")
            self.registry.add_automation(area_id, automation_id, automation_name)
            self._upsert_registry_switch(
                area_id,
                f"{automation_id}_switch",
                automation_name,
                "automation",
                {"automation_id": automation_id},
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "heat_setpoint"),
                build_alias("Input Number", area_name, "Heat Setpoint"),
                InputType.INPUT_NUMBER,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "cool_setpoint"),
                build_alias("Input Number", area_name, "Cool Setpoint"),
                InputType.INPUT_NUMBER,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "cooling_thermostat"),
                build_alias("Climate", area_name, "Cooling Thermostat"),
                InputType.CLIMATE,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "heating_thermostat"),
                build_alias("Climate", area_name, "Heating Thermostat"),
                InputType.CLIMATE,
            )
            self._log.ok(f"Climate rule OK: {area_slug}")

    def check_rules_buttons_automation(self, area: dict):
        """Evaluate button automation rules for an area."""
        area_id = area["area_id"]
        area_name = area.get("name", "unknown")
        area_slug = slugify(area_name)
        button = next((e["entity_id"] for e in self.registry.get_entities_by_type(area_id, EntityType.BUTTON)), None)
        if button :
            automation_id = self._prefixed_id("auto", area_name, "button")
            automation_name = build_alias("Automation", area_name, "Button")
            self.registry.add_automation(area_id, automation_id, automation_name)
            self._upsert_registry_switch(
                area_id,
                f"{automation_id}_switch",
                automation_name,
                "automation",
                {"automation_id": automation_id},
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "button_single"),
                build_alias("Input Select", area_name, "Button Single"),
                InputType.INPUT_SELECT,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "button_double"),
                build_alias("Input Select", area_name, "Button Double"),
                InputType.INPUT_SELECT,
            )
            self.registry.add_input(
                area_id,
                self._prefixed_id("auto", area_name, "button_long"),
                build_alias("Input Select", area_name, "Button Long"),
                InputType.INPUT_SELECT,
            )
            self._log.ok(f"Button rule OK: {area_slug}")

    def _ensure_presence_groups(self) -> tuple[bool, bool]:
        """Ensure global groups for presence devices and motion sensors."""
        self._ensure_registry_area("general", "General")
        device_entities: list[str] = []
        motion_entities: list[str] = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            for entity in self.registry.list_entities(area_id):
                if not isinstance(entity, dict):
                    continue
                entity_id = entity.get("entity_id")
                if not entity_id or "." not in entity_id:
                    continue
                labels = entity.get("labels") or []
                if Labels.DEVICES in labels and entity_id.startswith("device_tracker."):
                    device_entities.append(entity_id)
                if Labels.MOTION in labels and entity_id.startswith(("binary_sensor.", "sensor.")):
                    motion_entities.append(entity_id)

        presence_ready = bool(device_entities)
        motion_ready = bool(motion_entities)

        if device_entities:
            group_id = "core_presence_devices"
            self.registry.add_group("general", group_id, "Presence Devices", device_entities)
            self.group_manager.update_boolean_group(
                "General",
                group_id=group_id,
                group_name="Presence Devices",
                entities=device_entities,
                all_state=False,
                output_filename="general_groups.yaml",
            )
        if motion_entities:
            group_id = "core_motion_sensors"
            self.registry.add_group("general", group_id, "Motion Sensors", motion_entities)
            self.group_manager.update_boolean_group(
                "General",
                group_id=group_id,
                group_name="Motion Sensors",
                entities=motion_entities,
                all_state=False,
                output_filename="general_groups.yaml",
            )
        return presence_ready, motion_ready

    def _is_door_window_entity(self, entity: dict) -> bool:
        tokens = ("door", "window", "contact")
        entity_id = (entity.get("entity_id") or "").lower()
        if any(token in entity_id for token in tokens):
            return True
        for label in entity.get("labels") or []:
            label_text = str(label).lower()
            if "door" in label_text or "window" in label_text:
                return True
        return False

    def _get_door_entities(self, area_id: str) -> list[str]:
        out: list[str] = []
        for entity in self.registry.list_entities(area_id):
            if not isinstance(entity, dict):
                continue
            entity_id = entity.get("entity_id") or ""
            if not entity_id.startswith("binary_sensor."):
                continue
            if self._is_door_window_entity(entity):
                out.append(entity_id)
        return out

    def _get_device_on_entities(self, area_id: str) -> list[str]:
        domains = ("switch.", "light.", "fan.", "climate.")
        out: list[str] = []
        for entity in self.registry.list_entities(area_id):
            if not isinstance(entity, dict):
                continue
            entity_id = entity.get("entity_id") or ""
            if entity_id.startswith(domains):
                out.append(entity_id)
        return out

    def _get_left_on_device_entities(self, area_id: str) -> list[str]:
        """Return on/off devices excluding door/window labeled entities."""
        domains = ("switch.", "light.", "fan.", "climate.")
        out: list[str] = []
        for entity in self.registry.list_entities(area_id):
            if not isinstance(entity, dict):
                continue
            entity_id = entity.get("entity_id") or ""
            if not entity_id.startswith(domains):
                continue
            if self._is_door_window_entity(entity):
                continue
            out.append(entity_id)
        return out

    def check_rules_notify_automations(self, *, presence_ok: bool, motion_ok: bool) -> None:
        """Evaluate notify automation rules globally (no area discrimination)."""
        self._ensure_registry_area("general", "General")
        global_area_id = "general"

        def _is_system_area(area_name: str | None) -> bool:
            return slugify(area_name or "") == "system"

        def _cleanup_area(area_id: str) -> None:
            area = self.registry.get_area(area_id) if area_id else None
            if not area:
                return
            for auto in list(area.get("automations", []) or []):
                auto_id = auto.get("id") if isinstance(auto, dict) else None
                if auto_id and "notify_" in auto_id and auto_id not in {
                    "auto_notify_low_battery",
                    "auto_notify_unavailable_devices",
                    "auto_notify_away_entities",
                    "auto_notify_open_door_window",
                }:
                    self.registry.delete_automation(area_id, auto_id)
                    switch_id = f"{auto_id}_switch"
                    if self.registry.get_switch(area_id, switch_id):
                        self.registry.delete_switch(area_id, switch_id)
            for input_entry in list(area.get("inputs", []) or []):
                input_id = input_entry.get("id") if isinstance(input_entry, dict) else None
                if input_id and "notify_" in input_id and input_id not in {
                    "auto_notify_low_battery_toggle",
                    "auto_notify_unavailable_devices_toggle",
                    "auto_notify_away_toggle",
                    "auto_notify_open_door_window_toggle",
                    "auto_notify_door_open_minutes",
                    "auto_notify_device_on_minutes",
                }:
                    self.registry.delete_input(area_id, input_id)

        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            if not area_id:
                continue
            _cleanup_area(area_id)

        battery_entities: list[str] = []
        door_entities: list[str] = []
        device_entities: list[str] = []
        for area in self.registry.list_areas():
            area_id = area.get("area_id")
            area_name = area.get("name") or area_id or ""
            if not area_id or _is_system_area(area_name):
                continue
            battery_entities.extend(
                [
                    e.get("entity_id")
                    for e in self.registry.get_entities_by_type(area_id, EntityType.BATTERY)
                    if isinstance(e, dict) and e.get("entity_id")
                ]
            )
            door_entities.extend(self._get_door_entities(area_id))
            device_entities.extend(self._get_left_on_device_entities(area_id))

        low_battery_id = "auto_notify_low_battery"
        low_battery_toggle_id = "auto_notify_low_battery_toggle"
        if battery_entities:
            self.registry.add_automation(global_area_id, low_battery_id, "low battery notification")
            self.registry.add_input(
                global_area_id,
                low_battery_toggle_id,
                "Notify - Low Battery",
                InputType.INPUT_BOOLEAN,
            )
        else:
            if self.registry.get_automation(global_area_id, low_battery_id):
                self.registry.delete_automation(global_area_id, low_battery_id)
            for input_id in [low_battery_toggle_id]:
                if self.registry.get_input(global_area_id, input_id):
                    self.registry.delete_input(global_area_id, input_id)

        unavailable_id = "auto_notify_unavailable_devices"
        unavailable_toggle_id = "auto_notify_unavailable_devices_toggle"
        has_unavailable_targets = any(
            True
            for area in self.registry.list_areas()
            if area.get("area_id")
            and not _is_system_area(area.get("name") or area.get("area_id"))
            and self.registry.list_entities(area.get("area_id"))
        )
        if has_unavailable_targets:
            self.registry.add_automation(global_area_id, unavailable_id, "unavailable device notification")
            self.registry.add_input(
                global_area_id,
                unavailable_toggle_id,
                "Notify - Unavailable Devices",
                InputType.INPUT_BOOLEAN,
            )
        else:
            if self.registry.get_automation(global_area_id, unavailable_id):
                self.registry.delete_automation(global_area_id, unavailable_id)
            if self.registry.get_input(global_area_id, unavailable_toggle_id):
                self.registry.delete_input(global_area_id, unavailable_toggle_id)

        away_id = "auto_notify_away_entities"
        away_toggle_id = "auto_notify_away_toggle"
        away_device_minutes_id = "auto_notify_device_on_minutes"
        if presence_ok and motion_ok and device_entities:
            self.registry.add_automation(global_area_id, away_id, "left-on device notification")
            self.registry.add_input(
                global_area_id,
                away_toggle_id,
                "Notify - Away Entities",
                InputType.INPUT_BOOLEAN,
            )
            self.registry.add_input(
                global_area_id,
                away_device_minutes_id,
                "Notify - Device On Minutes",
                InputType.INPUT_NUMBER,
            )
        else:
            if self.registry.get_automation(global_area_id, away_id):
                self.registry.delete_automation(global_area_id, away_id)
            for input_id in [away_toggle_id, away_device_minutes_id]:
                if self.registry.get_input(global_area_id, input_id):
                    self.registry.delete_input(global_area_id, input_id)

        door_id = "auto_notify_open_door_window"
        door_toggle_id = "auto_notify_open_door_window_toggle"
        door_minutes_id = "auto_notify_door_open_minutes"
        if presence_ok and motion_ok and door_entities:
            self.registry.add_automation(global_area_id, door_id, "open-door-window notification")
            self.registry.add_input(
                global_area_id,
                door_toggle_id,
                "Notify - Open Door/Window",
                InputType.INPUT_BOOLEAN,
            )
            self.registry.add_input(
                global_area_id,
                door_minutes_id,
                "Notify - Door Open Minutes",
                InputType.INPUT_NUMBER,
            )
        else:
            if self.registry.get_automation(global_area_id, door_id):
                self.registry.delete_automation(global_area_id, door_id)
            for input_id in [door_toggle_id, door_minutes_id]:
                if self.registry.get_input(global_area_id, input_id):
                    self.registry.delete_input(global_area_id, input_id)

    def apply_rules(self):
        """Apply all automation rules across areas."""
        presence_ok, motion_ok = self._ensure_presence_groups()
        for area in self.registry.list_areas():
            self.check_rules_light_automation(area)
            self.check_rules_blinds_automation(area)
            self.check_rules_climate_automation(area)
            self.check_rules_buttons_automation(area)
            self._purge_obsolete_automation_inputs(area)
        self.check_rules_notify_automations(presence_ok=presence_ok, motion_ok=motion_ok)
        self._log.ok("All rules applied.")

    def _purge_obsolete_automation_inputs(self, area: dict) -> None:
        """Remove legacy automation toggle inputs from the registry."""
        area_id = area.get("area_id")
        area_name = area.get("name") or area_id or "unknown"
        for input_id in [
            self._prefixed_id("auto", area_name, "light_toggle"),
            self._prefixed_id("auto", area_name, "blinds_weekday_toggle"),
            self._prefixed_id("auto", area_name, "blinds_weekend_toggle"),
            self._prefixed_id("auto", area_name, "climate_toggle"),
            self._prefixed_id("auto", area_name, "button_toggle"),
        ]:
            if self.registry.get_input(area_id, input_id):
                self.registry.delete_input(area_id, input_id)

        legacy_automations = [
            build_object_id(area_name, "light"),
            build_object_id(area_name, "blinds_weekend"),
            build_object_id(area_name, "blinds_weekday"),
            build_object_id(area_name, "climate"),
            build_object_id(area_name, "button"),
        ]
        for automation_id in legacy_automations:
            if self.registry.get_automation(area_id, automation_id):
                self.registry.delete_automation(area_id, automation_id)
            switch_id = f"{automation_id}_switch"
            if self.registry.get_switch(area_id, switch_id):
                self.registry.delete_switch(area_id, switch_id)

        legacy_inputs = [
            build_object_id(area_name, "light_toggle"),
            build_object_id(area_name, "primary_lights_toggle"),
            build_object_id(area_name, "secondary_lights_toggle"),
            build_object_id(area_name, "lux_threshold"),
            build_object_id(area_name, "lights_delay"),
            build_object_id(area_name, "blinds_weekday_toggle"),
            build_object_id(area_name, "blinds_weekend_toggle"),
            build_object_id(area_name, "blinds_times"),
            build_object_id(area_name, "climate_toggle"),
            build_object_id(area_name, "cooling_thermostat"),
            build_object_id(area_name, "heating_thermostat"),
            build_object_id(area_name, "heat_setpoint"),
            build_object_id(area_name, "cool_setpoint"),
            build_object_id(area_name, "button_toggle"),
            build_object_id(area_name, "button_single"),
            build_object_id(area_name, "button_double"),
            build_object_id(area_name, "button_long"),
        ]
        for input_id in legacy_inputs:
            if self.registry.get_input(area_id, input_id):
                self.registry.delete_input(area_id, input_id)

    # ---------------------------
    # 5) Dashboards
    # ---------------------------
    def create_dashboards(self):
        """Generate dashboards from registry data."""
        self._log.start("Creating dashboards from registry.")
        self.dashboard_manager.create_main(self.registry.data)
        self.dashboard_manager.create_config(self.registry.data)
        self._log.ok("Dashboards generated.")

    # ---------------------------
    # Templates resolver
    # ---------------------------
    def _resolve_input_template(self, input_entry: dict) -> str:
        """Resolve the template name for an input entry."""
        if input_entry["type"] == InputType.INPUT_NUMBER and EntityType.LUX in input_entry["id"]:
            return "lux_threshold_template.yaml"
        if input_entry["type"] == InputType.INPUT_NUMBER and "setpoint" in input_entry["id"]:
            return "temperature_setpoint.yaml"
        if input_entry["type"] == InputType.INPUT_NUMBER and "delay" in input_entry["id"]:
            return "lights_delay.yaml"
        if input_entry["type"] == InputType.INPUT_NUMBER:
            return "simple_number_template.yaml"
        if input_entry["type"] == InputType.INPUT_DATETIME:
            return "blinds_inputs.yaml"
        if input_entry["type"] == InputType.CLIMATE:
            return "generic_thermostat_template.yaml"
        if input_entry["type"] == InputType.INPUT_BOOLEAN:
            return "automation_toggle_template.yaml"
        if input_entry["type"] == InputType.INPUT_TEXT:
            return "new_device_text_template.yaml"
        if input_entry["type"] == InputType.INPUT_SELECT:
            return "buttons_select_template.yaml"
        if input_entry["type"] == InputType.INPUT_BUTTON:
            return "simple_button_template.yaml"
        raise ValueError(f"No template mapping for input: {input_entry}")

    def _resolve_switch_template(self, switch_entry: dict) -> str:
        """Resolve the template name for a switch entry."""
        switch_type = switch_entry.get("type") or switch_entry.get("id")
        if switch_type == "automation":
            return "automation_switch_template.yaml"
        if switch_type == "tailscale":
            return "tailscale_switch.yaml"
        raise ValueError(f"No template mapping for switch: {switch_entry}")

    def _resolve_automation_template(self, automation_entry: dict) -> str:
        """Resolve the template name for an automation entry."""
        if automation_entry["id"].endswith("_light"):
            return "lights_automation_template.yaml"
        if automation_entry["id"].endswith("_blinds_weekday") or automation_entry["id"].endswith("_blinds_weekend"):
            return "blinds_automation_template.yaml"
        if automation_entry["id"].endswith("_climate"):
            return "temperature_automation_template.yaml"
        if automation_entry["id"].endswith("_button"):
            return "buttons_automation_template.yaml"
        if "notify_" in automation_entry["id"]:
            return "notify_generic.yaml"
        raise ValueError(f"No template mapping for automation: {automation_entry}")

    # ---------------------------
    # Actualizaciones vía Playwright (usa self.automator inyectado)
    # ---------------------------
    def update_dashboard_via_playwright(self, dashboard_path: str, yaml_path: str) -> None:
        """Update a dashboard by loading a full YAML file."""
        with open(yaml_path, "r", encoding="utf-8") as f:
            yaml_content = f.read()
        self.automator.write_yaml_to_ui(dashboard_path, yaml_content)

    def update_dashboard_new_device(
        self,
        dashboard_path: str,
        view_path: str,
        section_heading: str,
        yaml_fragment: str,
        create_if_missing: bool = True,
    ) -> None:
        """Insert a YAML fragment into a dashboard section."""
        raw_yaml = self.automator.read_yaml_from_ui(dashboard_path)
        try:
            doc = yaml.safe_load(raw_yaml) or {}
        except Exception as exc:
            raise RuntimeError(f"Invalid dashboard YAML: {exc}")

        if not isinstance(doc, dict):
            raise RuntimeError("Root YAML must be a mapping (dict).")

        views = doc.get("views")
        if not isinstance(views, list):
            if create_if_missing:
                views = []
                doc["views"] = views
            else:
                raise RuntimeError("'views' not found or invalid in YAML.")

        view = next((v for v in views if v.get("path") == view_path), None)
        if view is None:
            if create_if_missing:
                view = {"type": "sections", "path": view_path, "title": view_path.title(), "sections": []}
                views.append(view)
            else:
                raise RuntimeError(f"View '{view_path}' not found.")

        if view.get("type") != "sections":
            if create_if_missing:
                view["type"] = "sections"
            else:
                raise RuntimeError("Target view must be type: sections.")

        sections = view.get("sections")
        if not isinstance(sections, list):
            if create_if_missing:
                sections = []
                view["sections"] = sections
            else:
                raise RuntimeError("View has no 'sections' list.")

        target_idx = None
        for i, sec in enumerate(sections):
            cards = sec.get("cards") or []
            for c in cards:
                if isinstance(c, dict) and c.get("type") == "heading":
                    if (c.get("heading") or "").strip().lower() == section_heading.strip().lower():
                        target_idx = i
                        break
            if target_idx is not None:
                break

        if target_idx is None:
            if create_if_missing:
                sections.append({
                    "type": "grid",
                    "cards": [{
                        "type": "heading",
                        "heading": section_heading,
                        "heading_style": "title"
                    }]
                })
                target_idx = len(sections) - 1
            else:
                raise RuntimeError(f"Section with heading '{section_heading}' not found.")

        section = sections[target_idx]
        if not isinstance(section.get("cards"), list):
            section["cards"] = []
        cards = section["cards"]

        if isinstance(yaml_fragment, (dict, list)):
            frag = yaml_fragment
        else:
            try:
                frag = yaml.safe_load(yaml_fragment)
            except Exception as exc:
                raise RuntimeError(f"Invalid YAML fragment: {exc}")

        if isinstance(frag, list):
            new_cards = [c for c in frag if isinstance(c, dict)]
        elif isinstance(frag, dict):
            if "cards" in frag and isinstance(frag["cards"], list):
                new_cards = [c for c in frag["cards"] if isinstance(c, dict)]
            else:
                new_cards = [frag]
        else:
            raise RuntimeError("YAML fragment must be a card dict or a list of cards.")

        if not new_cards:
            return

        cards.extend(new_cards)

        new_yaml = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        self.automator.write_yaml_to_ui(dashboard_path, new_yaml)

    # ---------------------------
    # Flujo: device nuevo
    # ---------------------------
    def add_new_device(
        self,
        *,
        device_id: str,
        ) -> Dict[str, Any]:
        """Fetch a device, update inventory, and inject its dashboard card."""
        dashboard_path="config/",  # Cambiar según tu ID de dashboard
        view_path="devices",  # Opcional, depende de tu estructura
        section_heading="New Devices",  # Encabezado donde insertar el botón
        create_if_missing=True,
        data = self.ha.fetch_device_and_entities_by_id(device_id)
        device = data.get("device")
        entities = data.get("entities") or []
        if not device:
            raise ValueError(f"Device '{device_id}' no encontrado.")

        area_id = device.get("area_id")

        # Normalizar device para inventory
        dev_to_store = {
            "id": device.get("id"),
            "platform": device.get("platform"),
            "name_by_user": device.get("name_by_user"),
            "name": device.get("name"),
            "labels": device.get("labels") or [],
            "entities": sorted({e["entity_id"] for e in entities if e.get("entity_id")}),
            "services": {},  # puedes completar si lo necesitas
        }

        # Agregar/actualizar inventario cleaned
        self.inventory_processor.add_device_to_inventory(dev_to_store, area_id)

        # Inferir entidades "tipadas" para el registry (sin escribir archivos todavía)
        inferred_entities = self.inventory_processor.add_entities_by_device(dev_to_store, area_id)
        self.ha.set_input_boolean(f"{InputType.INPUT_BOOLEAN}.new_devices_toggle", "on")
        yaml_fragment = self.dashboard_creator.build_device_card_by_entity_id(entity_id=device_id,registry_data=re)
        # Inyectar el fragmento YAML en la view/section
        self.update_dashboard_new_device(
            dashboard_path=dashboard_path,
            view_path=view_path,
            section_heading=section_heading,
            yaml_fragment=yaml_fragment,
            create_if_missing=create_if_missing,
        )

        return {"device": dev_to_store, "area_id": area_id, "entities_inferred": inferred_entities}

    def handle_device_created_event(
        self,
        event_device: dict,
        *,
        dashboard_path: str,
        view_path: str,
        section_heading: str,
        yaml_fragment: str,
        create_if_missing: bool = True,
    ) -> None:
        """Handle a device-created event and run the new device flow."""
        device_id = event_device.get("id")
        if not device_id:
            self._log.warn(f"Device creation event missing device id: {event_device}")
            return

        self.add_new_device(
            device_id=device_id,
            dashboard_path=dashboard_path,
            view_path=view_path,
            section_heading=section_heading,
            yaml_fragment=yaml_fragment,
            create_if_missing=create_if_missing,
        )

