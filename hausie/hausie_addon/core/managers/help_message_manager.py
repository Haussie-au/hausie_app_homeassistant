from __future__ import annotations

import json
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from ..flow_logger import get_logger
from ..utils.naming import slugify


DEFAULT_MESSAGES: dict[str, list[dict[str, Any]]] = {
    "main": [{"text": "This is help for main view.", "weight": 1}],
    "devices": [{"text": "This is help for device view", "weight": 1}],
    "buttons": [{"text": "This is help for buttons view.", "weight": 1}],
    "battery": [{"text": "This is help for battery view.", "weight": 1}],
    "light_config": [{"text": "This is help for light automation view.", "weight": 1}],
    "blinds_config": [{"text": "This is help for blinds configuration view.", "weight": 1}],
    "temperature_config": [{"text": "This is help for temperature automations view.", "weight": 1}],
    "notify_automation": [{"text": "This is help for notify automation view.", "weight": 1}],
    "new-devices": [
        {
            "text": (
                "Remember to add common and short names to the new devices, "
                "this will help you use them easily. please select one of the following "
                "labels that apply to the kind of device you are adding"
            ),
            "weight": 1,
        }
    ],
    "users": [{"text": "This is help for users config view.", "weight": 1}],
    "automations": [{"text": "This is help for automations view.", "weight": 1}],
    "hausie": [{"text": "This is help for the Hausie dashboard.", "weight": 1}],
}


@dataclass
class HelpMessageManager:
    path: Path

    def __init__(self, path: Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[3] / "hausie" / "homeassistant" / "data" / "help_messages.json"
        env_override = Path(str(path)) if path else None
        env_path_raw = os.getenv("HAUSIE_HELP_MESSAGES_PATH", "").strip() if not env_override else ""
        env_path = Path(env_path_raw) if env_path_raw else None
        self.path = env_override or env_path or default_path
        self._log = get_logger("ui")

    @staticmethod
    def entity_id_for_view(view_key: str) -> str:
        slug = slugify(view_key)
        return f"input_text.ui_help_message_{slug}"

    def load(self) -> dict[str, Any]:
        created = False
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        else:
            data = {"version": 1, "views": {}}
            created = True
        if not isinstance(data, dict):
            data = {"version": 1, "views": {}}
        data.setdefault("version", 1)
        views = data.get("views")
        if not isinstance(views, dict):
            views = {}
        for view_key, messages in DEFAULT_MESSAGES.items():
            if view_key not in views:
                views[view_key] = {"messages": messages, "cursor": 0}
        for view_key, view_data in list(views.items()):
            if not isinstance(view_data, dict):
                views[view_key] = {"messages": self._normalize_messages(view_data), "cursor": 0}
                continue
            view_data.setdefault("cursor", 0)
            view_data["messages"] = self._normalize_messages(view_data.get("messages"))
        data["views"] = views
        if created:
            self.save(data)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def update_views(self, view_messages: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
        data = self.load()
        if replace:
            data["views"] = {}
        views = data.setdefault("views", {})
        for view_key, messages in (view_messages or {}).items():
            normalized = self._normalize_messages(messages)
            views[view_key] = {"messages": normalized, "cursor": 0}
        self.save(data)
        return data

    def rotate(self, view_keys: list[str] | None = None) -> dict[str, str]:
        data = self.load()
        views = data.get("views", {})
        selected: dict[str, str] = {}
        keys = view_keys or list(views.keys())
        for view_key in keys:
            view_data = views.get(view_key)
            if not isinstance(view_data, dict):
                continue
            message = self._pick_next_message(view_data)
            if message:
                selected[view_key] = message
        self.save(data)
        return selected

    def get_pool(self) -> dict[str, Any]:
        return self.load()

    def _pick_next_message(self, view_data: dict[str, Any]) -> str:
        expanded = self._expand_messages(view_data.get("messages"))
        if not expanded:
            return ""
        cursor = int(view_data.get("cursor") or 0)
        if cursor >= len(expanded) or cursor < 0:
            cursor = 0
        message = expanded[cursor]
        view_data["cursor"] = (cursor + 1) % len(expanded)
        return message

    def _expand_messages(self, messages: Any) -> list[str]:
        expanded: list[str] = []
        for entry in self._normalize_messages(messages):
            text = entry.get("text", "")
            if not text:
                continue
            weight = entry.get("weight") or 1
            try:
                weight_int = int(weight)
            except (TypeError, ValueError):
                weight_int = 1
            weight_int = max(1, min(10, weight_int))
            expanded.extend([text] * weight_int)
        return expanded

    def _normalize_messages(self, messages: Any) -> list[dict[str, Any]]:
        if messages is None:
            return []
        if isinstance(messages, dict) and "text" in messages:
            messages = [messages]
        if isinstance(messages, str):
            messages = [messages]
        if not isinstance(messages, list):
            return []
        normalized: list[dict[str, Any]] = []
        for entry in messages:
            if isinstance(entry, str):
                text = entry.strip()
                if text:
                    normalized.append({"text": text, "weight": 1})
                continue
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                if not text:
                    continue
                weight = entry.get("weight", 1)
                normalized.append({"text": text, "weight": weight})
        return normalized
