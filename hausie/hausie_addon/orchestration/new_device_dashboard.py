from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import yaml

PKG_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = PKG_DIR.parent


def _resolve_config_dashboard_path() -> Path:
    candidates = [
        ROOT_DIR / "hausie" / "homeassistant" / "dashboards" / "hausie_configuration_dashboard.yaml",
        ROOT_DIR / "homeassistant" / "dashboards" / "hausie_configuration_dashboard.yaml",
        Path("/config/dashboards/hausie_configuration_dashboard.yaml"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_config_dashboard_path() -> Path:
    """Return the local path for the config dashboard YAML."""
    return _resolve_config_dashboard_path()


def _load_dashboard_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Dashboard YAML not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return doc if isinstance(doc, dict) else {}


def _write_dashboard_yaml(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _ensure_new_devices_view(dashboard: dict) -> dict:
    views = dashboard.setdefault("views", [])
    for view in views:
        if not isinstance(view, dict):
            continue
        if view.get("path") == "new-devices" or view.get("title") == "New Devices":
            return view
    view = {
        "type": "sections",
        "max_columns": 4,
        "title": "New Devices",
        "path": "new-devices",
        "icon": "mdi:new-box",
        "sections": [],
    }
    views.append(view)
    return view


def _ensure_new_devices_section(view: dict) -> dict:
    sections = view.setdefault("sections", [])
    for section in sections:
        if not isinstance(section, dict):
            continue
        cards = section.get("cards") or []
        for card in cards:
            if isinstance(card, dict) and card.get("type") == "heading" and card.get("heading") == "New devices":
                return section
    section = {
        "type": "grid",
        "cards": [{"type": "heading", "heading": "New devices", "heading_style": "title"}],
    }
    sections.append(section)
    return section


def build_new_device_popup_card(device_id: str, entity_id: str, device_name: str | None) -> dict:
    if not entity_id:
        raise ValueError("entity_id is required for the tile card.")
    name = device_name or device_id or "New device"
    popup_content = {
        "type": "vertical-stack",
        "cards": [
            {
                "type": "markdown",
                "content": (
                    "Pon un nombre descriptivo al dispositiv y el area en el que esta\n"
                    "ubicado.\n\n"
                    "Lista de labels <br><br>"
                ),
                "text_only": True,
            },
            {
                "type": "entities",
                "entities": [
                    {
                        "entity": "input_text.new_device_name",
                        "name": "Name",
                        "secondary_info": "none",
                    },
                    {
                        "entity": "input_select.new_device_label",
                        "name": "Label",
                    },
                    {
                        "entity": "input_select.new_device_area",
                        "name": "Area",
                    },
                ],
            },
            {
                "type": "markdown",
                "content": "<br><br>",
                "text_only": True,
            },
        ],
    }

    sequence = [
        {
            "service": "input_text.set_value",
            "data": {"entity_id": "input_text.new_device_device_id", "value": device_id},
        },
        {
            "service": "input_text.set_value",
            "data": {"entity_id": "input_text.new_device_name", "value": name},
        },
        {
            "service": "browser_mod.popup",
            "data": {
                "title": name,
                "content": popup_content,
                "left_button": "cancel",
                "left_button_action": {
                    "action": "call-service",
                    "service": "browser_mod.close_popup",
                },
                "right_button": "save",
                "right_button_action": {
                    "service": "browser_mod.sequence",
                    "data": {
                        "sequence": [
                            {
                                "service": "input_button.press",
                                "data": {"entity_id": "input_button.new_device_save"},
                            },
                            {"service": "browser_mod.close_popup"},
                        ]
                    },
                },
            },
        },
    ]

    return {
        "type": "tile",
        "entity": entity_id,
        "icon": "mdi:new-box",
        "color": "primary",
        "show_entity_picture": False,
        "hide_state": True,
        "vertical": False,
        "features_position": "bottom",
        "tap_action": {
            "action": "fire-dom-event",
            "browser_mod": {
                "service": "browser_mod.sequence",
                "data": {"sequence": sequence},
            },
        },
        "icon_tap_action": {"action": "none"},
        "hausie_device_id": device_id,
    }


def upsert_new_device_button(
    device_id: str,
    entity_id: str,
    device_name: str | None = None,
    *,
    dashboard_path: Path | None = None,
) -> bool:
    if not device_id:
        raise ValueError("device_id is required.")
    if not entity_id:
        raise ValueError("entity_id is required.")
    path = dashboard_path or _resolve_config_dashboard_path()
    dashboard = _load_dashboard_yaml(path)
    view = _ensure_new_devices_view(dashboard)
    section = _ensure_new_devices_section(view)
    cards = section.setdefault("cards", [])

    card = build_new_device_popup_card(device_id, entity_id, device_name)
    card_name = (card.get("name") or "").strip().lower()
    match_idx = None
    for idx, existing in enumerate(cards):
        if not isinstance(existing, dict):
            continue
        if existing.get("type") == "heading":
            continue
        if existing.get("hausie_device_id") == device_id:
            match_idx = idx
            break
        existing_name = (existing.get("name") or "").strip().lower()
        if card_name and existing_name == card_name:
            match_idx = idx
            break

    updated = False
    if match_idx is None:
        insert_at = None
        for idx, existing in enumerate(cards):
            if isinstance(existing, dict) and existing.get("type") == "heading" and existing.get("heading") == "New devices":
                insert_at = idx + 1
                break
        if insert_at is None:
            cards.append(card)
        else:
            cards.insert(insert_at, card)
        updated = True
    else:
        if cards[match_idx] != card:
            cards[match_idx] = card
            updated = True

    if updated:
        _write_dashboard_yaml(path, dashboard)
    return updated
