# watcher_devices.py
import json, time, random, traceback, requests
import websocket  # pip install websocket-client

from ..core.flow_logger import get_logger

PING_EVERY = 25
CONNECT_TIMEOUT = 10
RECV_TIMEOUT = 1
MAX_BACKOFF = 60
MIN_INTERVAL_BETWEEN_UPDATES = 5


class DeviceCreateWatcher:
    def __init__(
        self,
        ha_url_ws: str,
        token: str,
        on_device_create,
        area_filter: set[str] | None = None,
        ha_url_rest: str | None = None,
    ):
        """Initialize the websocket watcher for device creation events."""
        self.ha_url_ws = ha_url_ws
        self.ha_url_rest = ha_url_rest
        self.token = token
        self.on_device_create = on_device_create
        self.area_filter = area_filter
        self._log = get_logger("new_device")

        self._last_ping_at = 0.0
        self._last_update_at = 0.0

    def _now(self):
        """Return the current time as a float timestamp."""
        return time.time()

    def _auth_and_subscribe(self, ws):
        """Authenticate to HA websocket and subscribe to device events."""
        self._log.start("Waiting for handshake.")
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake: {hello}")
        self._log.ok("Handshake received.")

        self._log.start("Authenticating.")
        ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_ok = json.loads(ws.recv())
        if auth_ok.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")
        self._log.ok("WebSocket authenticated.")

        sub_id = 7001
        self._log.start("Subscribing to device_registry_updated.")
        ws.send(json.dumps({
            "id": sub_id,
            "type": "subscribe_events",
            "event_type": "device_registry_updated"
        }))
        resp = json.loads(ws.recv())
        if not (resp.get("id") == sub_id and resp.get("type") == "result" and resp.get("success")):
            raise RuntimeError(f"Subscribe failed: {resp}")
        self._log.ok("Subscription successful.")
        return sub_id

    def _fetch_device_by_id(self, device_id: str) -> dict:
        """Fetch a device and its entities by id from HA."""
        base_url = (self.ha_url_rest or "").strip()
        if base_url:
            api_base = base_url.rstrip("/")
            if not api_base.endswith("/api"):
                api_base = f"{api_base}/api"
        else:
            url = self.ha_url_ws
            if "/api/websocket" in url:
                url = url.split("/api/websocket", 1)[0]
            url = url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
            api_base = f"{url}/api"

        full_url = f"{api_base}/device_registry/{device_id}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        self._log.start(f"Fetching device details {device_id}.")
        resp = requests.get(full_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            self._log.ok(f"Device {device_id} fetched.")
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()

        list_url = f"{api_base}/config/device_registry/list"
        try:
            list_resp = requests.get(list_url, headers=headers, timeout=15)
            if list_resp.status_code == 200:
                devices = list_resp.json()
                if isinstance(devices, list):
                    device = next((d for d in devices if d.get("id") == device_id), None)
                    if device:
                        self._log.ok(f"Device {device_id} fetched via list.")
                        return device
            else:
                self._log.warn(f"device_registry list status {list_resp.status_code}.")
        except Exception as exc:
            self._log.warn(f"error fetching device_registry list: {exc}")

        self._log.warn(f"device {device_id} not found in REST; returning id.")
        return {"id": device_id}

    def run_forever(self):
        """Run the event loop and call the handler for new devices."""
        backoff = 1
        while True:
            ws = None
            try:
                self._log.start("Connecting to WebSocket.")
                ws = websocket.create_connection(self.ha_url_ws, timeout=CONNECT_TIMEOUT)
                ws.settimeout(RECV_TIMEOUT)

                sub_id = self._auth_and_subscribe(ws)
                self._last_ping_at = self._now()

                self._log.start("Waiting for events.")
                while True:
                    now = self._now()
                    if (now - self._last_ping_at) >= PING_EVERY:
                        ws.send(json.dumps({"type": "ping"}))
                        self._last_ping_at = now
                        self._log.info("Ping sent.")

                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except websocket.WebSocketConnectionClosedException as e:
                        raise RuntimeError(f"WS cerrado: {e}")

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        self._log.warn("error parsing JSON message.")
                        continue

                    if msg.get("type") == "event" and msg.get("id") == sub_id:
                        self._log.info("Event received.")
                        data = msg.get("event", {}).get("data", {})

                        if data.get("action") == "create" and "device_id" in data:
                            device_id = data["device_id"]
                            self._log.ok(f"New device created: {device_id}")

                            try:
                                device = self._fetch_device_by_id(device_id)
                            except Exception as e:
                                self._log.error(f"Error fetching device: {e}")
                                continue

                            if self.area_filter is not None:
                                if device.get("area_id") not in self.area_filter:
                                    self._log.warn("device ignored by area filter.")
                                    continue

                            if (self._now() - self._last_update_at) < MIN_INTERVAL_BETWEEN_UPDATES:
                                self._log.skip("Skipping due to rate limit.")
                                continue

                            try:
                                self._log.start("Running logic for new device.")
                                self.on_device_create(device)
                                self._last_update_at = self._now()
                            except Exception as e:
                                self._log.error(f"Error in on_device_create: {e}")
                                traceback.print_exc()

            except KeyboardInterrupt:
                self._log.warn("Interrupted by user.")
                break
            except Exception as e:
                self._log.error(f"Error: {e}")
                traceback.print_exc()
                sleep_s = min(backoff, MAX_BACKOFF)
                jitter = random.uniform(0, sleep_s * 0.3)
                wait = sleep_s + jitter
                self._log.warn(f"Retrying in {wait:.1f}s.")
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)
            finally:
                if ws:
                    try:
                        ws.close()
                        self._log.info("Connection closed.")
                    except:
                        pass

    def wait_for_new_device(self, timeout_s: int | None = None) -> dict | None:
        """Wait for a single new device event and return its payload."""
        deadline = self._now() + timeout_s if timeout_s else None
        backoff = 1

        while True:
            if deadline and self._now() >= deadline:
                return None

            ws = None
            try:
                self._log.start("Connecting to WebSocket.")
                ws = websocket.create_connection(self.ha_url_ws, timeout=CONNECT_TIMEOUT)
                ws.settimeout(RECV_TIMEOUT)

                sub_id = self._auth_and_subscribe(ws)
                self._last_ping_at = self._now()

                self._log.start("Waiting for new device.")
                while True:
                    if deadline and self._now() >= deadline:
                        return None

                    now = self._now()
                    if (now - self._last_ping_at) >= PING_EVERY:
                        ws.send(json.dumps({"type": "ping"}))
                        self._last_ping_at = now

                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except websocket.WebSocketConnectionClosedException as e:
                        raise RuntimeError(f"WS cerrado: {e}")

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        self._log.warn("error parsing JSON message.")
                        continue

                    if msg.get("type") == "event" and msg.get("id") == sub_id:
                        data = msg.get("event", {}).get("data", {})
                        if data.get("action") == "create" and "device_id" in data:
                            device_id = data["device_id"]
                            self._log.ok(f"New device created: {device_id}")

                            device = self._fetch_device_by_id(device_id)
                            if self.area_filter is not None:
                                if device.get("area_id") not in self.area_filter:
                                    self._log.warn("device ignored by area filter.")
                                    continue

                            if (self._now() - self._last_update_at) < MIN_INTERVAL_BETWEEN_UPDATES:
                                self._log.skip("Skipping due to rate limit.")
                                continue

                            self._last_update_at = self._now()
                            return device

            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._log.error(f"Error: {e}")
                traceback.print_exc()
                sleep_s = min(backoff, MAX_BACKOFF)
                jitter = random.uniform(0, sleep_s * 0.3)
                wait = sleep_s + jitter
                self._log.warn(f"Retrying in {wait:.1f}s.")
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)
            finally:
                if ws:
                    try:
                        ws.close()
                        self._log.info("Connection closed.")
                    except Exception:
                        pass
