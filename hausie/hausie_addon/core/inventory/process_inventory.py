import json
from pathlib import Path
from collections import OrderedDict
from .registry_manager import RegistryManager
from ...constants import Labels, EntityType
from ..flow_logger import get_logger

log = get_logger("core")


class InventoryProcessor:
    def __init__(self, data_dir: Path = None, output_dir: Path = None):
        """Initialize inventory input and output directories."""
        base_dir = Path(__file__).resolve().parents[3] / "hausie" / "homeassistant" / "data"
        self.data_dir = data_dir or base_dir
        self.output_dir = output_dir or base_dir
        self.raw_file = self.data_dir / "raw.json"
        self.inventory_file = self.output_dir / "inventory.json"
        self.registry = RegistryManager()

    # ---------------------------
    # Carga de archivos
    # ---------------------------
    def _load_raw(self) -> dict:
        """Load a raw JSON snapshot from disk."""
        if not self.raw_file.exists():
            raise FileNotFoundError(f"Raw data not found: {self.raw_file}")
        with open(self.raw_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Raw data must be a JSON object.")
        return data

    def _load_inventory(self) -> dict:
        """Load a cleaned inventory JSON file."""
        if not self.inventory_file.exists():
            raise FileNotFoundError(f"Inventory not found: {self.inventory_file}")
        with open(self.inventory_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_users(self) -> list[dict]:
        """Return the user list from the raw snapshot."""
        raw = self._load_raw()
        users = raw.get("users", [])
        return users if isinstance(users, list) else []

    # ---------------------------
    # Procesamiento principal (snapshot completo)
    # ---------------------------
    def process(self):
        """Build cleaned inventory files from raw Home Assistant data."""
        # Cargar datos raw
        raw = self._load_raw()
        areas_raw = raw.get("areas", [])
        devices_raw = raw.get("devices", [])
        entities_raw = raw.get("entities", [])
        services_raw = raw.get("services", [])
        users_raw = raw.get("users", [])

        # Dispositivos base
        devices = []
        for device in devices_raw:
            identifiers = device.get("identifiers", [])
            platform = identifiers[0][0] if identifiers and isinstance(identifiers[0], list) else None
            devices.append({
                "id": device.get("id"),
                "area_id": device.get("area_id"),
                "platform": platform,
                "name_by_user": device.get("name_by_user"),
                "name": device.get("name"),
                "labels": device.get("labels"),
                "entities": [],
                "services": {}
            })

        # Mapear entidades a dispositivos
        entities_by_device = {}
        for entity in entities_raw:
            device_id = entity.get("device_id")
            entity_id = entity.get("entity_id")
            if device_id and entity_id:
                entities_by_device.setdefault(device_id, []).append(entity_id)

        # Mapear servicios por dominio
        services_by_domain = {
            service["domain"]: list(service.get("services", {}).keys())
            for service in services_raw if "domain" in service
        }

        # Adjuntar entidades y servicios a cada device
        for device in devices:
            device_id = device.get("id")
            device["entities"] = entities_by_device.get(device_id, [])
            domains = {eid.split(".", 1)[0] for eid in device["entities"] if "." in eid}
            for domain in domains:
                device["services"][domain] = services_by_domain.get(domain, [])

        # Agrupar devices por área
        devices_by_area = {}
        for device in devices:
            area_id = device.get("area_id")
            devices_by_area.setdefault(area_id, []).append(device)

        # Construir áreas enriquecidas
        areas = []
        for area in areas_raw:
            area_id = area.get("area_id")
            area_devices = devices_by_area.get(area_id, [])
            device_list = [device.get("name_by_user") or device.get("name") for device in area_devices]

            area_dict = OrderedDict()
            area_dict["area_id"] = area_id
            area_dict["floor_id"] = area.get("floor_id")
            area_dict["name"] = area.get("name")
            area_dict["labels"] = area.get("labels")
            area_dict["device_list"] = device_list
            area_dict["devices"] = area_devices

            areas.append(area_dict)

        # Eliminar area_id duplicado dentro de cada device
        for area in areas:
            for device in area["devices"]:
                device.pop("area_id", None)

        # Guardar inventario completo
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_file = self.inventory_file
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"areas": areas, "users": users_raw if isinstance(users_raw, list) else []}, f, indent=2)

        log.ok(f"Inventory saved to: {output_file}")

    # -----------------------------------
    # Métodos auxiliares de selección
    # -----------------------------------
    @staticmethod
    def get_main_entity_from_device(dev: dict) -> str | None:
        """Return the shortest entity id for a device."""
        entities = dev.get("entities", [])
        if not entities:
            return None
        return min(entities, key=lambda eid: len(eid.split(".", 1)[1]) if "." in eid else len(eid))

    @staticmethod
    def get_entity_matching_string(dev: dict, match_string: str) -> str | None:
        """Return the shortest entity id containing a substring."""
        entities = dev.get("entities", [])
        match_string = match_string.lower()
        filtered = [eid for eid in entities if match_string in eid.lower()]
        if not filtered:
            return None
        return min(filtered, key=lambda eid: len(eid.split(".", 1)[1]) if "." in eid else len(eid))

    # ---------------------------
    # Detección de entidades por dispositivo (in-place)
    # ---------------------------
    def add_entities_by_device(self, device: dict, area_entities: list):
        """Append detected entities for a device into the area list."""
        device_name = device.get("name_by_user") or device.get("name") or "unknown"
        device_id = device.get("id") or device.get("name") or "unknown"
        labels = device.get("labels", []) or []
        entities_to_add = []

        def add_entity(entity_id, types):
            """Append a typed entity entry to the working list."""
            if entity_id:
                # Normaliza types/labels a listas
                tlist = types if isinstance(types, list) else [types] if types else []
                llist = labels if isinstance(labels, list) else [labels] if labels else []
                entities_to_add.append({
                    "entity_id": entity_id,
                    "device": device_name,
                    "device_id": device_id,
                    "labels": llist,
                    "types": tlist
                })

        # Battery
        add_entity(self.get_entity_matching_string(device, EntityType.BATTERY), [EntityType.BATTERY])

        # Lights
        if Labels.PRIMARY_LIGHT in labels or Labels.SECONDARY_LIGHT in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN, EntityType.LIGHT])

        # Temperature & Humidity
        if Labels.TEMPERATURE in labels:
            add_entity(self.get_entity_matching_string(device, EntityType.TEMPERATURE), [EntityType.TEMPERATURE, EntityType.MAIN])
            add_entity(self.get_entity_matching_string(device, EntityType.HUMIDITY), [EntityType.HUMIDITY])

        # Motion
        if Labels.MOTION in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN])
            add_entity(self.get_entity_matching_string(device, "occupancy"), [EntityType.MOTION])
            add_entity(self.get_entity_matching_string(device, "illuminance"), [EntityType.LUX])

        # Blinds
        if Labels.BLIND in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN, EntityType.COVER])

        # Plant
        if Labels.PLANT in labels:
            add_entity(self.get_entity_matching_string(device, EntityType.HUMIDITY), [EntityType.MAIN, EntityType.PLANT])

        # Heating Appliances
        if Labels.HEATING in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN, EntityType.HEATING])

        # Cooling appliances
        if Labels.COOLING in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN, EntityType.COOLING])

        # buttons 
        if Labels.BUTTON in labels:
            add_entity(self.get_main_entity_from_device(device), [EntityType.MAIN, EntityType.BUTTON])
            
        # ---- NUEVO: asegurar que exista una 'main' usando tu método ----
        has_main = any(
            isinstance(e, dict) and "types" in e and (EntityType.MAIN in (e.get("types") or []))
            for e in entities_to_add
        )
        if not has_main:
            main_eid = self.get_main_entity_from_device(device)
            if main_eid:
                # si ya existe esa entidad en la lista, solo marcar 'main'
                existing = next((e for e in entities_to_add if e.get("entity_id") == main_eid), None)
                if existing:
                    if EntityType.MAIN not in (existing.get("types") or []):
                        existing["types"] = (existing.get("types") or []) + [EntityType.MAIN]
                else:
                    add_entity(main_eid, [EntityType.MAIN])
        # Extiende in-place
        for e in entities_to_add:
            if isinstance(e, dict) and e.get("entity_id"):
                area_entities.append(e)

    # ---------------------------
    # Ensamble de entidades por área
    # ---------------------------
    def populate_entities_registry(self):
        """Build a map of area ids to detected entities from cleaned inventory."""
        inventory = self._load_inventory()
        detected_entities = {}

        for area in inventory.get("areas", []):
            area_id = area.get("area_id")
            area_entities = []

            for device in area.get("devices", []):
                self.add_entities_by_device(device, area_entities)

            # saneo final
            area_entities = [
                e for e in area_entities
                if isinstance(e, dict) and e.get("entity_id")
            ]
            detected_entities[area_id] = area_entities

        return detected_entities

    # ---------------------------
    # Inserción incremental de un device en el inventario cleaned
    # ---------------------------
    def add_device_to_inventory(self, device: dict, area_id: str) -> dict:
        """Upsert a device into cleaned inventory files."""
        # 1) Normalizar device (sin area_id interno, coherente con process())
        dev_to_store = {
            "id": device.get("id"),
            "platform": device.get("platform"),
            "name_by_user": device.get("name_by_user"),
            "name": device.get("name"),
            "labels": device.get("labels"),
            "entities": device.get("entities", []),
            "services": device.get("services", {}),
        }
        if not dev_to_store["id"]:
            raise ValueError("El device debe incluir 'id'.")

        # 2) Cargar inventario actual (o crear estructura vacía)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            inventory = self._load_inventory()
        except Exception:
            inventory = {"areas": []}

        areas = inventory.get("areas") or []

        # 3) Buscar/crear el área por area_id
        idx = next((i for i, a in enumerate(areas) if a.get("area_id") == area_id), None)
        if idx is None:
            area_dict = OrderedDict()
            area_dict["area_id"] = area_id
            area_dict["floor_id"] = None
            area_dict["name"] = None
            area_dict["labels"] = None
            area_dict["device_list"] = []
            area_dict["devices"] = []
            areas.append(area_dict)
            idx = len(areas) - 1

        # 4) Upsert del device por id dentro del área
        devices_list = areas[idx].get("devices", [])
        ex_idx = next((i for i, d in enumerate(devices_list) if d.get("id") == dev_to_store["id"]), None)
        if ex_idx is None:
            devices_list.append(dev_to_store)
        else:
            devices_list[ex_idx] = dev_to_store
        areas[idx]["devices"] = devices_list

        # 5) Recalcular device_list (nombres mostrados)
        areas[idx]["device_list"] = [(d.get("name_by_user") or d.get("name")) for d in devices_list]

        # 6) Guardar inventario completo
        with open(self.inventory_file, "w", encoding="utf-8") as f:
            json.dump({"areas": areas}, f, indent=2)

        log.ok(f"Device '{dev_to_store['id']}' added/updated in area '{area_id}'.")
        return dev_to_store
