from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_OPTIONS_PATH = Path("/data/options.json")
DEFAULT_CACHE_PATH = Path("/data/subscription_cache.json")

DEFAULT_FEATURES = {
    "remote_support": False,
    "auto_lights": False,
    "auto_blinds": False,
    "auto_climate": False,
    "ui_advanced_config": False,
}


def _default_subscription(*, status: str = "inactive") -> dict[str, Any]:
    return {
        "plan": "plan 1",
        "status": status,
        "features": dict(DEFAULT_FEATURES),
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        return


def load_options(path: Path = DEFAULT_OPTIONS_PATH) -> dict[str, Any]:
    return _read_json(path) or {}


def _normalize_features(raw_features: Any) -> dict[str, bool]:
    features = dict(DEFAULT_FEATURES)
    if isinstance(raw_features, dict):
        for key, value in raw_features.items():
            if not key:
                continue
            features[str(key)] = bool(value)
    return features


def _normalize_subscription(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_subscription(status="unknown")
    plan = str(raw.get("plan") or "plan 1")
    status = str(raw.get("status") or "unknown")
    features = _normalize_features(raw.get("features"))
    return {"plan": plan, "status": status, "features": features}


def _request_subscription(api_base_url: str, token: str, timeout_s: int) -> dict[str, Any]:
    import requests

    url = f"{api_base_url.rstrip('/')}/api/billing/me"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Subscription response must be a JSON object.")
    return data


def fetch_subscription_status(
    *,
    options_path: Path = DEFAULT_OPTIONS_PATH,
    cache_path: Path = DEFAULT_CACHE_PATH,
    api_base_url: str | None = None,
    user_token: str | None = None,
    timeout_s: int = 5,
) -> dict[str, Any]:
    opts = load_options(options_path)
    api_base = (api_base_url or opts.get("api_base_url") or "").strip()
    token = (user_token or opts.get("user_token") or "").strip()

    if not token:
        return _default_subscription(status="inactive")

    try:
        if not api_base:
            raise ValueError("api_base_url is required to fetch subscription.")
        data = _request_subscription(api_base, token, timeout_s)
        _write_json(cache_path, data)
        return _normalize_subscription(data)
    except Exception:
        cached = _read_json(cache_path)
        if cached:
            return _normalize_subscription(cached)
        return _default_subscription(status="unknown")


def is_feature_active(subscription: dict[str, Any], feature_key: str) -> bool:
    status = str(subscription.get("status") or "").strip().lower()
    if status != "active":
        return False
    features = subscription.get("features") or {}
    if not isinstance(features, dict):
        return False
    return bool(features.get(feature_key))


def update_ha_subscription_entities(
    subscription: dict[str, Any],
    *,
    ha_client: Any | None = None,
    ha_url: str | None = None,
    ha_token: str | None = None,
    plan_entity: str = "input_text.hausie_subscription_plan",
    status_entity: str = "input_text.hausie_subscription_status",
    remote_support_entity: str = "input_boolean.allow_remote_support",
) -> None:
    plan = str(subscription.get("plan") or "free")
    status = str(subscription.get("status") or "unknown")
    features = subscription.get("features") or {}
    allow_remote = bool(features.get("remote_support") and status == "active")

    def call_service(domain: str, service: str, data: dict) -> None:
        if ha_client is not None:
            ha_client.call_service(domain, service, data)
            return
        if not ha_url or not ha_token:
            raise ValueError("ha_url and ha_token are required when ha_client is not provided.")
        import requests

        url = f"{ha_url.rstrip('/')}/api/services/{domain}/{service}"
        headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json=data, timeout=10)

    call_service("input_text", "set_value", {"entity_id": plan_entity, "value": plan})
    call_service("input_text", "set_value", {"entity_id": status_entity, "value": status})
    call_service(
        "input_boolean",
        "turn_on" if allow_remote else "turn_off",
        {"entity_id": remote_support_entity},
    )
