# hausie_app/script_creator.py
from __future__ import annotations
from pathlib import Path
import yaml

from ..utils.naming import prefix_filename, slugify
from ..flow_logger import get_logger
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Base directories
PKG_DIR = Path(__file__).resolve().parents[2]  # .../hausie_app
ROOT_DIR = PKG_DIR.parent

log = get_logger("core")


class ScriptCreator:
    """ScriptCreator renders script YAML files from templates."""

    def __init__(self, templates_base: Path | None = None, outputs_base: Path | None = None):
        """Initialize template paths and script output directory."""
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "scripts")).resolve()
        self.scripts_dir = (outputs_base or (ROOT_DIR / "hausie" / "homeassistant" / "scripts")).resolve()
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

        self.jinja = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render_template(self, template_file: str, context: dict) -> dict:
        """Render a Jinja template and return the parsed script entry."""
        tpl_path = (self.templates_dir / template_file).resolve()
        if not tpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tpl_path}")

        template = self.jinja.get_template(template_file)
        rendered = template.render(**context)
        parsed = yaml.safe_load(rendered) or {}
        if not isinstance(parsed, dict):
            raise ValueError("Invalid rendered YAML for script: expected mapping.")
        return parsed

    def _merge_into_file(self, dest_file: Path, entry: dict) -> None:
        """Merge a script entry into a YAML file."""
        if dest_file.exists():
            existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        else:
            existing = {}

        existing.update(entry)
        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)

        log.ok(f"Script updated: {dest_file}")

    def _resolve_area_dir(self) -> Path:
        """Return the scripts root directory (no per-area subfolders)."""
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        return self.scripts_dir

    def create_toggle_script(
        self,
        area_folder: str | None,
        script_id: str,
        script_name: str,
        entities: list[str],
        output_filename: str | None = None,
    ):
        """Create or update a toggle script inside an area script file."""
        is_group = len(entities) == 1 and str(entities[0]).startswith("group.")
        context = {
            "script_id": script_id,
            "script_name": script_name,
            "entities": entities,
            "is_group": is_group,
        }
        entry = self._render_template("toggle_script.yaml", context)

        safe_area = slugify(area_folder) if area_folder else "general"
        filename = prefix_filename(output_filename or f"{safe_area}_scripts.yaml")
        dest_dir = self._resolve_area_dir()
        dest_file = (dest_dir / filename).resolve()
        self._merge_into_file(dest_file, entry)

    def create_help_boxes_reset_script(
        self,
        script_id: str,
        script_name: str,
        entity_ids: list[str],
        output_filename: str | None = None,
    ) -> None:
        """Create a script that turns on all help box toggles."""
        context = {
            "script_id": script_id,
            "script_name": script_name,
            "entity_ids": entity_ids,
        }
        entry = self._render_template("help_boxes_reset_script.yaml", context)
        filename = prefix_filename(output_filename or "general_scripts.yaml")
        dest_dir = self._resolve_area_dir()
        dest_file = (dest_dir / filename).resolve()
        self._merge_into_file(dest_file, entry)

    def create_upgrade_plan_popup_script(
        self,
        script_id: str,
        script_name: str,
        output_filename: str | None = None,
    ) -> None:
        """Create a script that opens the upgrade popup using runtime variables."""
        context = {
            "script_id": script_id,
            "script_name": script_name,
        }
        entry = self._render_template("upgrade_plan_popup_script.yaml", context)
        filename = prefix_filename(output_filename or "general_scripts.yaml")
        dest_dir = self._resolve_area_dir()
        dest_file = (dest_dir / filename).resolve()
        self._merge_into_file(dest_file, entry)

