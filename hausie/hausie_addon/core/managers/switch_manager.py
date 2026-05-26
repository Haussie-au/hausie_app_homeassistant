from __future__ import annotations

from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater


class SwitchManager:
    """CRUD operations for switch YAML entries."""

    def __init__(self, *, yaml_updater: HomeAssistantYamlUpdater) -> None:
        self.yaml_updater = yaml_updater

    def create(
        self,
        area_name: str,
        switch_entry: dict,
        *,
        context: dict,
        template_file: str,
        output_filename: str | None = None,
    ) -> None:
        """Create or update a switch entry (merge into existing file)."""
        self.yaml_updater.update_switch_entry(
            area_name,
            template_file=template_file,
            context=context,
            output_filename=output_filename,
        )

    def update(
        self,
        area_name: str,
        switch_entry: dict,
        *,
        context: dict,
        template_file: str,
        output_filename: str | None = None,
    ) -> None:
        """Update a switch entry (merge into existing file)."""
        self.yaml_updater.update_switch_entry(
            area_name,
            template_file=template_file,
            context=context,
            output_filename=output_filename,
        )

