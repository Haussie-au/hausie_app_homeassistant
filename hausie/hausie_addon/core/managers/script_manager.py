from __future__ import annotations

from ..creators.script_creator import ScriptCreator
from ..io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater
from ..utils.naming import prefix_filename, slugify


class ScriptManager:
    """CRUD operations for scripts."""

    def __init__(
        self,
        *,
        yaml_updater: HomeAssistantYamlUpdater,
        yaml_manager: HomeAssistantYamlManager,
        script_creator: ScriptCreator | None = None,
    ) -> None:
        self.yaml_updater = yaml_updater
        self.yaml_manager = yaml_manager
        self.script_creator = script_creator or ScriptCreator()

    def create_toggle_script(
        self,
        area_name: str | None,
        script_id: str,
        script_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Create or update a toggle script entry."""
        self.yaml_updater.update_toggle_script(
            area_name,
            script_id,
            script_name,
            entities,
            output_filename=output_filename,
        )

    def create_help_boxes_reset_script(
        self,
        *,
        script_id: str,
        script_name: str,
        entity_ids: list[str],
        output_filename: str | None = None,
    ) -> None:
        """Create or update a script that turns on help box toggles."""
        safe_area = "general"
        filename = output_filename or f"{safe_area}_scripts.yaml"
        self.yaml_updater.update_script_entry(
            filename,
            lambda: self.script_creator.create_help_boxes_reset_script(
                script_id=script_id,
                script_name=script_name,
                entity_ids=entity_ids,
                output_filename=filename,
            ),
        )

    def create_upgrade_plan_popup_script(
        self,
        *,
        script_id: str,
        script_name: str,
        output_filename: str | None = None,
    ) -> None:
        """Create or update the upgrade popup script."""
        safe_area = "general"
        filename = output_filename or f"{safe_area}_scripts.yaml"
        self.yaml_updater.update_script_entry(
            filename,
            lambda: self.script_creator.create_upgrade_plan_popup_script(
                script_id=script_id,
                script_name=script_name,
                output_filename=filename,
            ),
        )

    def read_remote(self, filename: str) -> str:
        """Read a script YAML file from the Pi."""
        if not self.yaml_updater.pi_sender or not self.yaml_updater.remote_root:
            raise RuntimeError("PI sender and remote_root are required to read scripts.")
        remote_path = f"{self.yaml_updater.remote_root.rstrip('/')}/scripts/{prefix_filename(filename)}"
        return self.yaml_updater.pi_sender.read_remote_text(remote_path)

    def update_toggle_script(
        self,
        area_name: str | None,
        script_id: str,
        script_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a toggle script entry."""
        self.create_toggle_script(
            area_name,
            script_id,
            script_name,
            entities,
            output_filename=output_filename,
        )

    def delete(self, script_id: str, *, area_name: str | None = None, filename: str | None = None) -> bool:
        """Delete a script entry."""
        safe_area = slugify(area_name) if area_name else None
        return self.yaml_manager.delete_script(script_id, safe_area, filename)
