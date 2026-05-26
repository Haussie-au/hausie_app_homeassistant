from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class InventoryComparator:
    """Compare two inventory snapshots and report differences."""

    def __init__(self) -> None:
        self.area_fields = ("name", "floor_id", "labels")
        self.device_fields = ("name", "name_by_user", "platform", "labels", "entities", "services")

    def compare_files(self, old_path: Path, new_path: Path) -> Dict[str, Any]:
        """Load two inventory files and compare them."""
        with open(old_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        with open(new_path, "r", encoding="utf-8") as f:
            new_data = json.load(f)
        return self.compare(old_data, new_data)

    def compare(self, old_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, Any]:
        """Compare two inventory dictionaries."""
        old_areas = self._index_areas(old_data)
        new_areas = self._index_areas(new_data)

        added_areas = sorted(set(new_areas) - set(old_areas))
        removed_areas = sorted(set(old_areas) - set(new_areas))

        changed_areas: List[Dict[str, Any]] = []
        for area_id in sorted(set(old_areas).intersection(new_areas)):
            old_area = old_areas[area_id]
            new_area = new_areas[area_id]

            area_changes = self._diff_fields(
                old_area,
                new_area,
                self.area_fields,
                list_fields={"labels"},
            )

            device_changes = self._diff_devices(
                old_area.get("devices", []),
                new_area.get("devices", []),
            )

            if area_changes or device_changes["added"] or device_changes["removed"] or device_changes["changed"]:
                changed_areas.append({
                    "area_id": area_id,
                    "field_changes": area_changes,
                    "devices_added": device_changes["added"],
                    "devices_removed": device_changes["removed"],
                    "devices_changed": device_changes["changed"],
                })

        return {
            "areas_added": added_areas,
            "areas_removed": removed_areas,
            "areas_changed": changed_areas,
        }

    def _index_areas(self, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        areas = {}
        for area in data.get("areas", []) or []:
            if isinstance(area, dict) and area.get("area_id"):
                areas[area["area_id"]] = area
        return areas

    def _diff_devices(self, old_devices: List[Dict[str, Any]], new_devices: List[Dict[str, Any]]) -> Dict[str, Any]:
        old_map = {d.get("id"): d for d in old_devices if isinstance(d, dict) and d.get("id")}
        new_map = {d.get("id"): d for d in new_devices if isinstance(d, dict) and d.get("id")}

        added = sorted(set(new_map) - set(old_map))
        removed = sorted(set(old_map) - set(new_map))
        changed = []

        for device_id in sorted(set(old_map).intersection(new_map)):
            old_dev = old_map[device_id]
            new_dev = new_map[device_id]
            changes = self._diff_fields(
                old_dev,
                new_dev,
                self.device_fields,
                list_fields={"labels", "entities"},
                dict_fields={"services"},
            )
            if changes:
                changed.append({
                    "device_id": device_id,
                    "field_changes": changes,
                })

        return {"added": added, "removed": removed, "changed": changed}

    def _diff_fields(
        self,
        old_obj: Dict[str, Any],
        new_obj: Dict[str, Any],
        fields: tuple[str, ...],
        *,
        list_fields: set[str] | None = None,
        dict_fields: set[str] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        list_fields = list_fields or set()
        dict_fields = dict_fields or set()
        changes: Dict[str, Dict[str, Any]] = {}

        for field in fields:
            old_val = old_obj.get(field)
            new_val = new_obj.get(field)

            if field in list_fields:
                old_norm = sorted(set(self._normalize_list(old_val)))
                new_norm = sorted(set(self._normalize_list(new_val)))
                if old_norm != new_norm:
                    changes[field] = {"old": old_norm, "new": new_norm}
                continue

            if field in dict_fields:
                old_norm = self._normalize_services(old_val)
                new_norm = self._normalize_services(new_val)
                if old_norm != new_norm:
                    changes[field] = {"old": old_norm, "new": new_norm}
                continue

            if old_val != new_val:
                changes[field] = {"old": old_val, "new": new_val}

        return changes

    def _normalize_list(self, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return [v for v in value if v is not None]
        return [value]

    def _normalize_services(self, value: Any) -> Dict[str, List[str]]:
        if not isinstance(value, dict):
            return {}
        normalized: Dict[str, List[str]] = {}
        for domain, services in value.items():
            if isinstance(services, list):
                normalized[domain] = sorted([str(s) for s in services if s is not None])
            elif services is None:
                normalized[domain] = []
            else:
                normalized[domain] = [str(services)]
        return normalized
