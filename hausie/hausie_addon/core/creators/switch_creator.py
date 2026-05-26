from __future__ import annotations

from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..utils.naming import build_filename, prefix_filename, slugify
from ..flow_logger import get_logger

PKG_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = PKG_DIR.parent

log = get_logger("core")


class SwitchCreator:
    """Render switch YAMLs from templates."""

    def __init__(self, templates_base: Path | None = None, outputs_base: Path | None = None):
        """Initialize template paths and switch output directory."""
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "switches")).resolve()
        self.switches_dir = (outputs_base or (ROOT_DIR / "hausie" / "homeassistant" / "switches")).resolve()
        self.switches_dir.mkdir(parents=True, exist_ok=True)

        self.jinja = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render_template(self, template_file: str, context: dict) -> list:
        """Render a Jinja template and return the parsed switch list."""
        tpl_path = (self.templates_dir / template_file).resolve()
        if not tpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tpl_path}")

        template = self.jinja.get_template(template_file)
        rendered = template.render(**context)
        parsed = yaml.safe_load(rendered) or []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ValueError("Invalid rendered YAML for switches: expected list.")
        return parsed

    def _merge_into_file(self, dest_file: Path, entries: list) -> None:
        """Merge switch entries into a YAML file."""
        if dest_file.exists():
            existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or []
            if not isinstance(existing, list):
                existing = []
        else:
            existing = []

        # Drop legacy template platform entries to avoid duplicates after migration.
        existing = [
            item for item in existing
            if not (
                isinstance(item, dict)
                and item.get("platform") == "template"
                and isinstance(item.get("switches"), dict)
            )
        ]

        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            platform = entry.get("platform")
            switches = entry.get("switches")
            if platform and isinstance(switches, dict):
                merged = False
                for item in existing:
                    if (
                        isinstance(item, dict)
                        and item.get("platform") == platform
                        and isinstance(item.get("switches"), dict)
                    ):
                        item["switches"].update(switches)
                        merged = True
                        break
                if not merged:
                    existing.append(entry)
                continue

            if isinstance(entry.get("switch"), list):
                merged = False
                for item in existing:
                    if isinstance(item, dict) and isinstance(item.get("switch"), list):
                        existing_list = item["switch"]
                        for new_switch in entry["switch"]:
                            if not isinstance(new_switch, dict):
                                continue
                            key = new_switch.get("unique_id") or new_switch.get("name")
                            if not key:
                                existing_list.append(new_switch)
                                continue
                            matched = False
                            for idx, existing_switch in enumerate(existing_list):
                                if not isinstance(existing_switch, dict):
                                    continue
                                existing_key = existing_switch.get("unique_id") or existing_switch.get("name")
                                if existing_key == key:
                                    existing_list[idx] = new_switch
                                    matched = True
                                    break
                            if not matched:
                                existing_list.append(new_switch)
                        merged = True
                        break
                if not merged:
                    existing.append(entry)
                continue

            existing.append(entry)

        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, sort_keys=False)

        log.ok(f"Switches updated: {dest_file}")

    def create_from_template(
        self,
        area_slug: str | None,
        *,
        template_file: str,
        context: dict,
        output_filename: str | None = None,
    ) -> Path:
        """Create or update a switch entry inside an area switches file."""
        safe_area = slugify(area_slug) if area_slug else ""
        filename = output_filename or (build_filename("switch", safe_area) if safe_area else "switches.yaml")
        filename = prefix_filename(filename)
        entries = self._render_template(template_file, context)
        dest_file = (self.switches_dir / filename).resolve()
        self._merge_into_file(dest_file, entries)
        return dest_file
