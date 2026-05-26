from __future__ import annotations

from ..io.homeassistant_yaml_manager import HomeAssistantYamlManager
from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater
from ..utils.naming import build_filename


class AutomationManager:
    """CRUD operations for automations."""

    def __init__(
        self,
        *,
        yaml_updater: HomeAssistantYamlUpdater,
        yaml_manager: HomeAssistantYamlManager,
    ) -> None:
        self.yaml_updater = yaml_updater
        self.yaml_manager = yaml_manager

    def create(self, automation_entry: dict, *, context: dict, template_file: str) -> None:
        """Create a new automation entry (idempotent)."""
        self.yaml_updater.update_automation(automation_entry, context=context, template_file=template_file)

    def read_remote(self, automation_id: str) -> str:
        """Read an automation YAML file from the Pi."""
        if not self.yaml_updater.pi_sender or not self.yaml_updater.remote_root:
            raise RuntimeError("PI sender and remote_root are required to read automations.")
        filename = build_filename("automation", automation_id)
        remote_path = f"{self.yaml_updater.remote_root.rstrip('/')}/automations/{filename}"
        return self.yaml_updater.pi_sender.read_remote_text(remote_path)

    def update(self, automation_entry: dict, *, context: dict, template_file: str) -> None:
        """Update an automation entry (replace file)."""
        self.yaml_updater.update_automation(automation_entry, context=context, template_file=template_file)

    def delete(self, automation_id: str) -> bool:
        """Delete an automation file."""
        return self.yaml_manager.delete_automation(automation_id=automation_id)
