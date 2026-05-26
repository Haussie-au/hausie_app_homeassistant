from __future__ import annotations

from ..io.homeassistant_yaml_updater import HomeAssistantYamlUpdater
from ..flow_logger import get_logger
from ..utils.naming import build_filename, slugify

log = get_logger("core")


class HelperManager:
    """CRUD operations for helpers (no deletes, no full replace)."""

    def __init__(self, *, yaml_updater: HomeAssistantYamlUpdater) -> None:
        self.yaml_updater = yaml_updater

    def create(
        self,
        area_name: str,
        input_entry: dict,
        *,
        context: dict,
        template_file: str,
        output_filename: str | None = None,
    ) -> None:
        """Create a helper entry (merge into existing file)."""
        self.yaml_updater.update_input(
            area_name,
            input_entry,
            context=context,
            template_file=template_file,
            output_filename=output_filename,
        )

    def read_remote(self, area_name: str, input_type: str) -> str:
        """Read a helper YAML file from the Pi."""
        if not self.yaml_updater.pi_sender or not self.yaml_updater.remote_root:
            raise RuntimeError("PI sender and remote_root are required to read helpers.")
        area_slug = slugify(area_name)
        filename = build_filename(input_type, area_slug)
        remote_path = f"{self.yaml_updater.remote_root.rstrip('/')}/helpers/{input_type}/{filename}"
        return self.yaml_updater.pi_sender.read_remote_text(remote_path)

    def update(
        self,
        area_name: str,
        input_entry: dict,
        *,
        context: dict,
        template_file: str,
        output_filename: str | None = None,
    ) -> None:
        """Update a helper entry (merge only)."""
        self.yaml_updater.update_input(
            area_name,
            input_entry,
            context=context,
            template_file=template_file,
            output_filename=output_filename,
        )

    def delete(self, *_args, **_kwargs) -> bool:
        """Helpers are never deleted."""
        log.skip("Helpers are not deleted by design.")
        return False
