import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List
import copy
import os

from ..utils.naming import build_object_id, slugify, titleize

from ...constants import Labels, EntityType, InputType
from ..flow_logger import get_logger

log = get_logger("ui")

def get_icon_for_entity(entity_id: str) -> str:
    """Return the default icon for an entity id."""
    if entity_id.startswith(f"{EntityType.LIGHT}."):
        return "mdi:lightbulb"
    if entity_id.startswith("switch."):
        return "mdi:toggle-switch"
    if entity_id.startswith("fan."):
        return "mdi:fan"
    return "mdi:devices"


class DashboardCreator:
    HELP_TOGGLE_IDS = {
        "main": "ui_help_main",
        "devices": "ui_help_devices",
        "buttons": "ui_help_buttons",
        "battery": "ui_help_battery",
        "light_config": "ui_help_light_config",
        "blinds_config": "ui_help_blinds_config",
        "temperature_config": "ui_help_temperature_config",
        "notify_automation": "ui_help_notify_automation",
        "new-devices": "ui_help_new_devices",
        "users": "ui_help_users",
        "automations": "ui_help_automations",
        "hausie": "ui_help_hausie",
    }
    def __init__(
        self,
        output_yaml_path: str = "hausie/homeassistant/dashboards/hausie_dashboard.yaml",
        config_output_yaml_path: str = "hausie/homeassistant/dashboards/hausie_configuration_dashboard.yaml",
        title: str = "Hausie",
        view_title: str = "Hausie",
        view_path: str = "home",
        view_icon: str = "mdi:home-assistant",
        button_styles: Optional[Dict[str, Any]] = None,
        main_config_view_path: Optional[str] = None,
        user_names: Optional[List[str]] = None,
        subscription_plan: Optional[str] = None,
        subscription_config_path: Optional[str] = None,
    ):
        """Configure dashboard output paths and defaults."""
        self.output_yaml_path = output_yaml_path
        self.config_output_yaml_path = config_output_yaml_path
        self.title = title
        self.view_title = view_title
        self.view_path = view_path
        self.view_icon = view_icon
        self.button_styles = button_styles or {"styles": {}}
        self.main_config_view_path = main_config_view_path
        self.user_names = user_names or self._get_user_names()
        self.subscription_plan = (subscription_plan or "").strip() or None
        self.subscription_config_path = subscription_config_path
        self._subscription_button_config: Dict[str, Any] | None = None

    @staticmethod
    def _get_user_names() -> List[str]:
        raw = os.getenv("HA_USER_NAMES", "").strip()
        if raw:
            names = [n.strip() for n in raw.split(",") if n.strip()]
        else:
            names = ["Monica", "Mateo", "Guest"]
        return names

    @staticmethod
    def _filter_user_names(names: List[str]) -> List[str]:
        blocked = ("hausie", "supervisor", "home assistant content")
        return [
            name
            for name in names
            if not any(token in name.lower() for token in blocked)
        ]

    @staticmethod
    def _is_blocked_user_name(name: str) -> bool:
        blocked = ("hausie", "supervisor", "home assistant content")
        return any(token in (name or "").lower() for token in blocked)

    def _get_user_records(self, registry_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        records = []
        for user in registry_data.get("users") or []:
            if not isinstance(user, dict):
                continue
            user_id = user.get("id") or user.get("user_id")
            name = user.get("name")
            if not user_id or not name:
                continue
            records.append({
                "id": user_id,
                "name": name,
                "isOwner": bool(user.get("isOwner")),
                "isAdmin": bool(user.get("isAdmin")),
            })
        return records

    def _resolve_user_names(self, registry_data: Dict[str, Any]) -> List[str]:
        users = registry_data.get("users") or []
        names = []
        seen = set()
        for user in users:
            if not isinstance(user, dict):
                continue
            name = user.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        if names:
            return self._filter_user_names(names)
        fallback = self.user_names or self._get_user_names()
        return self._filter_user_names(fallback)

    @staticmethod
    def _apply_view_header(view: Dict[str, Any]) -> None:
        title = view.get("title")
        if not title:
            return
        header = view.get("header") or {}
        header["card"] = {
            "type": "markdown",
            "text_only": True,
            "content": f"## {title}",
        }
        view["header"] = header

    @staticmethod
    def _slug_area_name(name: str) -> str:
        """Normalize an area name into a slug."""
        return slugify(name or "unknown")

    @staticmethod
    def _prefixed_id(prefix: str, area_name: str, subject: str) -> str:
        """Return a prefixed id for a given area and subject."""
        base = build_object_id(area_name, subject)
        return f"{prefix}_{base}"

    @classmethod
    def _is_users_area_excluded(cls, area_name: str) -> bool:
        slug = cls._slug_area_name(area_name)
        return slug in {"general", "system", "configuration"}

    @staticmethod
    def _get_area_icon(area_name: str) -> str:
        """Pick an icon for an area name."""
        name = (area_name or "").lower()
        if "living" in name:
            return "mdi:sofa"
        if "bedroom" in name:
            return "mdi:bed"
        if "kitchen" in name:
            return "mdi:fridge"
        if "bathroom" in name:
            return "mdi:shower"
        if "stairs" in name:
            return "mdi:stairs"
        if "hallway" in name:
            return "mdi:walk"
        if "entrance" in name or "door" in name:
            return "mdi:door"
        return "mdi:home"

    @staticmethod
    def _is_type(ent: Dict[str, Any], t: str) -> bool:
        """Check whether an entity includes the given type."""
        t = (t or "").lower()
        ent_types = ent.get("types") or []
        if isinstance(ent_types, str):
            ent_types = [ent_types]
        ent_types = [str(x).lower() for x in ent_types if x]
        return t in ent_types

    @staticmethod
    def _build_general_lights_badge() -> Dict[str, Any]:
        """Return the global lights badge for the main view."""
        return {
            "type": "entity",
            "show_name": True,
            "show_state": True,
            "show_icon": True,
            "entity": f"group.{DashboardCreator._prefixed_id('core', 'general', 'lights')}",
            "name": "All lights",
            "icon": "mdi:lamps",
            "show_entity_picture": True,
            "tap_action": {"action": "toggle"},
        }

    @staticmethod
    def _build_general_blinds_badge() -> Dict[str, Any]:
        """Return the global blinds badge for the main view."""
        return {
            "type": "entity",
            "show_name": True,
            "show_state": True,
            "show_icon": True,
            "entity": f"cover.{DashboardCreator._prefixed_id('core', 'general', 'blinds')}",
            "name": "All blinds",
            "icon": "mdi:blinds",
            "show_entity_picture": False,
            "tap_action": {"action": "toggle"},
        }

    def _build_general_badges(self, registry_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the badges shown on the main dashboard header."""
        badges = [self._build_general_lights_badge()]
        has_covers = any(self._get_area_main_cover_entities(area) for area in registry_data.get("areas", []))
        if has_covers:
            badges.append(self._build_general_blinds_badge())
        return badges
    def _build_main_tile(self, ent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a main tile card for an eligible entity."""
        entity_id = ent.get("entity_id") or ent.get("id")

        if not entity_id:
            return None
        labels = ent.get("labels") or []
        if Labels.SYSTEM in labels:
            return None
        if Labels.DEVICES in labels or "device" in labels:
            return None
        if self._is_type(ent, EntityType.MOTION) or Labels.MOTION in labels:
            return None
        if self._is_type(ent, EntityType.BUTTON) or Labels.BUTTON in labels:
            return None
        if Labels.TEMPERATURE in labels:
            return None

        if not self._is_type(ent, EntityType.MAIN):
            return None

        # Clima (thermostato)
        if entity_id.startswith(f"{EntityType.CLIMATE}.") or self._is_type(ent, EntityType.CLIMATE):
            return {
                "type": "thermostat",
                "entity": entity_id,
                "name": ent.get("device") or entity_id.split(".")[-1].replace("_", " ").title(),
                "features": [
                    {"type": "climate-hvac-modes"}
                ],
                "grid_options": {
                    "columns": 6,
                    "rows": 4
                },
                "show_current_as_primary": False
            }

        # Cover tile (especial con cover-position)
        if entity_id.startswith(f"{EntityType.COVER}."):
            return {
                "type": "tile",
                "entity": entity_id,
                "features_position": "bottom",
                "vertical": False,
                "icon": "mdi:blinds",
                "show_entity_picture": False,
                "hide_state": False,
                "features": [
                    {"type": "cover-position"}
                ]
            }

        # Default tile (con brillo solo para luces)
        return {
            "type": "tile",
            "entity": entity_id,
            "features_position": "bottom",
            "vertical": False,
            "features": [{"type": "light-brightness"}] if entity_id.startswith(f"{EntityType.LIGHT}.") else []
        }

    def _build_lights_heading_card(self, area: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build the lights heading card for an area if groups are available."""
        area_name = area.get("name", "Unknown")
        groups = {g.get("id"): g for g in area.get("groups", []) if g.get("id")}

        all_group_id = self._prefixed_id("core", area_name, "lights")
        if all_group_id not in groups:
            return None

        return {
            "type": "heading",
            "heading": "",
            "icon": "",
            "heading_style": "subtitle",
            "badges": [
                {
                    "type": "entity",
                    "show_state": True,
                    "show_icon": True,
                        "entity": f"group.{all_group_id}",
                        "name": "All Lights",
                        "icon": "mdi:lamps",
                        "color": "orange",
                        "state_content": "name",
                        "tap_action": {
                            "action": "perform-action",
                            "perform_action": f"script.{self._prefixed_id('core', area_name, 'group_lights')}",
                            "target": {},
                        },
                    }
            ],
            "tap_action": {"action": "none"},
        }

    @staticmethod
    def _build_entity_control_tile(entity_id: str, *, name: str) -> Dict[str, Any]:
        """Build a popup control tile for a single light/switch entity."""
        features: list[dict[str, Any]] = []
        icon = None
        if entity_id.startswith(f"{EntityType.LIGHT}."):
            features = [{"type": "light-brightness"}]
        else:
            features = [{"type": "toggle"}]
        if entity_id.startswith("group."):
            features = [{"type": "toggle"}]
            icon = "mdi:lamps"
        elif entity_id.startswith("switch."):
            icon = "mdi:toggle-switch"
        elif entity_id.startswith(f"{EntityType.LIGHT}."):
            icon = "mdi:lightbulb"

        card = {
            "type": "tile",
            "entity": entity_id,
            "name": name,
            "vertical": False,
            "hide_state": False,
            "features_position": "bottom",
            "features": features,
        }
        if icon:
            card["icon"] = icon
        return card

    @staticmethod
    def _interleave_card_columns(columns: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Arrange cards so each source column stays in the same visual grid column."""
        ordered: List[Dict[str, Any]] = []
        max_len = max((len(column) for column in columns), default=0)
        for row in range(max_len):
            for column in columns:
                if row < len(column):
                    ordered.append(column[row])
        return ordered

    def _build_light_group_popup_content(
        self,
        area: Dict[str, Any],
        *,
        group_entity: str,
        group_name: str,
        entities: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build popup content for a primary/secondary lights tile."""
        typed_columns: List[List[Dict[str, Any]]] = []

        lights_cards = []
        other_cards = []
        for index, ent in enumerate(entities, start=1):
            entity_id = ent.get("entity_id") or ent.get("id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            device_name = (
                ent.get("device")
                or ent.get("name")
                or titleize(entity_id.split(".", 1)[1])
                or f"Light {index}"
            )
            tile = self._build_entity_control_tile(entity_id, name=device_name)
            if entity_id.startswith(f"{EntityType.LIGHT}."):
                lights_cards.append(tile)
            else:
                other_cards.append(tile)

        if other_cards:
            typed_columns.append(other_cards)
        if lights_cards:
            typed_columns.append(lights_cards)

        popup_cards = [
            self._build_entity_control_tile(group_entity, name=group_name),
        ]
        if typed_columns:
            popup_cards.append(
                {
                    "type": "grid",
                    "columns": max(1, len(typed_columns)),
                    "square": False,
                    "cards": self._interleave_card_columns(typed_columns),
                }
            )

        area_name = area.get("name", "Unknown")
        return {
            "title": f"{titleize(area_name)} - Lights Control",
            "content": {
                "type": "vertical-stack",
                "cards": popup_cards,
            },
        }

    def _build_light_group_tiles(self, area: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build primary/secondary lights tiles with popup controls."""
        area_name = area.get("name", "Unknown")
        groups = {g.get("id"): g for g in area.get("groups", []) if g.get("id")}
        desired = [
            (self._prefixed_id("core", area_name, "primary_lights"), "Primary Lights"),
            (self._prefixed_id("core", area_name, "secondary_lights"), "Secondary Lights"),
        ]
        entity_map = {
            ent.get("entity_id"): ent
            for ent in area.get("entities", []) or []
            if isinstance(ent, dict) and ent.get("entity_id")
        }

        tiles: List[Dict[str, Any]] = []
        for group_id, fallback_name in desired:
            group = groups.get(group_id)
            if not isinstance(group, dict):
                continue
            group_entities = []
            for entity_id in group.get("entities", []) or []:
                ent = entity_map.get(entity_id)
                if isinstance(ent, dict):
                    group_entities.append(ent)
            if not group_entities:
                continue

            group_name = group.get("name") or fallback_name
            popup = self._build_light_group_popup_content(
                area,
                group_entity=f"group.{group_id}",
                group_name=group_name,
                entities=group_entities,
            )
            tiles.append(
                {
                    "type": "tile",
                    "entity": f"group.{group_id}",
                    "name": fallback_name,
                    "icon": "mdi:lamps",
                    "hide_state": False,
                    "vertical": False,
                    "tap_action": {
                        "action": "fire-dom-event",
                        "browser_mod": {
                            "service": "browser_mod.popup",
                            "data": popup,
                        },
                    },
                }
            )
        return tiles

    def _get_area_main_cover_entities(self, area: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return main cover entities for an area."""
        covers: List[Dict[str, Any]] = []
        seen = set()
        for ent in area.get("entities", []) or []:
            if not isinstance(ent, dict):
                continue
            entity_id = ent.get("entity_id") or ent.get("id")
            if not isinstance(entity_id, str) or not entity_id.startswith(f"{EntityType.COVER}."):
                continue
            if not self._is_type(ent, EntityType.COVER) or not self._is_type(ent, EntityType.MAIN):
                continue
            if entity_id in seen:
                continue
            seen.add(entity_id)
            covers.append(ent)
        return covers

    @classmethod
    def _build_blinds_group_entity_id(cls, area_name: str) -> str:
        """Return the grouped cover entity id for an area."""
        return f"cover.{cls._prefixed_id('core', area_name, 'blinds')}"

    def _build_blinds_group_tile(self, area: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build the grouped blinds tile with popup controls for one area."""
        area_name = area.get("name", "Unknown")
        covers = self._get_area_main_cover_entities(area)
        if not covers:
            return None

        popup_cards = []
        for index, ent in enumerate(covers, start=1):
            entity_id = ent.get("entity_id")
            if not entity_id:
                continue
            device_name = (
                ent.get("device")
                or ent.get("name")
                or titleize(entity_id.split(".", 1)[1])
                or f"Blind {index}"
            )
            popup_cards.append(
                {
                    "type": "tile",
                    "entity": entity_id,
                    "name": device_name,
                    "icon": "mdi:blinds",
                    "vertical": False,
                    "hide_state": False,
                    "features_position": "bottom",
                    "features": [{"type": "cover-position"}],
                }
            )

        if not popup_cards:
            return None

        return {
            "type": "tile",
            "entity": self._build_blinds_group_entity_id(area_name),
            "name": "All blinds",
            "icon": "mdi:blinds",
            "hide_state": True,
            "vertical": False,
            "features": [{"type": "cover-position"}],
            "features_position": "bottom",
            "tap_action": {
                "action": "fire-dom-event",
                "browser_mod": {
                    "service": "browser_mod.popup",
                    "data": {
                        "title": f"{titleize(area_name)} - Blinds Control",
                        "content": {
                            "type": "vertical-stack",
                            "cards": popup_cards,
                        },
                    },
                },
            },
        }

    def _build_visibility_conditions(
        self,
        registry_data: Dict[str, Any],
        *,
        scope_name: str,
        require_toggle: bool,
        allow_admin: bool,
        allow_blocked: bool,
    ) -> List[Dict[str, Any]]:
        conditions = []
        records = self._get_user_records(registry_data)
        if records:
            for user in records:
                user_id = user["id"]
                user_name = user["name"]
                is_admin = bool(user.get("isOwner") or user.get("isAdmin"))
                is_blocked = self._is_blocked_user_name(user_name)
                if is_blocked and not allow_blocked:
                    continue
                if is_admin and allow_admin and not require_toggle:
                    conditions.append({
                        "condition": "user",
                        "users": [user_id],
                    })
                    continue
                if is_blocked and allow_blocked and not require_toggle:
                    conditions.append({
                        "condition": "user",
                        "users": [user_id],
                    })
                    continue
                if require_toggle:
                    toggle_id = self._prefixed_id("perm", scope_name, user_name)
                    conditions.append({
                        "condition": "and",
                        "conditions": [
                            {"condition": "user", "users": [user_id]},
                            {
                                "condition": "state",
                                "entity": f"{InputType.INPUT_BOOLEAN}.{toggle_id}",
                                "state_not": "off",
                            },
                        ],
                    })
        else:
            for user_name in self._resolve_user_names(registry_data):
                toggle_id = self._prefixed_id("perm", scope_name, user_name)
                conditions.append({
                    "condition": "state",
                    "entity": f"{InputType.INPUT_BOOLEAN}.{toggle_id}",
                    "state_not": "off",
                })

        if not conditions:
            return []
        return [{"condition": "or", "conditions": conditions}]

    def _expand_hausie_sections_for_users(
        self,
        section: Dict[str, Any],
        area_name: str,
        registry_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        records = self._get_user_records(registry_data)
        if not records:
            return [section]
        conditions = []
        for user in records:
            user_id = user["id"]
            user_name = user["name"]
            if not self._is_blocked_user_name(user_name):
                toggle_id = self._prefixed_id("perm", area_name, user_name)
                conditions.append({
                    "condition": "and",
                    "conditions": [
                        {"condition": "user", "users": [user_id]},
                        {
                            "condition": "state",
                            "entity": f"{InputType.INPUT_BOOLEAN}.{toggle_id}",
                            "state_not": "off",
                        },
                    ],
                })
            else:
                conditions.append({"condition": "user", "users": [user_id]})

        if not conditions:
            return [section]

        section_copy = copy.deepcopy(section)
        section_copy["visibility"] = [{"condition": "or", "conditions": conditions}]
        return [section_copy]

    def _apply_config_view_visibility(self, views: List[Dict[str, Any]], registry_data: Dict[str, Any]) -> None:
        records = self._get_user_records(registry_data)
        admin_ids = [
            user["id"]
            for user in records
            if user.get("id") and (user.get("isOwner") or user.get("isAdmin"))
        ]
        if not admin_ids:
            return

        admin_visibility = {"condition": "user", "users": admin_ids}

        admin_only_names = {
            "Update Add-on",
            "Rebuild Hausie",
            "Users",
            "Areas",
            "Devices",
            "Restart Hausie",
            "Add Zigbee Device",
            "Add Device",
        }
        admin_only_badge_entities = {
            f"{InputType.INPUT_BOOLEAN}.allow_remote_support",
        }

        for view in views:
            if not isinstance(view, dict):
                continue
            view_path = view.get("path")
            if view_path == "new-devices":
                view["visibility"] = [admin_visibility]

            if view_path != "main":
                continue

            for section in view.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                for card in section.get("cards") or []:
                    if not isinstance(card, dict):
                        continue
                    card_name = (card.get("name") or "").strip()
                    if card_name in admin_only_names:
                        card["visibility"] = self._replace_admin_visibility(
                            card.get("visibility"), admin_visibility
                        )

            for badge in view.get("badges") or []:
                if not isinstance(badge, dict):
                    continue
                badge_entity = (badge.get("entity") or "").strip()
                if badge_entity in admin_only_badge_entities:
                    badge["visibility"] = self._replace_admin_visibility(
                        badge.get("visibility"), admin_visibility
                    )

    @staticmethod
    def _replace_admin_visibility(
        existing: list | None, admin_visibility: Dict[str, Any]
    ) -> list[Dict[str, Any]]:
        if not isinstance(existing, list):
            existing = []
        updated: list[Dict[str, Any]] = []
        replaced = False
        for condition in existing:
            if isinstance(condition, dict) and condition.get("condition") == "user":
                if not replaced:
                    updated.append(admin_visibility)
                    replaced = True
                continue
            updated.append(condition)
        if not replaced:
            updated.append(admin_visibility)
        return updated

    def _generate_dashboard_dict(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the main dashboard dictionary from registry data."""
        dashboard = {
            "title": self.title,
            "views": [
                {
                    "type": "sections",
                    "title": self.view_title,
                    "path": self.view_path,
                    "icon": self.view_icon,
                    "badges": self._build_general_badges(registry_data),
                    "header": {
                        "card": {
                            "type": "markdown",
                            "text_only": True,
                            "content": "# Hello {{ user }}",
                        }
                    },
                    "sections": [],
                }
            ],
        }

        sections = []
        general_section = None

        for area in registry_data.get("areas", []):
            area_display_name = area.get("name", "Unknown Area")
            area_slug = self._slug_area_name(area_display_name)
            if area_slug == "system":
                continue

            if area_slug == "general":
                section = {
                    "type": "grid",
                    "cards": [
                        {
                            "type": "heading",
                            "heading": "General",
                            "icon": "mdi:home",
                            "heading_style": "title",
                            "badges": [],
                        },
                        {
                            "type": "tile",
                            "entity": "sensor.date",
                            "color": "grey",
                            "vertical": False,
                            "features_position": "bottom",
                        },
                        {
                            "type": "tile",
                            "entity": "weather.forecast_home",
                            "name": {"type": "device"},
                            "color": "blue",
                            "show_entity_picture": False,
                            "hide_state": False,
                            "state_content": ["state", "temperature"],
                            "vertical": False,
                            "features": [],
                            "features_position": "bottom",
                        },
                    ],
                    "column_span": 4,
                }
                general_section = section
                continue

            badges = []
            for ent in area.get("entities", []):
                eid = ent.get("entity_id") or ent.get("id")
                if not eid:
                    continue
                if self._is_type(ent, EntityType.TEMPERATURE):
                    badges.append({"type": "entity", "entity": eid})
                if self._is_type(ent, EntityType.HUMIDITY):
                    badges.append({"type": "entity", "entity": eid})

            cards = [
                {
                    "type": "heading",
                    "heading": area_slug,
                    "icon": self._get_area_icon(area_slug),
                    "heading_style": "title",
                    "badges": badges,
                }
            ]

            light_group_tiles = self._build_light_group_tiles(area)
            cards.extend(light_group_tiles)
            grouped_light_entities = set()
            if light_group_tiles:
                for group in area.get("groups", []) or []:
                    if not isinstance(group, dict):
                        continue
                    group_id = group.get("id") or ""
                    if group_id.endswith("primary_lights") or group_id.endswith("secondary_lights"):
                        grouped_light_entities.update(group.get("entities", []) or [])

            blinds_group_tile = self._build_blinds_group_tile(area)
            if blinds_group_tile:
                cards.append(blinds_group_tile)

            for ent in area.get("entities", []):
                entity_id = ent.get("entity_id") or ent.get("id")
                if light_group_tiles and entity_id in grouped_light_entities:
                    continue
                if (
                    blinds_group_tile
                    and self._is_type(ent, EntityType.COVER)
                    and self._is_type(ent, EntityType.MAIN)
                ):
                    continue
                card = self._build_main_tile(ent)
                if card:
                    cards.append(card)

            if len(cards) > 1:
                section = {"type": "grid", "cards": cards}
                if area_slug == "general":
                    general_section = section
                else:
                    if self._is_users_area_excluded(area_display_name):
                        sections.append(section)
                    else:
                        sections.extend(
                            self._expand_hausie_sections_for_users(
                                section,
                                area_display_name,
                                registry_data,
                            )
                        )

        if general_section:
            sections.insert(0, general_section)

        help_section = self._build_help_section(
            view_path="hausie",
            message="This is help for the Hausie dashboard.",
            column_span=4,
        )
        sections.insert(0, help_section)

        dashboard["views"][0]["sections"] = sections
        return dashboard
    def _load_external_view(self, yaml_path: str) -> Optional[Dict[str, Any]]:
        """Load an external view YAML from disk."""
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            log.warn(f"Failed to load external view from {yaml_path}: {e}")
        return None

    def set_subscription_plan(self, plan: str | None) -> None:
        """Set the subscription plan for feature gating."""
        self.subscription_plan = (plan or "").strip() or None

    def _resolve_subscription_config_path(self) -> Path | None:
        path = (self.subscription_config_path or "").strip()
        if not path:
            path = os.getenv("HAUSIE_SUBSCRIPTION_CONFIG_PATH", "").strip()
        if not path:
            default_path = Path(__file__).resolve().parents[2] / "config" / "subscription_dashboard_buttons.yaml"
            return default_path
        return Path(path)

    def _load_subscription_button_config(self) -> Dict[str, Any] | None:
        if self._subscription_button_config is not None:
            return self._subscription_button_config
        path = self._resolve_subscription_config_path()
        if not path or not path.exists():
            self._subscription_button_config = None
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warn(f"Failed to load subscription config from {path}: {exc}")
            self._subscription_button_config = None
            return None
        if not isinstance(data, dict):
            self._subscription_button_config = None
            return None
        self._subscription_button_config = data
        return data

    def _resolve_subscription_plan(self, config: Dict[str, Any]) -> str:
        plan = (self.subscription_plan or "").strip()
        if not plan:
            plan = str(config.get("default_plan") or "plan 1")
        return plan.strip().lower()

    def _apply_subscription_button_rules(self, views: List[Dict[str, Any]]) -> None:
        config = self._load_subscription_button_config()
        if not config:
            return
        buttons = config.get("buttons") or {}
        plan_order = config.get("plan_order") or []
        if not isinstance(buttons, dict):
            return
        if isinstance(plan_order, str):
            plan_order = [plan_order]
        plan_order = [str(p).strip() for p in plan_order if str(p).strip()]
        if not plan_order:
            plan_order = ["plan 1", "plan 2", "plan 3", "plan 4"]

        plan = self._resolve_subscription_plan(config)
        plan_key = plan.strip().lower()
        plan_ranks = {str(name).strip().lower(): idx for idx, name in enumerate(plan_order)}
        current_rank = plan_ranks.get(plan_key)
        if current_rank is None:
            default_plan = str(config.get("default_plan") or plan_order[0]).strip().lower()
            current_rank = plan_ranks.get(default_plan, 0)

        def _is_unlocked(min_plan: str | None) -> bool:
            if not min_plan:
                return True
            min_key = str(min_plan).strip().lower()
            min_rank = plan_ranks.get(min_key)
            if min_rank is None:
                return True
            return current_rank >= min_rank

        name_to_key = {}
        for key, meta in buttons.items():
            if not isinstance(meta, dict):
                continue
            name = (meta.get("name") or "").strip()
            if name:
                name_to_key[name] = key

        if not name_to_key:
            return

        for view in views:
            if not isinstance(view, dict):
                continue
            if view.get("path") != "main":
                continue
            for section in view.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                for card in section.get("cards") or []:
                    if not isinstance(card, dict):
                        continue
                    if card.get("type") not in {"button", "tile"}:
                        continue
                    card_name = (card.get("name") or "").strip()
                    if not card_name:
                        continue
                    key = name_to_key.get(card_name)
                    if not key:
                        continue
                    meta = buttons.get(key) or {}
                    min_plan = meta.get("min_plan") if isinstance(meta, dict) else None
                    if _is_unlocked(min_plan):
                        overrides = meta.get("plan_overrides") if isinstance(meta, dict) else None
                        if isinstance(overrides, dict):
                            override = overrides.get(plan_key) or overrides.get(plan)
                            if isinstance(override, dict):
                                tap_action = override.get("tap_action")
                                if isinstance(tap_action, dict):
                                    card["tap_action"] = tap_action
                                visibility = (override.get("visibility") or "").strip().lower()
                                if visibility == "all":
                                    card.pop("visibility", None)
                        continue

                    upgrade = meta.get("upgrade") if isinstance(meta, dict) else None
                    if not isinstance(upgrade, dict):
                        upgrade = {}
                    plan_name = upgrade.get("plan_name") or str(min_plan or "plan 2")
                    feature_name = upgrade.get("feature_name") or card_name
                    cta_url = upgrade.get("cta_url") or "https://hausie.app/upgrade"
                    card["tap_action"] = self._build_upgrade_popup_action(
                        plan_name=plan_name,
                        feature_name=feature_name,
                        cta_url=cta_url,
                        popup=upgrade,
                    )

    @staticmethod
    def _ensure_devices_button_icon(view: Dict[str, Any]) -> None:
        """Ensure the Devices button has an icon in the main view."""
        if not isinstance(view, dict):
            return
        sections = view.get("sections")
        if not isinstance(sections, list):
            return
        for section in sections:
            cards = section.get("cards") if isinstance(section, dict) else None
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                if card.get("name") == "Devices":
                    card.setdefault("icon", "mdi:devices")
                    card.setdefault("show_icon", True)

    def _build_upgrade_popup_action(
        self,
        *,
        plan_name: str,
        feature_name: str,
        cta_url: str,
        popup: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        message = self._render_upgrade_popup_markdown(
            popup or {},
            plan_name=plan_name,
            feature_name=feature_name,
            cta_url=cta_url,
        )
        return {
            "action": "fire-dom-event",
            "browser_mod": {
                "service": "browser_mod.popup",
                "data": {
                    "title": self._render_upgrade_popup_title(popup or {}, plan_name=plan_name),
                    "dismissable": True,
                    "content": {
                        "type": "markdown",
                        "content": f"<br><br>\n{message}\n\n<br><br>",
                        "text_only": True,
                    },
                    "left_button": "back",
                    "left_button_action": {
                        "action": "call-service",
                        "service": "browser_mod.close_popup",
                    },
                },
            },
        }

    @staticmethod
    def _render_upgrade_popup_markdown(
        popup: Dict[str, Any],
        *,
        plan_name: str,
        feature_name: str,
        cta_url: str,
    ) -> str:
        if isinstance(popup, dict):
            raw = popup.get("popup_markdown")
            if not raw:
                popup_block = popup.get("popup")
                if isinstance(popup_block, dict):
                    raw = popup_block.get("markdown") or popup_block.get("content")
                    if not raw:
                        body = popup_block.get("body")
                        footer = popup_block.get("footer")
                        if body or footer:
                            raw = f"{body or ''}\n\n{footer or ''}".strip()
            if isinstance(raw, str) and raw.strip():
                return DashboardCreator._interpolate_upgrade_text(
                    raw, plan_name, feature_name, cta_url
                )
        return (
            f"## Upgrade your plan to {plan_name} to be able to use {feature_name}\n\n"
            f"Do it in just one click in ({cta_url})"
        )

    @staticmethod
    def _render_upgrade_popup_title(popup: Dict[str, Any], *, plan_name: str) -> str:
        if isinstance(popup, dict):
            popup_block = popup.get("popup")
            if isinstance(popup_block, dict):
                title = popup_block.get("title")
                if isinstance(title, str) and title.strip():
                    return DashboardCreator._interpolate_upgrade_text(title, plan_name, "", "")
        return "Upgrade"

    @staticmethod
    def _interpolate_upgrade_text(
        text: str, plan_name: str, feature_name: str, cta_url: str
    ) -> str:
        values = {
            "plan_name": plan_name,
            "feature_name": feature_name,
            "cta_url": cta_url,
        }
        result = text
        for key, value in values.items():
            result = result.replace(f"{{{{ {key} }}}}", value)
            result = result.replace(f"{{{{{key}}}}}", value)
            result = result.replace(f"{{{key}}}", value)
        return result

    def _build_help_section(
        self,
        *,
        view_path: str,
        message: str,
        heading: str = "Help",
        icon: str = "mdi:help",
        column_span: int = 4,
    ) -> Dict[str, Any]:
        """Build a help section that can be dismissed via an input_boolean."""
        helper_id = self.HELP_TOGGLE_IDS.get(view_path) or f"ui_help_{slugify(view_path)}"
        entity_id = f"{InputType.INPUT_BOOLEAN}.{helper_id}"
        message_id = f"ui_help_message_{slugify(view_path)}"
        message_entity = f"{InputType.INPUT_TEXT}.{message_id}"
        close_badge = {
            "type": "entity",
            "show_state": False,
            "show_icon": True,
            "entity": entity_id,
            "icon": "mdi:close",
            "tap_action": {
                "action": "call-service",
                "service": "input_boolean.turn_off",
                "target": {"entity_id": entity_id},
            },
        }
        visibility = [{"condition": "state", "entity": entity_id, "state": "on"}]
        return {
            "type": "grid",
            "cards": [
                {
                    "type": "heading",
                    "heading": heading,
                    "heading_style": "subtitle",
                    "icon": icon,
                    "badges": [close_badge],
                    "grid_options": {"columns": 48, "rows": "auto"},
                    "visibility": visibility,
                },
                {
                    "type": "markdown",
                    "content": f"{{{{ states('{message_entity}') }}}}",
                    "text_only": True,
                    "grid_options": {"columns": 48, "rows": "auto"},
                    "visibility": visibility,
                },
            ],
            "column_span": column_span,
        }
    def _generate_config_dashboard_dict(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the configuration dashboard dictionary."""
        views = []

        if self.main_config_view_path:
            external_view = self._load_external_view(self.main_config_view_path)
            if external_view:
                self._ensure_devices_button_icon(external_view)
                views.append(external_view)

        views.extend([
            self._generate_devices_view(registry_data),
            self.create_battery_config_view(registry_data),
            self.create_users_view(registry_data),
            self.create_automations_view(),
            self.create_buttons_config_view(registry_data),
            self.create_light_config_view(registry_data),
            self.create_blinds_config_view(registry_data),
            self.create_temperature_config_view(registry_data),
            self.create_notify_automation_view(registry_data),
            self.create_new_devices_view(),
        ])
        self._apply_config_view_visibility(views, registry_data)
        self._apply_subscription_button_rules(views)
        return {"views": views}

    def create_from_registry(self, registry_data: Dict[str, Any]) -> None:
        """Write the main dashboard YAML from registry data."""
        dashboard_dict = self._generate_dashboard_dict(registry_data)
        Path(self.output_yaml_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(dashboard_dict, f, sort_keys=False, allow_unicode=True)
        log.ok(f"Main dashboard created at {self.output_yaml_path}")

    def create_config_dashboard(self, registry_data: Dict[str, Any]) -> None:
        """Write the configuration dashboard YAML from registry data."""
        config_dict = self._generate_config_dashboard_dict(registry_data)
        Path(self.config_output_yaml_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_output_yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_dict, f, sort_keys=False, allow_unicode=True)
        log.ok(f"Config dashboard created at {self.config_output_yaml_path}")

    def create_new_devices_view(self) -> Dict[str, Any]:
        """Build the hidden New Devices view."""
        help_text = (
            "Remember to add common and short names to the new devices, "
            "this will help you use them easily. please select one of the following "
            "labels that apply to the kind of device you are adding\n\n"
            "Labels\n"
            "  * a"
        )
        view = {
            "type": "sections",
            "max_columns": 4,
            "title": "New Devices",
            "path": "new-devices",
            "icon": "mdi:new-box",
            "sections": [
                self._build_help_section(
                    view_path="new-devices",
                    message=help_text,
                    heading="Help",
                    column_span=4,
                ),
                {
                    "type": "grid",
                    "cards": [
                        {
                            "type": "heading",
                            "heading": "New devices",
                            "heading_style": "title",
                        },
                    ],
                }
            ],
            "header": {
                "layout": "start",
                "badges_position": "bottom",
                "badges_wrap": "wrap",
            },
        }
        self._apply_view_header(view)
        return view

        # === BUILDERS UNITARIOS ===
    def build_device_main_button_card(self, ent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a device button card for the Devices view."""
        if not self._is_type(ent, EntityType.MAIN):
            return None
        eid = ent.get("entity_id") or ent.get("id")
        device_id = ent.get("device_id")
        device_name = ent.get("device")
        labels = ent.get("labels") or []
        if not eid or not device_id:
            return None

        icon = None
        if Labels.BUTTON in labels or self._is_type(ent, EntityType.BUTTON):
            icon = "mdi:gesture-tap-button"
        elif ent.get("icon"):
            icon = ent["icon"]

        return {
            "type": "custom:button-card",
            "entity": eid,
            "name": device_name,
            "show_state": True,
            "state_display": (
                "[[[\n"
                "  if (!entity || !entity.state) return '';\n"
                "  if (entity.state === 'unavailable') return 'Unavailable';\n"
                "  if (entity.state === 'unknown') return 'Unknown';\n"
                "  return '';\n"
                "]]]"
            ),
            **({"icon": icon} if icon else {}),
            "grid_options": {"columns": 6},
            "tap_action": {
                "action": "navigate",
                "navigation_path": f"/config/devices/device/{device_id}"
            },
            "layout": "icon_name",
            "styles": copy.deepcopy(self.button_styles)
        }

    def build_battery_tile(self, ent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a tile card for a battery entity."""
        labels = ent.get("labels") or []
        if Labels.SYSTEM in labels or Labels.DEVICES in labels or "device" in labels:
          return None
        if not self._is_type(ent, EntityType.BATTERY):
            return None
        
        eid = ent.get("entity_id") or ent.get("id")
        if not eid:
            return None
        name = ent.get("device") or eid.split(".")[-1].replace("_", " ").title()
        return {
            "type": "tile",
            "name": name,
            "entity": eid,
            "tap_action": {"action": "none"},
            "icon_tap_action": {"action": "none"},
        }

    def build_light_config_cards_for_area(self, area: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a light configuration section for an area."""
        area_name = area.get("name", "Unknown")
        light_auto_id = self._prefixed_id("auto", area_name, "light")
        light_switch_id = f"{light_auto_id}_switch"
        lux_threshold_id = self._prefixed_id("auto", area_name, "lux_threshold")
        lights_delay_id = self._prefixed_id("auto", area_name, "lights_delay")
        primary_toggle_id = self._prefixed_id("auto", area_name, "primary_lights_toggle")
        secondary_toggle_id = self._prefixed_id("auto", area_name, "secondary_lights_toggle")

        has_light_automation = any(
            auto.get("id") == self._prefixed_id("auto", area_name, "light")
            for auto in area.get("automations", [])
        )
        if not has_light_automation:
            return None

        section = {"type": "grid", "cards": []}

        # Header + toggle maestro
        section["cards"].append({
            "type": "heading",
            "heading": area_name,
            "icon": self._get_area_icon(area_name),
            "heading_style": "title",
            "badges": [
                {
                    "type": "entity",
                    "show_state": True,
                    "show_icon": True,
                    "entity": f"switch.{light_switch_id}",
                    "tap_action": {"action": "toggle"},
                    "color": "accent",
                }
            ]
        })

        # Lux (primero con type "lux")
        lux_entity = next(
            (e.get("entity_id") for e in area.get("entities", []) if self._is_type(e, EntityType.LUX)),
            None
        )
        if lux_entity:
            section["cards"].append({"type": "tile", "name": "Illuminance", "entity": lux_entity})

        # Motion (por label 'motion' o type 'motion')
        motion_entity = next(
            (e.get("entity_id") for e in area.get("entities", [])
             if self._is_type(e, EntityType.MOTION)),
            None
        )
        if motion_entity:
            section["cards"].append({
                "type": "tile",
                "name": "Motion",
                "entity": motion_entity,
                "features_position": "bottom",
                "vertical": False
            })

        # Inputs de configuración
        section["cards"].append({
            "type": "entities",
            "entities": [
                {"entity": f"{InputType.INPUT_NUMBER}.{lux_threshold_id}", "name": "Lux Threshold"},
                {"entity": f"{InputType.INPUT_NUMBER}.{lights_delay_id}", "name": "Lights Delay"}
            ]
        })

        # Texto + toggles de grupos
        section["cards"].append({
            "type": "markdown",
            "content": "Select the devices that will turn on with motion",
            "text_only": True
        })
        section["cards"].append({
            "type": "tile",
            "features_position": "bottom",
            "vertical": False,
            "entity": f"{InputType.INPUT_BOOLEAN}.{primary_toggle_id}",
            "name": "Primary Lights"
        })
        section["cards"].append({
            "type": "tile",
            "features_position": "bottom",
            "vertical": False,
            "entity": f"{InputType.INPUT_BOOLEAN}.{secondary_toggle_id}",
            "name": "Secondary Lights"
        })

        return section

    def build_blinds_config_cards_for_area(self, area: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a blinds configuration section for an area."""
        area_name = area.get("name", "Unknown")
        weekday_auto_id = self._prefixed_id("auto", area_name, "blinds_weekday")
        weekend_auto_id = self._prefixed_id("auto", area_name, "blinds_weekend")
        weekday_switch_id = f"{weekday_auto_id}_switch"
        weekend_switch_id = f"{weekend_auto_id}_switch"

        has_cover = any(
            (e.get("entity_id", "").startswith(f"{EntityType.COVER}.") and self._is_type(e, EntityType.MAIN))
            for e in area.get("entities", [])
        )
        if not has_cover:
            return None

        section = {"type": "grid", "cards": []}

        # Weekday + Weekend
        for time_of_day in ["Weekday", "Weekend"]:
            time_key = time_of_day.lower()

            section["cards"].append({
                "type": "heading",
                "heading": area_name if time_key == "weekday" else "",
                "heading_style": "title" if time_key == "weekday" else "subtitle",
                "icon": self._get_area_icon(area_name) if time_key == "weekday" else "",
                "badges": [{
                    "type": "entity",
                    "entity": f"switch.{weekday_switch_id if time_key == 'weekday' else weekend_switch_id}",
                    "show_state": True,
                    "show_icon": True,
                    "tap_action": {"action": "toggle"}
                }]
            })

            section["cards"].append({
                "type": "entities",
                "title": time_of_day,
                "entities": [
                    {
                        "entity": f"{InputType.INPUT_DATETIME}.{self._prefixed_id('auto', area_name, f'blinds_up_{time_key}')}",
                        "name": "Blinds up",
                        "icon": "mdi:blinds-open",
                        "secondary_info": "none"
                    },
                    {
                        "entity": f"{InputType.INPUT_DATETIME}.{self._prefixed_id('auto', area_name, f'blinds_down_{time_key}')}",
                        "name": "Blinds Down",
                        "icon": "mdi:roller-shade-closed"
                    }
                ],
                "grid_options": {"columns": 12, "rows": "auto"}
            })

        return section


        # === VISTAS QUE SOLO ITERAN Y LLAMAN BUILDERS ===
    def _generate_devices_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the devices view sections."""
        view = {
            "type": "sections",
            "path": "devices",
            "title": "Devices",
            "icon": "mdi:devices",
            "sections": [],
            "subview": True,
        }
        view["sections"].append(
            self._build_help_section(
                view_path="devices",
                message="This is help for device view",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            section = {"type": "grid", "columns": 2, "square": False, "cards": []}
            section["cards"].append({
                "type": "heading",
                "heading": area.get("name", "Unknown"),
                "icon": "mdi:sofa",
                "heading_style": "title",
                "grid_options": {"columns": 12}
            })

            for ent in area.get("entities", []):
                card = self.build_device_main_button_card(ent)
                if card:
                    section["cards"].append(card)

            if len(section["cards"]) > 1:
                view["sections"].append(section)

        self._apply_view_header(view)
        return view

    def create_light_config_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the light automation configuration view."""
        view = {
            "type": "sections",
            "title": "Light Automation",
            "path": "light_config",
            "icon": "mdi:lightbulb-group",
            "sections": []
        }
        view["sections"].append(
            self._build_help_section(
                view_path="light_config",
                message="This is help for light automation view.",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            area_name = area.get("name", "Unknown")
            section = self.build_light_config_cards_for_area(area)
            if not section:
                continue
            if self._is_users_area_excluded(area_name):
                view["sections"].append(section)
            else:
                view["sections"].extend(
                    self._expand_hausie_sections_for_users(
                        section,
                        area_name,
                        registry_data,
                    )
                )

        view["max_columns"] = 4
        view["subview"] = True
        view.setdefault("cards", [])
        self._apply_view_header(view)
        return view

    def create_buttons_config_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the buttons configuration view."""
        view = {
            "type": "sections",
            "title": "Buttons",
            "path": "buttons",
            "icon": "mdi:gesture-tap-button",
            "sections": [],
        }
        view["sections"].append(
            self._build_help_section(
                view_path="buttons",
                message="This is help for buttons view.",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            area_name = area.get("name", "Unknown")
            button_auto_id = self._prefixed_id("auto", area_name, "button")
            button_switch_id = f"{button_auto_id}_switch"
            button_single_id = build_object_id(area_name, "button_single")
            button_double_id = build_object_id(area_name, "button_double")
            button_long_id = build_object_id(area_name, "button_long")
            seen = set()
            buttons = []
            for ent in area.get("entities", []):
                if not (self._is_type(ent, EntityType.BUTTON) or Labels.BUTTON in (ent.get("labels") or [])):
                    continue
                device_id = ent.get("device_id")
                device_name = ent.get("device")
                key = device_id or device_name or ent.get("entity_id")
                if not key or key in seen:
                    continue
                seen.add(key)
                buttons.append(ent)
            if not buttons:
                continue

            section = {"type": "grid", "cards": []}
            section["cards"].append({
                "type": "heading",
                "heading_style": "title",
                "heading": area_name,
                "icon": self._get_area_icon(area_name),
            })

            for idx, ent in enumerate(buttons, start=1):
                button_name = ent.get("device") or ent.get("entity_id") or f"Button {idx}"
                section["cards"].append({
                    "type": "heading",
                    "heading_style": "subtitle",
                    "heading": button_name,
                    "badges": [
                        {
                            "type": "entity",
                            "show_state": True,
                            "show_icon": True,
                            "entity": f"switch.{button_switch_id}",
                            "tap_action": {"action": "toggle"},
                            "color": "accent",
                        }
                    ],
                })
                section["cards"].append({
                    "type": "entities",
                    "entities": [
                        {"entity": f"{InputType.INPUT_SELECT}.{button_single_id}", "name": "Single Click"},
                        {"entity": f"{InputType.INPUT_SELECT}.{button_double_id}", "name": "Double Click"},
                        {"entity": f"{InputType.INPUT_SELECT}.{button_long_id}", "name": "Long Click"},
                    ],
                })

            if self._is_users_area_excluded(area_name):
                view["sections"].append(section)
            else:
                view["sections"].extend(
                    self._expand_hausie_sections_for_users(
                        section,
                        area_name,
                        registry_data,
                    )
                )

        view["max_columns"] = 4
        view["subview"] = True
        view.setdefault("cards", [])
        self._apply_view_header(view)
        return view

    def create_blinds_config_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the blinds configuration view."""
        view = {
            "type": "sections",
            "title": "Blinds Configuration",
            "path": "blinds_config",
            "icon": "mdi:blinds",
            "sections": []
        }
        view["sections"].append(
            self._build_help_section(
                view_path="blinds_config",
                message="This is help for blinds configuration view.",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            area_name = area.get("name", "Unknown")
            section = self.build_blinds_config_cards_for_area(area)
            if not section:
                continue
            if self._is_users_area_excluded(area_name):
                view["sections"].append(section)
            else:
                view["sections"].extend(
                    self._expand_hausie_sections_for_users(
                        section,
                        area_name,
                        registry_data,
                    )
                )

        view["max_columns"] = 4
        view["subview"] = True
        view.setdefault("cards", [])
        self._apply_view_header(view)
        return view

    def create_temperature_config_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the temperature automation configuration view."""
        view = {
            "type": "sections",
            "title": "Temperature Automations",
            "path": "temperature_config",
            "icon": "mdi:thermometer",
            "sections": [],
            "subview": True,
        }
        view["sections"].append(
            self._build_help_section(
                view_path="temperature_config",
                message="This is help for temperature automations view.",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            area_name = area.get("name", "Unknown")
            climate_auto_id = self._prefixed_id("auto", area_name, "climate")
            climate_switch_id = f"{climate_auto_id}_switch"
            heat_setpoint_id = self._prefixed_id("auto", area_name, "heat_setpoint")
            cool_setpoint_id = self._prefixed_id("auto", area_name, "cool_setpoint")

            has_climate_automation = any(
                auto.get("id") == climate_auto_id
                for auto in area.get("automations", [])
            )
            if not has_climate_automation:
                continue

            section = {"type": "grid", "cards": []}
            section["cards"].append({
                "type": "heading",
                "heading": area_name,
                "icon": self._get_area_icon(area_name),
                "heading_style": "title",
                "badges": [
                    {
                        "type": "entity",
                        "show_state": True,
                        "show_icon": True,
                        "entity": f"switch.{climate_switch_id}",
                        "tap_action": {"action": "toggle"},
                        "color": "accent",
                    }
                ],
            })
            section["cards"].append({
                "type": "entities",
                "entities": [
                    {"entity": f"{InputType.INPUT_NUMBER}.{heat_setpoint_id}", "name": "Heat Setpoint"},
                    {"entity": f"{InputType.INPUT_NUMBER}.{cool_setpoint_id}", "name": "Cool Setpoint"},
                ],
            })

            view["sections"].append(section)

        self._apply_view_header(view)
        return view

    def create_battery_config_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the battery status view."""
        view = {
            "type": "sections",
            "title": "Battery",
            "path": "battery",
            "icon": "mdi:battery",
            "badges": [],
            "sections": [],
            "subview": True,
        }
        view["sections"].append(
            self._build_help_section(
                view_path="battery",
                message="This is help for battery view.",
                column_span=4,
            )
        )

        for area in registry_data.get("areas", []):
            area_name = area.get("name", "Unknown")
            section = {"type": "grid", "cards": []}

            # Header del área
            section["cards"].append({
                "type": "heading",
                "heading": area_name,
                "icon": self._get_area_icon(area_name),
                "heading_style": "title"
            })

            # Un tile por cada entidad battery:
            added = 0
            for ent in area.get("entities", []):
                card = self.build_battery_tile(ent)
                if card:
                    section["cards"].append(card)
                    added += 1

            if added > 0:
                view["sections"].append(section)

        self._apply_view_header(view)
        return view

    def create_users_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the users configuration view."""
        view = {
            "type": "sections",
            "max_columns": 4,
            "title": "Users",
            "path": "users",
            "icon": "mdi:account-group",
            "sections": [],
        }
        view["sections"].append(
            self._build_help_section(
                view_path="users",
                message="This is help for users config view.",
                column_span=4,
            )
        )
        view["sections"].append({
            "type": "grid",
            "cards": [
                {
                    "type": "tile",
                    "entity": "input_button.perm_manage_users",
                    "name": "Manage users",
                    "icon": "mdi:account-plus",
                    "hide_state": True,
                    "vertical": False,
                    "grid_options": {"columns": 6, "rows": 1},
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": "/config/person",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                }
            ],
            "column_span": 2,
        })

        user_names = self._resolve_user_names(registry_data)

        def _build_users_section(area_name: str) -> Dict[str, Any]:
            section = {"type": "grid", "cards": []}
            section["cards"].append({
                "type": "heading",
                "heading": area_name,
                "heading_style": "title",
            })
            entities = []
            all_users_id = self._prefixed_id("perm", area_name, "all_users")
            all_users_entity = f"{InputType.INPUT_BOOLEAN}.{all_users_id}"
            if user_names:
                all_users_entity = f"group.{all_users_id}"
            entities.append({
                "entity": all_users_entity,
                "name": "All users",
                "icon": "mdi:account-group",
            })
            for user_name in user_names:
                user_id = self._prefixed_id("perm", area_name, user_name)
                entities.append({
                    "entity": f"{InputType.INPUT_BOOLEAN}.{user_id}",
                    "name": user_name,
                    "icon": "mdi:account",
                })
            section["cards"].append({
                "type": "entities",
                "entities": entities,
                "show_header_toggle": False,
            })
            return section

        for area in registry_data.get("areas", []):
            area_name = area.get("name") or area.get("area_id") or "Unknown"
            if self._is_users_area_excluded(area_name):
                continue
            view["sections"].append(_build_users_section(area_name))

        view["subview"] = True
        self._apply_view_header(view)
        return view

    def create_automations_view(self) -> Dict[str, Any]:
        """Build the automations hub view."""
        route = os.getenv("HA_CONFIG_DASHBOARD_ROUTE", "config-dashboard").strip().strip("/")
        view = {
            "type": "sections",
            "max_columns": 4,
            "title": "Automations",
            "path": "automations",
            "icon": "mdi:refresh-auto",
            "sections": [],
            "subview": True,
        }
        view["sections"].append(
            self._build_help_section(
                view_path="automations",
                message="This is help for automations view.",
                column_span=4,
            )
        )
        view["sections"].append({
            "type": "grid",
            "cards": [
                {
                    "type": "tile",
                    "grid_options": {"columns": 12, "rows": 1},
                    "entity": "input_button.ui_lights_automations",
                    "icon": "mdi:lightbulb-group",
                    "hide_state": True,
                    "vertical": False,
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": f"/{route}/light_config",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                },
                {
                    "type": "tile",
                    "grid_options": {"columns": 12, "rows": 1},
                    "entity": "input_button.ui_button_automations",
                    "hide_state": True,
                    "vertical": False,
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": f"/{route}/buttons",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                },
                {
                    "type": "tile",
                    "grid_options": {"columns": 12, "rows": 1},
                    "entity": "input_button.ui_blinds_automations",
                    "icon": "mdi:blinds",
                    "show_entity_picture": False,
                    "hide_state": True,
                    "vertical": False,
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": f"/{route}/blinds_config",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                },
                {
                    "type": "tile",
                    "grid_options": {"columns": 12, "rows": 1},
                    "entity": "input_button.ui_temperature_automations",
                    "icon": "mdi:thermometer",
                    "hide_state": True,
                    "vertical": False,
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": f"/{route}/temperature_config",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                },
                {
                    "type": "tile",
                    "grid_options": {"columns": 12, "rows": 1},
                    "entity": "input_button.ui_notify_automations",
                    "icon": "mdi:bell-alert",
                    "hide_state": True,
                    "vertical": False,
                    "tap_action": {
                        "action": "navigate",
                        "navigation_path": f"/{route}/notify_automation",
                    },
                    "icon_tap_action": {"action": "none"},
                    "features_position": "bottom",
                },
            ],
        })
        self._apply_view_header(view)
        return view

    def create_notify_automation_view(self, registry_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build the notify automation configuration view."""
        view = {
            "type": "sections",
            "title": "Notify Automation",
            "path": "notify_automation",
            "icon": "mdi:bell-alert",
            "sections": [],
            "subview": True,
        }
        view["sections"].append(
            self._build_help_section(
                view_path="notify_automation",
                message="This is help for notify automation view.",
                column_span=4,
            )
        )

        notify_sections = [
            {
                "automation_id": "auto_notify_low_battery",
                "title": "low battery notification",
                "toggle_id": "auto_notify_low_battery_toggle",
            },
            {
                "automation_id": "auto_notify_unavailable_devices",
                "title": "unavailable device notification",
                "toggle_id": "auto_notify_unavailable_devices_toggle",
            },
            {
                "automation_id": "auto_notify_away_entities",
                "title": "left-on device notification",
                "toggle_id": "auto_notify_away_toggle",
            },
            {
                "automation_id": "auto_notify_open_door_window",
                "title": "open-door-window notification",
                "toggle_id": "auto_notify_open_door_window_toggle",
            },
        ]

        general_area = next((a for a in registry_data.get("areas", []) if a.get("area_id") == "general"), None)
        available_automations = {
            a.get("id") for a in (general_area.get("automations", []) or []) if isinstance(a, dict)
        } if general_area else set()

        for entry in notify_sections:
            if entry["automation_id"] not in available_automations:
                continue
            section = {"type": "grid", "cards": []}
            section["cards"].append({
                "type": "heading",
                "heading": entry["title"],
                "icon": "mdi:bell-alert",
                "heading_style": "title",
                "badges": [
                    {
                        "show_state": True,
                        "show_icon": True,
                        "type": "entity",
                        "entity": f"{InputType.INPUT_BOOLEAN}.{entry['toggle_id']}",
                        "tap_action": {"action": "toggle"},
                        "color": "accent",
                    },
                ],
            })
            view["sections"].append(section)

        view["max_columns"] = 4
        view.setdefault("cards", [])
        self._apply_view_header(view)
        return view
        # === HELPERS PÚBLICOS PARA USOS ATÓMICOS ===
    def build_device_card_by_entity_id(self, registry_data: Dict[str, Any], entity_id: str) -> Optional[Dict[str, Any]]:
        """Build a device button card by entity id."""
        for area in registry_data.get("areas", []):
            for ent in area.get("entities", []):
                if ent.get("entity_id") == entity_id:
                  if ent.get("types") == EntityType.MAIN:
                    return self.build_device_main_button_card(ent)
        return None

    def build_battery_tile_by_entity_id(self, registry_data: Dict[str, Any], entity_id: str) -> Optional[Dict[str, Any]]:
        """Build a battery tile by entity id."""
        for area in registry_data.get("areas", []):
            for ent in area.get("entities", []):
                if ent.get("entity_id") == entity_id and self._is_type(ent, EntityType.BATTERY):
                    return self.build_battery_tile(ent)
        return None

    def build_light_config_section_for_area(self, registry_data: Dict[str, Any], area_name: str) -> Optional[Dict[str, Any]]:
        """Build a light config section for a specific area."""
        area = next((a for a in registry_data.get("areas", []) if a.get("name") == area_name), None)
        if not area:
            return None
        return self.build_light_config_cards_for_area(area)

    def build_blinds_config_section_for_area(self, registry_data: Dict[str, Any], area_name: str) -> Optional[Dict[str, Any]]:
        """Build a blinds config section for a specific area."""
        area = next((a for a in registry_data.get("areas", []) if a.get("name") == area_name), None)
        if not area:
            return None
        return self.build_blinds_config_cards_for_area(area)


