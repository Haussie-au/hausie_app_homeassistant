# hausie_app/input_creator.py
from __future__ import annotations
from pathlib import Path
import os
import yaml
from jinja2 import Template
from ...constants import InputType
from ..flow_logger import get_logger
from ..utils.naming import build_alias, build_filename, build_object_id, prefix_filename, slugify

# Directorios base
PKG_DIR  = Path(__file__).resolve().parents[2]  # .../hausie_app
ROOT_DIR = PKG_DIR.parent                             # raíz del repo

log = get_logger("core")

class InputCreator:
    def __init__(self,
                 templates_base: Path | None = None,
                 outputs_base: Path | None = None):
        """Initialize template paths and input output directory."""
        # Templates ahora viven dentro del paquete
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "inputs")).resolve()
        # Por defecto escribimos en <raiz>/hausie/homeassistant/helpers
        self.inputs_dir    = (outputs_base  or (ROOT_DIR / "hausie" / "homeassistant" / "helpers")).resolve()
    def _create_input(
        self,
        area: str,
        input_type: str,
        input_name: str,
        template_file: str,
        context: dict = None,
        output_filename: str | None = None,
    ):
        """
        Create or update an area-level input YAML from a Jinja template.
        """
        src = (self.templates_dir / input_type / template_file).resolve()
        dest_dir = (self.inputs_dir / input_type).resolve()
        area_slug = slugify(area)
        filename = output_filename or build_filename(input_type, area_slug)
        dest_file = dest_dir / prefix_filename(filename)

        if not src.exists():
            raise FileNotFoundError(f"ƒ?O Template not found: {src}")

        dest_dir.mkdir(parents=True, exist_ok=True)

        # Render Jinja template
        with open(src, "r", encoding="utf-8") as f:
            template = Template(f.read())
        rendered = template.render(area_name=area_slug, **(context or {}))
        parsed = yaml.safe_load(rendered) or {}
        if not isinstance(parsed, dict):
            raise ValueError(f"Invalid rendered YAML for {input_type}: expected mapping.")

        if input_type in parsed and isinstance(parsed[input_type], dict):
            entries = parsed[input_type]
        else:
            entries = parsed

        if not isinstance(entries, dict):
            raise ValueError(f"Invalid entries for {input_type}: expected mapping.")

        helper_id = Path(input_name).stem if input_name else None
        if helper_id and helper_id not in entries and len(entries) == 1 and input_type != InputType.CLIMATE:
            only_key = next(iter(entries))
            entries = {helper_id: entries[only_key]}

        if input_type == InputType.INPUT_BOOLEAN and helper_id:
            name_override = (context or {}).get("automation_name")
            if name_override:
                entries.setdefault(helper_id, {})
                entries[helper_id]["name"] = name_override

        if dest_file.exists():
            existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or {}
        else:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

        if input_type in existing and isinstance(existing[input_type], dict):
            merged = existing[input_type]
        elif existing and isinstance(existing, dict):
            merged = existing
        else:
            merged = {}

        merged.update(entries)

        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, sort_keys=False)
        log.ok(f"ƒo. Input updated: {dest_file}")

    # --- Specific input creators ---
    def create_lux_threshold(self, area: str):
        """Create or update the lux threshold input for an area."""
        helper_id = build_object_id(area, "lux_threshold")
        self._create_input(area, InputType.INPUT_NUMBER, f"{helper_id}.yaml", "lux_threshold_template.yaml")

    def create_lights_delay(self, area: str):
        """Create or update the lights delay input for an area."""
        helper_id = build_object_id(area, "lights_delay")
        self._create_input(area, InputType.INPUT_NUMBER, f"{helper_id}.yaml", "lights_delay.yaml")

    def create_blinds_datetime(self, area: str):
        """Create or update blinds datetime inputs for an area."""
        helper_id = build_object_id(area, "blinds_times")
        self._create_input(area, InputType.INPUT_DATETIME, f"{helper_id}.yaml", "blinds_inputs.yaml")

    def create_heating_thermostat(self, area: str):
        """Create or update a heating thermostat input for an area."""
        helper_id = build_object_id(area, "heating_thermostat")
        self._create_input(
            area, InputType.CLIMATE, f"{helper_id}.yaml", "generic_thermostat_template.yaml",
            context={"mode": "heat", "min_temp": 15, "max_temp": 22, "initial_temp": 20}
        )

    def create_cooling_thermostat(self, area: str):
        """Create or update a cooling thermostat input for an area."""
        helper_id = build_object_id(area, "cooling_thermostat")
        self._create_input(
            area, InputType.CLIMATE, f"{helper_id}.yaml", "generic_thermostat_template.yaml",
            context={"mode": "cool", "min_temp": 20, "max_temp": 28, "initial_temp": 24}
        )

    def add_input_boolean(self, area: str, automation_type: str):
        """
        Create an input_boolean to enable/disable automations.
        """
        automation_id = build_object_id(area, automation_type)
        self._create_input(
            area,
            InputType.INPUT_BOOLEAN,
            f"{automation_id}.yaml",
            "automation_toggle_template.yaml",
            context={"automation_name": build_alias("Input Boolean", area, automation_type)},
        )
    # --- Delete input ---
    def delete_input(self, area: str, input_type: str, input_name: str):
        """Delete an input entry from the area input file."""
        area_slug = slugify(area)
        dest_file = (self.inputs_dir / input_type / build_filename(input_type, area_slug)).resolve()
        helper_id = Path(input_name).stem if input_name else None
        if not dest_file.exists():
            log.warn(f"ƒsÿ‹÷? Input not found: {dest_file}")
            return

        if not helper_id:
            os.remove(dest_file)
            log.ok(f"ÐY-'‹÷? Deleted input file: {dest_file}")
            return

        existing = yaml.safe_load(dest_file.read_text(encoding="utf-8")) or {}
        if not isinstance(existing, dict):
            log.warn(f"ƒsÿ‹÷? Input not found: {dest_file}")
            return

        section = existing.get(input_type)
        if not isinstance(section, dict):
            section = existing

        if helper_id in section:
            section.pop(helper_id, None)
            if section:
                with open(dest_file, "w", encoding="utf-8") as f:
                    yaml.safe_dump(section, f, sort_keys=False)
                log.ok(f"ÐY-'‹÷? Deleted input: {helper_id}")
            else:
                os.remove(dest_file)
                log.ok(f"ÐY-'‹÷? Deleted input file: {dest_file}")
        else:
            log.warn(f"ƒsÿ‹÷? Input not found: {helper_id}")

