# hausie_app/automation_creator.py
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ...constants import InputType
from ..flow_logger import get_logger
from ..utils.naming import build_alias, build_filename, build_object_id, slugify

# Directorios base
PKG_DIR  = Path(__file__).resolve().parents[2]  # .../hausie_app
ROOT_DIR = PKG_DIR.parent                             # raíz del repo

log = get_logger("auto")

class AutomationCreator:
    def __init__(self,
                 templates_base: Path | None = None,
                 outputs_base: Path | None = None):
        """Initialize template paths and automation output directory."""
        # Templates ahora dentro del paquete
        self.templates_dir = (templates_base or (PKG_DIR / "templates" / "automations")).resolve()
        # Salidas por defecto en <raiz>/hausie/homeassistant/automations
        self.automations_dir = (outputs_base  or (ROOT_DIR / "hausie" / "homeassistant" / "automations")).resolve()
        self.automations_dir.mkdir(parents=True, exist_ok=True)

        # Jinja
        self.jinja = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _create_from_template(self, automation_filename: str, template_file: str, context: dict):
        """Render a Jinja template and write the automation YAML."""
        tpl_path = (self.templates_dir / template_file).resolve()
        if not tpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tpl_path}")

        template = self.jinja.get_template(template_file)
        rendered = template.render(**context)

        dest_file = (self.automations_dir / automation_filename).resolve()
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(rendered)

        log.ok(f"Automation written (overwritten if existed): {dest_file}")

    # --- Automations específicas ---
    def create_light_automation(self, area_name: str, motion_sensors: list, lights: list, motion_to_lux: dict):
        """Render and write a light automation for an area."""
        area_slug = slugify(area_name)
        automation_filename = build_filename("automation", area_slug, "light")
        context = {
            "area_name": area_slug,
            "motion_sensors": motion_sensors,
            "lights": lights,
            "motion_to_lux": motion_to_lux,
            "automation_alias": build_alias("Automation", area_name, "Light"),
        }
        self._create_from_template(automation_filename, "lights_automation_template.yaml", context)

    def create_blinds_automation(self, area_name: str, blinds: list, schedule_type: str = "weekday"):
        """Render and write a blinds automation for an area."""
        schedule = (schedule_type or "weekday").lower()
        if schedule not in {"weekday", "weekend"}:
            schedule = "weekday"
        area_slug = slugify(area_name)
        automation_filename = build_filename("automation", area_slug, f"blinds_{schedule}")
        context = {
            "area_name": area_slug,
            "blinds": blinds,
            "schedule_type": schedule,
            "days": ["mon", "tue", "wed", "thu", "fri"] if schedule == "weekday" else ["sat", "sun"],
            "blinds_up": f"{InputType.INPUT_DATETIME}.{build_object_id(area_name, f'blinds_up_{schedule}')}",
            "blinds_down": f"{InputType.INPUT_DATETIME}.{build_object_id(area_name, f'blinds_down_{schedule}')}",
            "automation_alias": build_alias("Automation", area_name, f"Blinds {schedule.title()}"),
        }
        self._create_from_template(automation_filename, "blinds_automation_template.yaml", context)

    def create_temperature_automation(self, area_name: str, temp_sensor: str, heat_switch: str, cool_switch: str):
        """Render and write a climate automation for an area."""
        area_slug = slugify(area_name)
        automation_filename = build_filename("automation", area_slug, "climate")
        context = {
            "area_name": area_slug,
            "temp_sensor": temp_sensor,
            "heat_switch": heat_switch,
            "cool_switch": cool_switch,
            "automation_alias": build_alias("Automation", area_name, "Climate"),
        }
        self._create_from_template(automation_filename, "temperature_automation_template.yaml", context)

    def update_entity_in_automations(self, old_entity: str, new_entity: str):
        """Replace an entity id in all automation YAML files."""
        updated_files = []
        for yaml_file in self.automations_dir.glob("*.yaml"):
            content = yaml_file.read_text(encoding="utf-8")
            if old_entity in content:
                yaml_file.write_text(content.replace(old_entity, new_entity), encoding="utf-8")
                updated_files.append(yaml_file.name)
                log.info(f"Updated {yaml_file.name}: {old_entity} -> {new_entity}")

        if updated_files:
            log.ok(f"Entities updated in {len(updated_files)} automation(s).")
        else:
            log.warn(f"no automations found containing {old_entity}.")


