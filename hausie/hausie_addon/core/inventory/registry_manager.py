import json
from pathlib import Path
from typing import List, Dict, Optional

from ..flow_logger import get_logger

log = get_logger("core")


class RegistryManager:
    """RegistryManager stores areas, entities, groups, scripts, automations, inputs, and switches."""

    def __init__(self, registry_file: Path = None):
        """Initialize the registry manager and load persisted data."""
        self.registry_file = (
            registry_file
            or Path(__file__).resolve().parents[3] / "hausie" / "homeassistant" / "data" / "registry.json"
        )
        self.data = self._load()

    # ------------------------------
    # IO
    # ------------------------------
    def _load(self) -> Dict:
        """Load registry file or initialize if not found."""
        if self.registry_file.exists():
            with open(self.registry_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"areas": [], "users": []}

        data.setdefault("users", [])
        data.setdefault("labels", [])

        # Normalize keys for backward compatibility
        for area in data.get("areas", []):
            area.setdefault("labels", [])
            area.setdefault("devices", [])
            area.setdefault("entities", [])
            area.setdefault("groups", [])
            area.setdefault("scripts", [])
            area.setdefault("automations", [])
            area.setdefault("inputs", [])
            area.setdefault("switches", [])

        return data

    def _save(self) -> None:
        """Save current state into registry file."""
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.registry_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    # ------------------------------
    # Areas
    # ------------------------------
    def add_area(self, area_id: str, name: str, labels: list = None) -> None:
        """Add a new area to the registry if missing."""
        if not any(a["area_id"] == area_id for a in self.data["areas"]):
            self.data["areas"].append({
                "area_id": area_id,
                "name": name,
                "labels": labels or [],
                "devices": [],
                "entities": [],
                "groups": [],
                "scripts": [],
                "automations": [],
                "inputs": [],
                "switches": [],
            })
            self._save()

    def update_area(self, area_id: str, name: str | None = None, labels: list | None = None) -> None:
        """Update an existing area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        if name is not None:
            area["name"] = name
        if labels is not None:
            area["labels"] = list(labels)
        self._save()

    def get_area(self, area_id: str) -> Optional[Dict]:
        """Return an area record by id."""
        return next((a for a in self.data["areas"] if a["area_id"] == area_id), None)

    def delete_area(self, area_id: str) -> None:
        """Remove an area from the registry."""
        before = len(self.data["areas"])
        self.data["areas"] = [a for a in self.data["areas"] if a["area_id"] != area_id]
        if len(self.data["areas"]) < before:
            log.ok(f"Deleted area {area_id}")
            self._save()
        else:
            log.warn(f"Warning: area {area_id} not found")

    def reset_area_collections(
        self,
        area_id: str,
        *,
        devices: bool = False,
        entities: bool = False,
        groups: bool = True,
        scripts: bool = True,
        automations: bool = True,
        inputs: bool = True,
        switches: bool = True,
    ) -> None:
        """Clear registry collections for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        if devices:
            area["devices"] = []
        if entities:
            area["entities"] = []
        if groups:
            area["groups"] = []
        if scripts:
            area["scripts"] = []
        if automations:
            area["automations"] = []
        if inputs:
            area["inputs"] = []
        if switches:
            area["switches"] = []
        self._save()

    def list_areas(self) -> List[Dict]:
        """Return all areas in the registry."""
        return list(self.data.get("areas", []))

    def get_area_ids(self) -> List[str]:
        """Return a list of area ids."""
        return [a["area_id"] for a in self.data.get("areas", [])]

    # ------------------------------
    # Users
    # ------------------------------
    @staticmethod
    def _normalize_user(user: Dict) -> Optional[Dict]:
        """Normalize a user entry for storage."""
        if not isinstance(user, dict):
            return None
        user_id = user.get("id") or user.get("user_id")
        name = user.get("name")
        if not user_id or name is None:
            return None
        is_owner = user.get("isOwner")
        if is_owner is None:
            is_owner = user.get("is_owner")
        is_admin = user.get("isAdmin")
        if is_admin is None:
            is_admin = user.get("is_admin")
        return {
            "id": user_id,
            "name": name,
            "isOwner": bool(is_owner),
            "isAdmin": bool(is_admin),
        }

    def set_users(self, users: List[Dict]) -> bool:
        """Replace the user list in the registry."""
        cleaned = []
        seen = set()
        for user in users or []:
            parsed = self._normalize_user(user)
            if not parsed:
                continue
            if parsed["id"] in seen:
                continue
            seen.add(parsed["id"])
            cleaned.append(parsed)
        previous = self.data.get("users", [])
        if cleaned == previous:
            return False
        self.data["users"] = cleaned
        self._save()
        return True

    def list_users(self) -> List[Dict]:
        """Return all users in the registry."""
        return list(self.data.get("users", []))

    # ------------------------------
    # Labels
    # ------------------------------
    @staticmethod
    def _normalize_label(label: Dict) -> Optional[Dict]:
        """Normalize a label entry for storage."""
        if not isinstance(label, dict):
            return None
        label_id = label.get("label_id") or label.get("id")
        name = label.get("name") or label.get("label")
        if not label_id or name is None:
            return None
        normalized = {
            "id": label_id,
            "name": name,
        }
        if "icon" in label:
            normalized["icon"] = label.get("icon")
        if "color" in label:
            normalized["color"] = label.get("color")
        return normalized

    def set_labels(self, labels: List[Dict]) -> bool:
        """Replace the label list in the registry."""
        cleaned = []
        seen = set()
        for label in labels or []:
            parsed = self._normalize_label(label)
            if not parsed:
                continue
            label_id = parsed["id"]
            if label_id in seen:
                continue
            seen.add(label_id)
            cleaned.append(parsed)
        previous = self.data.get("labels", [])
        if cleaned == previous:
            return False
        self.data["labels"] = cleaned
        self._save()
        return True

    def list_labels(self) -> List[Dict]:
        """Return all labels in the registry."""
        return list(self.data.get("labels", []))

    # ------------------------------
    # Groups
    # ------------------------------
    def add_group(self, area_id: str, group_id: str, name: str, entities: List[str] = None) -> None:
        """Add or update a group definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return

        group_entry = {
            "id": group_id,
            "name": name,
            "entities": list(entities or []),
        }

        groups = area.setdefault("groups", [])
        existing = next((g for g in groups if g.get("id") == group_id), None)
        if existing:
            existing.update(group_entry)
        else:
            groups.append(group_entry)
        self._save()

    def update_group(self, area_id: str, group_id: str, name: str | None = None, entities: List[str] | None = None) -> None:
        """Update a group definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        existing = next((g for g in area.get("groups", []) if g.get("id") == group_id), None)
        if not existing:
            log.warn(f"Group {group_id} not found in {area_id}")
            return
        if name is not None:
            existing["name"] = name
        if entities is not None:
            existing["entities"] = list(entities)
        self._save()

    def get_group(self, area_id: str, group_id: str) -> Optional[Dict]:
        """Return a group definition by id."""
        area = self.get_area(area_id)
        if not area:
            return None
        return next((g for g in area.get("groups", []) if g.get("id") == group_id), None)

    def list_groups(self, area_id: str) -> List[Dict]:
        """Return all groups for an area."""
        area = self.get_area(area_id)
        if not area:
            return []
        return list(area.get("groups", []))

    def delete_group(self, area_id: str, group_id: str) -> None:
        """Remove a group definition from an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Warning: area {area_id} not found")
            return
        before = len(area.get("groups", []))
        area["groups"] = [g for g in area.get("groups", []) if g.get("id") != group_id]
        if len(area["groups"]) < before:
            log.ok(f"Deleted group {group_id} from {area_id}")
            self._save()
        else:
            log.warn(f"Warning: group {group_id} not found in {area_id}")

    # ------------------------------
    # Scripts
    # ------------------------------
    def add_script(
        self,
        area_id: str,
        script_id: str,
        name: str,
        entities: List[str] = None,
        device_name: str | None = None,
    ) -> None:
        """Add or update a script definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return

        script_entry = {
            "id": script_id,
            "name": name,
            "entities": list(entities or []),
        }
        if device_name:
            script_entry["device_name"] = device_name

        scripts = area.setdefault("scripts", [])
        existing = next((s for s in scripts if s.get("id") == script_id), None)
        if existing:
            existing.update(script_entry)
        else:
            scripts.append(script_entry)
        self._save()

    def update_script(
        self,
        area_id: str,
        script_id: str,
        name: str | None = None,
        entities: List[str] | None = None,
        device_name: str | None = None,
    ) -> None:
        """Update a script definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        existing = next((s for s in area.get("scripts", []) if s.get("id") == script_id), None)
        if not existing:
            log.warn(f"Script {script_id} not found in {area_id}")
            return
        if name is not None:
            existing["name"] = name
        if entities is not None:
            existing["entities"] = list(entities)
        if device_name is not None:
            if device_name:
                existing["device_name"] = device_name
            else:
                existing.pop("device_name", None)
        self._save()

    def get_script(self, area_id: str, script_id: str) -> Optional[Dict]:
        """Return a script definition by id."""
        area = self.get_area(area_id)
        if not area:
            return None
        return next((s for s in area.get("scripts", []) if s.get("id") == script_id), None)

    def list_scripts(self, area_id: str) -> List[Dict]:
        """Return all scripts for an area."""
        area = self.get_area(area_id)
        if not area:
            return []
        return list(area.get("scripts", []))

    def delete_script(self, area_id: str, script_id: str) -> None:
        """Remove a script definition from an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Warning: area {area_id} not found")
            return
        before = len(area.get("scripts", []))
        area["scripts"] = [s for s in area.get("scripts", []) if s.get("id") != script_id]
        if len(area["scripts"]) < before:
            log.ok(f"Deleted script {script_id} from {area_id}")
            self._save()
        else:
            log.warn(f"Warning: script {script_id} not found in {area_id}")

    # ------------------------------
    # Entities (solo entity_id, múltiples tipos en 'types')
    # ------------------------------
    def _find_entity_record(self, area: Dict, entity_id: str) -> Optional[Dict]:
        """Find an entity record inside an area by entity id."""
        return next((e for e in area.get("entities", []) if e.get("entity_id") == entity_id), None)

    def _normalize_entity(self, ent: Dict) -> Optional[Dict]:
        """Normalize an entity entry for storage."""
        if not isinstance(ent, dict):
            return None
        entity_id = ent.get("entity_id")
        if not entity_id:
            return None
        types = ent.get("types") or []
        labels = ent.get("labels") or []
        return {
            "entity_id": entity_id,
            "device": ent.get("device") or "unknown",
            "device_id": ent.get("device_id"),
            "types": list(set(types)) if isinstance(types, list) else [types],
            "labels": list(set(labels)) if isinstance(labels, list) else [labels],
        }

    def set_entities(self, area_id: str, entities: List[Dict]) -> None:
        """Replace the entity list for an area with a normalized list."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        cleaned = []
        for ent in entities or []:
            parsed = self._normalize_entity(ent)
            if parsed:
                cleaned.append(parsed)
        area["entities"] = cleaned
        self._save()

    # ------------------------------
    # Devices
    # ------------------------------
    def _normalize_device(self, dev: Dict) -> Optional[Dict]:
        """Normalize a device entry for storage."""
        if not isinstance(dev, dict):
            return None
        dev_id = dev.get("id")
        if not dev_id:
            return None
        return {
            "id": dev_id,
            "name": dev.get("name"),
            "name_by_user": dev.get("name_by_user"),
            "platform": dev.get("platform"),
            "labels": dev.get("labels") or [],
            "entities": dev.get("entities") or [],
        }

    def add_device(self, area_id: str, device: Dict) -> None:
        """Add or update a device for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        dev = self._normalize_device(device)
        if not dev:
            log.skip("add_device: device is invalid, skipping")
            return
        devices = area.setdefault("devices", [])
        existing = next((d for d in devices if d.get("id") == dev["id"]), None)
        if existing:
            existing.update(dev)
        else:
            devices.append(dev)
        self._save()

    def set_devices(self, area_id: str, devices: List[Dict]) -> None:
        """Replace the device list for an area with a normalized list."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Warning: area {area_id} not found")
            return

        cleaned = []
        for dev in devices or []:
            parsed = self._normalize_device(dev)
            if parsed:
                cleaned.append(parsed)

        area["devices"] = cleaned
        self._save()

    def get_device(self, area_id: str, device_id: str) -> Optional[Dict]:
        """Return a device record by id for an area."""
        area = self.get_area(area_id)
        if not area:
            return None
        return next((d for d in area.get("devices", []) if d.get("id") == device_id), None)

    def update_device(
        self,
        area_id: str,
        device_id: str,
        name: str | None = None,
        name_by_user: str | None = None,
        platform: str | None = None,
        labels: List[str] | None = None,
        entities: List[str] | None = None,
    ) -> None:
        """Update a device record for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        device = next((d for d in area.get("devices", []) if d.get("id") == device_id), None)
        if not device:
            log.warn(f"Device {device_id} not found in {area_id}")
            return
        if name is not None:
            device["name"] = name
        if name_by_user is not None:
            device["name_by_user"] = name_by_user
        if platform is not None:
            device["platform"] = platform
        if labels is not None:
            device["labels"] = list(labels)
        if entities is not None:
            device["entities"] = list(entities)
        self._save()

    def delete_device(self, area_id: str, device_id: str) -> None:
        """Remove a device record from an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        before = len(area.get("devices", []))
        area["devices"] = [d for d in area.get("devices", []) if d.get("id") != device_id]
        if len(area["devices"]) < before:
            log.ok(f"Deleted device {device_id} from {area_id}")
            self._save()
        else:
            log.warn(f"Device {device_id} not found in {area_id}")

    def list_devices(self, area_id: str) -> List[Dict]:
        """Return the device list for an area."""
        area = self.get_area(area_id)
        if not area:
            return []
        return list(area.get("devices", []))

    def add_entity(
        self,
        area_id: str,
        entity_id: str,
        device: str,
        device_id: str,
        types: List[str] = None,
        labels: List[str] = None,
    ) -> None:
        """Add or update an entity record for an area."""
        if not entity_id:
            log.skip("add_entity: entity_id is empty, skipping")
            return

        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return

        ent = self._find_entity_record(area, entity_id)
        if ent is None:
            new_ent = {
                "entity_id": entity_id,
                "device": device or "unknown",
                "device_id": device_id,
                "types": list(set(types)) if types else [],
                "labels": list(set(labels)) if labels else [],
            }
            area["entities"].append(new_ent)
            self._save()
            return

        if device:
            ent["device"] = device

        ent["types"] = list(set(ent.get("types", [])).union(types or []))
        ent["labels"] = list(set(ent.get("labels", [])).union(labels or []))
        self._save()

    def update_entity(
        self,
        area_id: str,
        entity_id: str,
        device: str | None = None,
        device_id: str | None = None,
        types: List[str] | None = None,
        labels: List[str] | None = None,
    ) -> None:
        """Update an entity record for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        ent = self._find_entity_record(area, entity_id)
        if not ent:
            log.warn(f"Entity {entity_id} not found in {area_id}")
            return
        if device is not None:
            ent["device"] = device or ent.get("device")
        if device_id is not None:
            ent["device_id"] = device_id
        if types is not None:
            ent["types"] = list(set(types))
        if labels is not None:
            ent["labels"] = list(set(labels))
        self._save()

    def get_entity(self, area_id: str, entity_id: str) -> Optional[Dict]:
        """Return an entity record by id for an area."""
        area = self.get_area(area_id)
        if not area:
            return None
        return self._find_entity_record(area, entity_id)

    def delete_entity(self, area_id: str, entity_id: str) -> None:
        """Delete an entity record from an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Warning: area {area_id} not found")
            return
        before = len(area["entities"])
        area["entities"] = [e for e in area["entities"] if e.get("entity_id") != entity_id]
        if len(area["entities"]) < before:
            log.ok(f"Deleted entity {entity_id} from {area_id}")
            self._save()
        else:
            log.warn(f"Warning: entity {entity_id} not found in {area_id}")

    def delete_entity_type(self, area_id: str, entity_id: str, entity_type: str) -> None:
        """Remove a type from an entity and delete the entity if empty."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Warning: area {area_id} not found")
            return
        ent = self._find_entity_record(area, entity_id)
        if not ent:
            log.warn(f"Warning: entity {entity_id} not found in {area_id}")
            return
        if "types" not in ent or not isinstance(ent["types"], list):
            ent["types"] = []

        if entity_type in ent["types"]:
            ent["types"].remove(entity_type)

        if not ent["types"]:
            # sin types -> eliminar la entity completa
            area["entities"] = [e for e in area["entities"] if e.get("entity_id") != entity_id]
            log.ok(f"Deleted entity {entity_id} (no types left) from {area_id}")
        self._save()

    def list_entities(self, area_id: str) -> List[Dict]:
        """Return all entities for an area."""
        area = self.get_area(area_id)
        if not area:
            return []
        return list(area.get("entities", []))

    def get_entities_by_type(self, area_id: str, entity_type: str) -> List[Dict]:
        """Return entities whose types include the requested type."""
        area = self.get_area(area_id)
        if not area:
            return []
        out = []
        for e in area.get("entities", []):
            types = e.get("types", [])
            if isinstance(types, list) and entity_type in types:
                out.append(e)
        return out

    # ------------------------------
    # Inputs
    # ------------------------------
    def add_input(self, area_id: str, input_id: str, name: str, input_type: str) -> None:
        """Add an input definition to an area."""
        area = self.get_area(area_id)
        if area:
            if not any(i["id"] == input_id for i in area["inputs"]):
                area["inputs"].append({
                    "id": input_id,
                    "name": name,
                    "type": input_type
                })
                self._save()

    def update_input(self, area_id: str, input_id: str, name: str | None = None, input_type: str | None = None) -> None:
        """Update an input definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        existing = next((i for i in area.get("inputs", []) if i.get("id") == input_id), None)
        if not existing:
            log.warn(f"Input {input_id} not found in {area_id}")
            return
        if name is not None:
            existing["name"] = name
        if input_type is not None:
            existing["type"] = input_type
        self._save()

    def get_input(self, area_id: str, input_id: str) -> Optional[Dict]:
        """Return an input definition by id."""
        area = self.get_area(area_id)
        if area:
            return next((i for i in area["inputs"] if i["id"] == input_id), None)
        return None

    def list_inputs(self, area_id: str) -> List[Dict]:
        """Return all inputs for an area."""
        area = self.get_area(area_id)
        return list(area.get("inputs", [])) if area else []

    def delete_input(self, area_id: str, input_id: str) -> None:
        """Remove an input definition from an area."""
        area = self.get_area(area_id)
        if area:
            before = len(area["inputs"])
            area["inputs"] = [i for i in area["inputs"] if i["id"] != input_id]
            if len(area["inputs"]) < before:
                log.ok(f"Deleted input {input_id} from {area_id}")
                self._save()
            else:
                log.warn(f"Warning: input {input_id} not found in {area_id}")

    # ------------------------------
    # Switches
    # ------------------------------
    def add_switch(self, area_id: str, switch_id: str, name: str, switch_type: str | None = None, data: Dict | None = None) -> None:
        """Add a switch definition to an area."""
        area = self.get_area(area_id)
        if area:
            if not any(s["id"] == switch_id for s in area["switches"]):
                entry = {"id": switch_id, "name": name}
                if switch_type:
                    entry["type"] = switch_type
                if data:
                    entry["data"] = data
                area["switches"].append(entry)
                self._save()

    def update_switch(self, area_id: str, switch_id: str, name: str | None = None, switch_type: str | None = None, data: Dict | None = None) -> None:
        """Update a switch definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        existing = next((s for s in area.get("switches", []) if s.get("id") == switch_id), None)
        if not existing:
            log.warn(f"Switch {switch_id} not found in {area_id}")
            return
        if name is not None:
            existing["name"] = name
        if switch_type is not None:
            existing["type"] = switch_type
        if data is not None:
            existing["data"] = data
        self._save()

    def get_switch(self, area_id: str, switch_id: str) -> Optional[Dict]:
        """Return a switch definition by id."""
        area = self.get_area(area_id)
        if area:
            return next((s for s in area.get("switches", []) if s.get("id") == switch_id), None)
        return None

    def list_switches(self, area_id: str) -> List[Dict]:
        """Return all switches for an area."""
        area = self.get_area(area_id)
        return list(area.get("switches", [])) if area else []

    def delete_switch(self, area_id: str, switch_id: str) -> None:
        """Remove a switch definition from an area."""
        area = self.get_area(area_id)
        if area:
            before = len(area["switches"])
            area["switches"] = [s for s in area["switches"] if s.get("id") != switch_id]
            if len(area["switches"]) < before:
                log.ok(f"Deleted switch {switch_id} from {area_id}")
                self._save()
            else:
                log.warn(f"Warning: switch {switch_id} not found in {area_id}")

    # ------------------------------
    # Automations
    # ------------------------------
    def add_automation(self, area_id: str, automation_id: str, name: str) -> None:
        """Add an automation definition to an area."""
        area = self.get_area(area_id)
        if area:
            if not any(a["id"] == automation_id for a in area["automations"]):
                area["automations"].append({
                    "id": automation_id,
                    "name": name
                })
                self._save()

    def update_automation(self, area_id: str, automation_id: str, name: str | None = None) -> None:
        """Update an automation definition for an area."""
        area = self.get_area(area_id)
        if not area:
            log.warn(f"Area {area_id} not found")
            return
        existing = next((a for a in area.get("automations", []) if a.get("id") == automation_id), None)
        if not existing:
            log.warn(f"Automation {automation_id} not found in {area_id}")
            return
        if name is not None:
            existing["name"] = name
        self._save()

    def get_automation(self, area_id: str, automation_id: str) -> Optional[Dict]:
        """Return an automation definition by id."""
        area = self.get_area(area_id)
        if area:
            return next((a for a in area["automations"] if a["id"] == automation_id), None)
        return None

    def list_automations(self, area_id: str) -> List[Dict]:
        """Return all automations for an area."""
        area = self.get_area(area_id)
        return list(area.get("automations", [])) if area else []

    def delete_automation(self, area_id: str, automation_id: str) -> None:
        """Remove an automation definition from an area."""
        area = self.get_area(area_id)
        if area:
            before = len(area["automations"])
            area["automations"] = [a for a in area["automations"] if a["id"] != automation_id]
            if len(area["automations"]) < before:
                log.ok(f"Deleted automation {automation_id} from {area_id}")
                self._save()
            else:
                log.warn(f"Warning: automation {automation_id} not found in {area_id}")

    # ------------------------------
    # Reset
    # ------------------------------
    def reset(self) -> None:
        """Clear the registry data and persist the empty state."""
        self.data = {"areas": [], "users": [], "labels": []}
        self._save()
