from __future__ import annotations

from typing import Any

from ..clients.ha_client import HAClient
from ..flow_logger import get_logger


class NotificationManager:
    """Send Home Assistant notifications via notify or persistent_notification."""

    def __init__(self, *, ha_client: HAClient, default_notify_service: str | None = None) -> None:
        self.ha_client = ha_client
        self.default_notify_service = default_notify_service
        self._log = get_logger("ui")

    def _resolve_notify_service(self, service: str | None) -> tuple[str, str]:
        raw = (service or self.default_notify_service or "notify.notify").strip()
        if "." in raw:
            domain, svc = raw.split(".", 1)
            return domain, svc
        return "notify", raw

    def send(
        self,
        *,
        title: str,
        message: str,
        service: str | None = None,
        targets: list[str] | None = None,
        data: dict[str, Any] | None = None,
        persistent: bool = False,
        notification_id: str | None = None,
    ) -> Any:
        """Send a notification. Uses persistent_notification if persistent=True."""
        payload: dict[str, Any] = {
            "title": title,
            "message": message,
        }
        if data:
            payload["data"] = dict(data)
        if targets:
            payload["target"] = list(targets)

        if persistent:
            if notification_id:
                payload["notification_id"] = notification_id
            self._log.start(f"Sending persistent notification: {title}")
            result = self.ha_client.call_service("persistent_notification", "create", payload)
            self._log.ok("Persistent notification sent.")
            return result

        domain, svc = self._resolve_notify_service(service)
        self._log.start(f"Sending notification via {domain}.{svc}: {title}")
        result = self.ha_client.call_service(domain, svc, payload)
        self._log.ok("Notification sent.")
        return result

