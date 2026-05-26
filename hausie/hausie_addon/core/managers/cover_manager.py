from __future__ import annotations

from ..io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater


class CoverManager:
    """CRUD operations for grouped covers."""

    def __init__(
        self,
        *,
        yaml_updater: HomeAssistantYamlUpdater,
        yaml_manager: HomeAssistantYamlManager,
    ) -> None:
        self.yaml_updater = yaml_updater
        self.yaml_manager = yaml_manager

    def create_group(
        self,
        area_name: str,
        unique_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Create or update a grouped cover file."""
        self.yaml_updater.update_cover_group(
            area_name,
            unique_id,
            group_name,
            entities,
            output_filename=output_filename,
        )

    def update_group(
        self,
        area_name: str,
        unique_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a grouped cover file."""
        self.yaml_updater.update_cover_group(
            area_name,
            unique_id,
            group_name,
            entities,
            output_filename=output_filename,
        )

    def delete(self, area_name: str | None = None, *, filename: str | None = None) -> bool:
        """Delete a grouped cover file."""
        return self.yaml_manager.delete_cover_file(area_name, filename)
