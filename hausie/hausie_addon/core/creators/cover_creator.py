from __future__ import annotations

from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..utils.naming import build_filename, prefix_filename, slugify
from ..flow_logger import get_logger

PKG_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = PKG_DIR.parent

log = get_logger("core")


class CoverCreator:
    """Render cover-group YAMLs from templates."""

    def __init__(self, templates_base: Path | None = None, outputs_base: Path | None = None):
        """Initialize template paths and covers output directory."""
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "covers")).resolve()
        self.covers_dir = (outputs_base or (ROOT_DIR / "hausie" / "homeassistant" / "covers")).resolve()
        self.covers_dir.mkdir(parents=True, exist_ok=True)

        self.jinja = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render_template(self, template_file: str, context: dict) -> list[dict]:
        """Render a Jinja template and return the parsed cover entries."""
        tpl_path = (self.templates_dir / template_file).resolve()
        if not tpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tpl_path}")

        template = self.jinja.get_template(template_file)
        rendered = template.render(**context)
        parsed = yaml.safe_load(rendered) or []
        if not isinstance(parsed, list):
            raise ValueError("Invalid rendered YAML for cover: expected list.")
        return [entry for entry in parsed if isinstance(entry, dict)]

    def _merge_into_file(self, dest_file: Path, entries: list[dict], *, unique_id: str) -> None:
        """Merge a cover entry into a YAML list file."""
        if dest_file.exists():
            existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or []
            if not isinstance(existing, list):
                existing = []
        else:
            existing = []

        existing = [
            entry
            for entry in existing
            if not (isinstance(entry, dict) and str(entry.get("unique_id") or "").strip() == unique_id)
        ]
        existing.extend(entries)

        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)

        log.ok(f"Cover group updated: {dest_file}")

    def create_cover_group(
        self,
        area_slug: str | None,
        group_name: str,
        unique_id: str,
        entities: list[str] | None,
        *,
        output_filename: str | None = None,
    ) -> Path:
        """Create or update a grouped cover file for an area."""
        safe_area = slugify(area_slug) if area_slug else ""
        filename = output_filename or (build_filename("cover", safe_area) if safe_area else f"{unique_id}.yaml")
        filename = prefix_filename(filename)
        context = {
            "group_name": group_name,
            "unique_id": unique_id,
            "entities": list(entities or []),
        }
        entries = self._render_template("cover_group.yaml", context)
        dest_file = (self.covers_dir / filename).resolve()
        self._merge_into_file(dest_file, entries, unique_id=unique_id)
        return dest_file
