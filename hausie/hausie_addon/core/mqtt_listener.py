from __future__ import annotations

import json
import uuid
from typing import Any

import paho.mqtt.client as mqtt

from .clients.ha_client import HAClient
from .flow_logger import get_logger
from .managers.notification_manager import NotificationManager


class MQTTNotificationListener:
    """Subscribe to Hausie MQTT topics and forward messages to HA notifications."""

    def __init__(
        self,
        *,
        ha_client: HAClient,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        base_topic: str = "hausie",
        device_id: str | None = None,
        plan: str | None = None,
        qos: int = 0,
        keepalive: int = 30,
        client_id: str | None = None,
        default_title: str = "Hausie",
        default_notify_service: str | None = None,
    ) -> None:
        self._log = get_logger("mqtt")
        self._host = host
        self._port = int(port or 1883)
        self._base = base_topic.strip().strip("/")
        self._device_id = (device_id or "").strip() or None
        self._plan = (plan or "").strip() or None
        self._qos = qos
        self._keepalive = keepalive
        self._default_title = default_title
        self._notifier = NotificationManager(
            ha_client=ha_client,
            default_notify_service=default_notify_service,
        )

        cid = client_id or f"hausie-addon-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(client_id=cid, clean_session=True)
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _topics(self) -> list[str]:
        topics = [f"{self._base}/broadcast"]
        if self._device_id:
            topics.append(f"{self._base}/{self._device_id}/notify")
        if self._plan:
            topics.append(f"{self._base}/plan/{self._plan}/notify")
        return topics

    def _on_connect(self, client: mqtt.Client, *_args) -> None:
        for topic in self._topics():
            client.subscribe(topic, qos=self._qos)
        self._log.ok(f"MQTT connected. Subscribed to: {', '.join(self._topics())}")

    def _parse_payload(self, raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8", errors="ignore").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return {"message": str(data)}
        except json.JSONDecodeError:
            return {"message": text}

    def _on_message(self, _client: mqtt.Client, msg: mqtt.MQTTMessage) -> None:
        payload = self._parse_payload(msg.payload or b"")
        if not payload:
            return
        title = payload.get("title") or self._default_title
        message = payload.get("message") or ""
        if not message:
            return
        try:
            self._notifier.send(
                title=title,
                message=message,
                service=payload.get("service"),
                targets=payload.get("targets"),
                data=payload.get("data"),
                persistent=bool(payload.get("persistent", False)),
                notification_id=payload.get("notification_id"),
            )
        except Exception as exc:
            self._log.error(f"MQTT notify failed: {exc}")

    def start(self) -> None:
        self._log.start(f"Connecting MQTT at {self._host}:{self._port}")
        self._client.connect(self._host, self._port, self._keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
