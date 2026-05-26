from __future__ import annotations

from ..io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater


class GroupManager:
    """CRUD operations for groups."""

    def __init__(
        self,
        *,
        yaml_updater: HomeAssistantYamlUpdater,
        yaml_manager: HomeAssistantYamlManager,
    ) -> None:
        self.yaml_updater = yaml_updater
        self.yaml_manager = yaml_manager

    def create_light_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Create or update a light group entry."""
        self.yaml_updater.update_light_group(
            area_name,
            group_id,
            group_name,
            entities,
            output_filename=output_filename,
        )

    def read_remote(self, filename: str) -> str:
        """Read a group YAML file from the Pi."""
        if not self.yaml_updater.pi_sender or not self.yaml_updater.remote_root:
            raise RuntimeError("PI sender and remote_root are required to read groups.")
        remote_path = f"{self.yaml_updater.remote_root.rstrip('/')}/groups/{filename}"
        return self.yaml_updater.pi_sender.read_remote_text(remote_path)

    def update_light_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a light group entry."""
        self.yaml_updater.update_light_group(
            area_name,
            group_id,
            group_name,
            entities,
            output_filename=output_filename,
        )

    def create_boolean_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        all_state: bool = True,
        output_filename: str | None = None,
    ) -> None:
        """Create or update a boolean group entry."""
        self.yaml_updater.update_boolean_group(
            area_name,
            group_id,
            group_name,
            entities,
            all_state=all_state,
            output_filename=output_filename,
        )

    def update_boolean_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        all_state: bool = True,
        output_filename: str | None = None,
    ) -> None:
        """Update a boolean group entry."""
        self.yaml_updater.update_boolean_group(
            area_name,
            group_id,
            group_name,
            entities,
            all_state=all_state,
            output_filename=output_filename,
        )

    def delete(self, group_id: str, *, area_name: str | None = None, filename: str | None = None) -> bool:
        """Delete a group entry."""
        return self.yaml_manager.delete_group(group_id, area_name, filename)
