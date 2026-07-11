import json
import requests
import websocket
from pathlib import Path

from ...constants import InputType
from ..flow_logger import get_logger

log = get_logger("core")

class HAClient:
    def __init__(self, ha_url_ws: str, ha_url_rest: str, token: str, output_dir: Path = None):
        """Initialize Home Assistant client settings and output paths."""
        self.ha_url_ws = ha_url_ws
        self.ha_url_rest = ha_url_rest
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # Carpeta donde se guardan los datos RAW
        self.data_dir = output_dir or Path(__file__).resolve().parents[3] / "hausie" / "homeassistant" / "data"
        self.raw_file = self.data_dir / "raw.json"
        
    def _send_and_wait(self, ws, request_id: int, message_type: str):
        """Send a websocket request and wait for the matching response."""
        ws.send(json.dumps({"id": request_id, "type": message_type}))
        while True:
            response = ws.recv()
            response_msg = json.loads(response)
            if response_msg.get("id") == request_id:
                return response_msg.get("result")

    def _save_raw(self, data: dict) -> None:
        """Save JSON data under hausie/homeassistant/data/raw.json."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.raw_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.ok(f"Saved raw data to {self.raw_file}")

    def _load_raw(self) -> dict:
        """Load JSON data from hausie/homeassistant/data/raw.json."""
        if not self.raw_file.exists():
            raise FileNotFoundError(f"Raw data not found: {self.raw_file}")
        with open(self.raw_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Raw data must be a JSON object.")
        return data

    @staticmethod
    def _upsert_by_key(items: list, key: str, new_item: dict) -> None:
        """Insert or update a dict in a list by key."""
        if not isinstance(items, list):
            return
        new_val = new_item.get(key)
        if new_val is None:
            return
        for idx, item in enumerate(items):
            if isinstance(item, dict) and item.get(key) == new_val:
                items[idx] = new_item
                return
        items.append(new_item)

    def _upsert_many_by_key(self, items: list, key: str, new_items: list) -> None:
        """Insert or update many dicts in a list by key."""
        if not isinstance(new_items, list):
            return
        for item in new_items:
            if isinstance(item, dict):
                self._upsert_by_key(items, key, item)

    @staticmethod
    def _normalize_users(users: list) -> list[dict]:
        """Normalize user records from Home Assistant."""
        normalized = []
        for user in users or []:
            if not isinstance(user, dict):
                continue
            user_id = user.get("id") or user.get("user_id")
            name = user.get("name")
            if not user_id or name is None:
                continue
            group_ids = user.get("group_ids") or []
            if not isinstance(group_ids, list):
                group_ids = []
            is_owner = user.get("is_owner")
            if is_owner is None:
                is_owner = user.get("isOwner")
            is_admin = user.get("is_admin")
            if is_admin is None:
                is_admin = user.get("isAdmin")
            if is_admin is None:
                is_admin = "system-admin" in group_ids
            username = user.get("username")
            for credential in user.get("credentials") or []:
                if not isinstance(credential, dict):
                    continue
                data = credential.get("data") if isinstance(credential.get("data"), dict) else {}
                username = username or data.get("username")
            normalized.append({
                "id": user_id,
                "name": name,
                "username": username,
                "isOwner": bool(is_owner),
                "isAdmin": bool(is_admin),
            })
        return normalized

    @staticmethod
    def _normalize_labels(labels: list) -> list[dict]:
        """Normalize label records from Home Assistant."""
        normalized = []
        for label in labels or []:
            if not isinstance(label, dict):
                continue
            label_id = label.get("label_id") or label.get("id")
            name = label.get("name") or label.get("label")
            if not label_id or name is None:
                continue
            entry = {"id": label_id, "name": name}
            if "icon" in label:
                entry["icon"] = label.get("icon")
            if "color" in label:
                entry["color"] = label.get("color")
            normalized.append(entry)
        return normalized

    def _get_services_via_rest(self):
        """Fetch services from the Home Assistant REST API."""
        url = f"{self.ha_url_rest}/services"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            return response.json()
        else:
            log.warn(f"Failed to get services. Status code: {response.status_code}")
            return None

    def get_services(self) -> list[dict]:
        """Return the list of services from the Home Assistant REST API."""
        services = self._get_services_via_rest()
        return services if isinstance(services, list) else []

    def get_states(self) -> list[dict]:
        """Fetch entity states from the Home Assistant REST API."""
        url = f"{self.ha_url_rest}/states"
        response = requests.get(url, headers=self.headers, timeout=15)
        if response.status_code != 200:
            raise RuntimeError(f"Failed to get states. Status code: {response.status_code}")
        data = response.json()
        return data if isinstance(data, list) else []

    def get_config(self) -> dict:
        """Fetch core config from the Home Assistant REST API."""
        url = f"{self.ha_url_rest}/config"
        response = requests.get(url, headers=self.headers, timeout=15)
        if response.status_code != 200:
            raise RuntimeError(f"Failed to get config. Status code: {response.status_code}")
        data = response.json()
        return data if isinstance(data, dict) else {}

    def call_service(self, domain: str, service: str, data: dict | None = None) -> dict:
        """Call a Home Assistant service via REST."""
        if not domain or not service:
            raise ValueError("domain and service are required.")
        url = f"{self.ha_url_rest}/services/{domain}/{service}"
        payload = data or {}
        resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Service call failed {resp.status_code}: {resp.text}")
        return resp.json()

    
    def fetch_all(self, *, include_users: bool = True):
        """Authenticate over websocket and fetch areas, devices, entities, and services."""
        ws = websocket.create_connection(self.ha_url_ws)

        # WebSocket auth
        msg = json.loads(ws.recv())
        if msg.get("type") != "auth_required":
            raise Exception(f"Unexpected message: {msg}")

        ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(ws.recv())
        if msg.get("type") != "auth_ok":
            raise Exception(f"Authentication failed: {msg}")

        log.ok("Authenticated via WebSocket.")

        areas = self._send_and_wait(ws, 1, "config/area_registry/list")
        devices = self._send_and_wait(ws, 2, "config/device_registry/list")
        entities = self._send_and_wait(ws, 3, "config/entity_registry/list")
        users = self._send_and_wait(ws, 4, "config/auth/list") if include_users else None
        services = self._get_services_via_rest() or []

        ws.close()
        log.info("WebSocket closed.")

        raw_snapshot = {
            "areas": areas or [],
            "devices": devices or [],
            "entities": entities or [],
            "services": services,
        }
        if include_users:
            raw_snapshot["users"] = self._normalize_users(users or [])
        else:
            try:
                raw_snapshot["users"] = self._load_raw().get("users", [])
            except FileNotFoundError:
                raw_snapshot["users"] = []
        self._save_raw(raw_snapshot)

    
    def fetch_device_and_entities_by_id(self, device_id: str) -> dict:
        """Fetch a device and its entities by id and update raw caches."""
        ws = websocket.create_connection(self.ha_url_ws)
        try:
            # WebSocket auth
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_required":
                raise Exception(f"Unexpected message: {msg}")

            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_ok":
                raise Exception(f"Authentication failed: {msg}")

            # Listado completo y filtrado local
            devices = self._send_and_wait(ws, 101, "config/device_registry/list") or []
            entities = self._send_and_wait(ws, 102, "config/entity_registry/list") or []

            device = next((d for d in devices if d.get("id") == device_id), None)
            device_entities = [e for e in entities if e.get("device_id") == device_id]

        finally:
            ws.close()

        # ---- Persistencia RAW (merge/upsert en agregados) ----
        try:
            raw = self._load_raw()
        except FileNotFoundError:
            raw = {"areas": [], "devices": [], "entities": [], "services": []}
        raw.setdefault("areas", [])
        raw.setdefault("devices", [])
        raw.setdefault("entities", [])
        raw.setdefault("services", [])

        if device is not None:
            self._upsert_by_key(raw["devices"], key="id", new_item=device)
        if device_entities:
            self._upsert_many_by_key(raw["entities"], key="entity_id", new_items=device_entities)

        self._save_raw(raw)

        return {"device": device, "entities": device_entities}

    def fetch_users(self) -> list[dict]:
        """Fetch users from Home Assistant via WebSocket."""
        ws = websocket.create_connection(self.ha_url_ws)
        try:
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_required":
                raise Exception(f"Unexpected message: {msg}")

            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_ok":
                raise Exception(f"Authentication failed: {msg}")

            users = self._send_and_wait(ws, 1, "config/auth/list") or []
        finally:
            ws.close()
        return self._normalize_users(users)

    def _auth_ws_call(self, message_type: str, payload: dict | None = None):
        """Call a Home Assistant auth websocket command."""
        ws = websocket.create_connection(self.ha_url_ws)
        try:
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_required":
                raise Exception(f"Unexpected message: {msg}")

            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_ok":
                raise Exception(f"Authentication failed: {msg}")

            request = {"id": 1, "type": message_type}
            if payload:
                request.update(payload)
            ws.send(json.dumps(request))
            while True:
                response = json.loads(ws.recv())
                if response.get("id") != 1:
                    continue
                if not response.get("success", False):
                    raise RuntimeError(f"HA auth command failed: {response}")
                return response.get("result")
        finally:
            ws.close()

    def create_auth_user(
        self,
        *,
        name: str,
        username: str,
        password: str,
        is_admin: bool = True,
        local_only: bool = False,
    ) -> dict:
        """Create a temporary Home Assistant login user."""
        group_ids = ["system-admin"] if is_admin else ["system-users"]
        user = self._auth_ws_call(
            "config/auth/create",
            {
                "name": name,
                "group_ids": group_ids,
                "local_only": local_only,
            },
        )
        if isinstance(user, dict) and isinstance(user.get("user"), dict):
            user = user["user"]
        if not isinstance(user, dict) or not user.get("id"):
            raise RuntimeError(f"Home Assistant did not return a user id: {user}")
        credential = self._auth_ws_call(
            "config/auth_provider/homeassistant/create",
            {
                "user_id": user["id"],
                "username": username,
                "password": password,
            },
        )
        return {
            "user": user,
            "credential": credential if isinstance(credential, dict) else {},
        }

    def delete_auth_user_by_username(self, username: str) -> bool:
        """Delete a Home Assistant auth user by username."""
        target = username.strip().lower()
        if not target:
            return False
        users = self.fetch_users()
        for user in users:
            username = str(user.get("username") or user.get("name") or "").strip().lower()
            if username != target:
                continue
            user_id = user.get("id")
            if not user_id:
                continue
            self._auth_ws_call("config/auth/delete", {"user_id": user_id})
            return True
        return False

    def fetch_labels(self) -> list[dict]:
        """Fetch labels from Home Assistant via WebSocket."""
        ws = websocket.create_connection(self.ha_url_ws)
        try:
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_required":
                raise Exception(f"Unexpected message: {msg}")

            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(ws.recv())
            if msg.get("type") != "auth_ok":
                raise Exception(f"Authentication failed: {msg}")

            labels = self._send_and_wait(ws, 1, "config/label_registry/list") or []
        finally:
            ws.close()
        return self._normalize_labels(labels)

    def create_label(
        self,
        *,
        name: str,
        icon: str | None = None,
        color: str | None = None,
    ) -> dict:
        """Create a Home Assistant label."""
        payload = {"name": str(name or "").strip()}
        if not payload["name"]:
            raise ValueError("Label name is required.")
        if icon:
            payload["icon"] = str(icon).strip()
        if color:
            payload["color"] = str(color).strip()
        result = self._auth_ws_call("config/label_registry/create", payload)
        normalized = self._normalize_labels([result] if isinstance(result, dict) else [])
        return normalized[0] if normalized else {}

    def update_label(
        self,
        *,
        label_id: str,
        name: str,
        icon: str | None = None,
        color: str | None = None,
    ) -> dict:
        """Update a Home Assistant label."""
        payload = {
            "label_id": str(label_id or "").strip(),
            "name": str(name or "").strip(),
        }
        if not payload["label_id"]:
            raise ValueError("label_id is required.")
        if not payload["name"]:
            raise ValueError("Label name is required.")
        if icon:
            payload["icon"] = str(icon).strip()
        if color:
            payload["color"] = str(color).strip()
        result = self._auth_ws_call("config/label_registry/update", payload)
        normalized = self._normalize_labels([result] if isinstance(result, dict) else [])
        return normalized[0] if normalized else {}

    def list_exposed_entities(self) -> dict[str, dict[str, bool]]:
        """Return explicit voice-assistant exposure preferences per entity."""
        result = self._auth_ws_call("homeassistant/expose_entity/list") or {}
        exposed = result.get("exposed_entities") if isinstance(result, dict) else {}
        return exposed if isinstance(exposed, dict) else {}

    def set_entity_exposure(
        self,
        *,
        assistants: list[str],
        entity_ids: list[str],
        should_expose: bool,
    ) -> dict:
        """Expose or unexpose entities for the given assistants."""
        normalized_assistants = [str(item or "").strip() for item in assistants if str(item or "").strip()]
        normalized_entity_ids = [str(item or "").strip() for item in entity_ids if str(item or "").strip()]
        if not normalized_assistants:
            raise ValueError("assistants is required.")
        if not normalized_entity_ids:
            return {}
        result = self._auth_ws_call(
            "homeassistant/expose_entity",
            {
                "assistants": normalized_assistants,
                "entity_ids": normalized_entity_ids,
                "should_expose": bool(should_expose),
            },
        )
        return result if isinstance(result, dict) else {}

    def upsert_raw_users(self, users: list[dict]) -> None:
        """Update raw snapshot with the latest users list."""
        try:
            raw = self._load_raw()
        except FileNotFoundError:
            raw = {"areas": [], "devices": [], "entities": [], "services": []}
        raw["users"] = list(users or [])
        self._save_raw(raw)

    def set_input_boolean(self, entity_id: str, state) -> dict:
      """Call the input_boolean service to set a helper state."""
      if isinstance(state, str):
          st = state.strip().lower()
          if st in ("on", "true", "1"):
              service = "turn_on"
          elif st in ("off", "false", "0"):
              service = "turn_off"
          elif st == "toggle":
              service = "toggle"
          else:
              raise ValueError(f"Invalid state string: {state!r}. Use 'on', 'off' o 'toggle'.")
      else:
          service = "turn_on" if bool(state) else "turn_off"

      url = f"{self.ha_url_rest}/services/{InputType.INPUT_BOOLEAN}/{service}"
      payload = {"entity_id": entity_id}
      resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
      if resp.status_code // 100 != 2:
          raise RuntimeError(f"Service call failed {resp.status_code}: {resp.text}")
      return resp.json()
