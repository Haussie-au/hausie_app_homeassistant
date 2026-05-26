from __future__ import annotations

import json
import os
from dataclasses import dataclass

import requests
import websocket  # pip install websocket-client

from ..settings import Settings
from ..core.flow_logger import get_logger

TARGET_SAVE_ENTITY = "input_button.new_device_save"
log = get_logger("new_device")


@dataclass
class _WSClient:
    url: str
    token: str
    ws: websocket.WebSocket | None = None
    _next_id: int = 1

    def connect(self) -> None:
        self.ws = websocket.create_connection(self.url, timeout=10)
        self.ws.settimeout(30)
        hello = json.loads(self.ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake: {hello}")
        self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_ok = json.loads(self.ws.recv())
        if auth_ok.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")

    def close(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def subscribe(self, event_type: str) -> int:
        if not self.ws:
            raise RuntimeError("WebSocket not connected.")
        req_id = self._next_id
        self._next_id += 1
        self.ws.send(
            json.dumps(
                {"id": req_id, "type": "subscribe_events", "event_type": event_type}
            )
        )
        resp = json.loads(self.ws.recv())
        if not (resp.get("id") == req_id and resp.get("success")):
            raise RuntimeError(f"Subscribe failed: {resp}")
        return req_id

    def ping(self) -> None:
        if not self.ws:
            return
        req_id = self._next_id
        self._next_id += 1
        try:
            self.ws.send(json.dumps({"id": req_id, "type": "ping"}))
        except Exception:
            pass


def _addon_base_url() -> str:
    return os.getenv("HAUSIE_ADDON_URL", "http://localhost:8000").rstrip("/")


def _call_addon_new_device_save() -> None:
    url = f"{_addon_base_url()}/new_device_save"
    requests.post(url, json={}, timeout=10)


def main(argv: list[str] | None = None) -> None:
    settings = Settings()
    ws = _WSClient(settings.HA_WS_URL, settings.HA_TOKEN)
    ws.connect()
    sub_id = ws.subscribe("state_changed")
    log.start("Waiting for input_button.new_device_save...")
    try:
        while True:
            try:
                raw = ws.ws.recv()
            except websocket.WebSocketTimeoutException:
                ws.ping()
                continue
            msg = json.loads(raw)
            if msg.get("type") != "event" or msg.get("id") != sub_id:
                continue
            data = msg.get("event", {}).get("data", {})
            entity_id = data.get("entity_id")
            if entity_id != TARGET_SAVE_ENTITY:
                continue
            _call_addon_new_device_save()
    except KeyboardInterrupt:
        log.warn("Stopped.")
    finally:
        ws.close()


if __name__ == "__main__":
    main()
