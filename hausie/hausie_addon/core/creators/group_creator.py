from __future__ import annotations

from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..utils.naming import build_filename, prefix_filename, slugify
from ..flow_logger import get_logger

PKG_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = PKG_DIR.parent

log = get_logger("core")


class GroupCreator:
    """Render group YAMLs from templates."""

    def __init__(self, templates_base: Path | None = None, outputs_base: Path | None = None):
        """Initialize template paths and groups output directory."""
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "groups")).resolve()
        self.groups_dir = (outputs_base or (ROOT_DIR / "hausie" / "homeassistant" / "groups")).resolve()
        self.groups_dir.mkdir(parents=True, exist_ok=True)

        self.jinja = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render_template(self, template_file: str, context: dict) -> dict:
        """Render a Jinja template and return the parsed group entry."""
        tpl_path = (self.templates_dir / template_file).resolve()
        if not tpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tpl_path}")

        template = self.jinja.get_template(template_file)
        rendered = template.render(**context)
        parsed = yaml.safe_load(rendered) or {}
        if not isinstance(parsed, dict):
            raise ValueError("Invalid rendered YAML for group: expected mapping.")
        return parsed

    def _merge_into_file(self, dest_file: Path, entry: dict) -> None:
        """Merge a group entry into a YAML file."""
        if dest_file.exists():
            existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        else:
            existing = {}

        existing.update(entry)
        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)

        log.ok(f"Group updated: {dest_file}")
        return dest_file

    def create_light_group(
        self,
        area_slug: str | None,
        group_id: str,
        group_name: str,
        lights: list[str] | None,
        output_filename: str | None = None,
    ) -> Path:
        """Create or update a light group inside an area group file."""
        safe_area = slugify(area_slug) if area_slug else ""
        filename = output_filename or (build_filename("group", safe_area) if safe_area else f"{group_id}.yaml")
        filename = prefix_filename(filename)
        context = {
            "group_id": group_id,
            "group_name": group_name,
            "entities": list(lights or []),
        }
        entry = self._render_template("light_groups.yaml", context)
        dest_file = (self.groups_dir / filename).resolve()
        self._merge_into_file(dest_file, entry)
        return dest_file

    def create_boolean_group(
        self,
        area_slug: str | None,
        group_id: str,
        group_name: str,
        entities: list[str] | None,
        *,
        all_state: bool = True,
        output_filename: str | None = None,
    ) -> Path:
        """Create or update a boolean group inside an area group file."""
        safe_area = slugify(area_slug) if area_slug else ""
        filename = output_filename or (build_filename("group", safe_area) if safe_area else f"{group_id}.yaml")
        filename = prefix_filename(filename)
        context = {
            "group_id": group_id,
            "group_name": group_name,
            "entities": list(entities or []),
            "all_state": bool(all_state),
        }
        entry = self._render_template("boolean_groups.yaml", context)
        dest_file = (self.groups_dir / filename).resolve()
        self._merge_into_file(dest_file, entry)
        return dest_file


class GroupGenerator(GroupCreator):
    """Compatibility alias for the creator."""

