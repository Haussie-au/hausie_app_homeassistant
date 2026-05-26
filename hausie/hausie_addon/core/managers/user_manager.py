from __future__ import annotations

from typing import List, Dict

from ..clients.ha_client import HAClient
from ..inventory.registry_manager import RegistryManager
from ..flow_logger import get_logger

log = get_logger("perm")


class UserManager:
    """Manage Home Assistant users and keep the registry in sync."""

    def __init__(self, *, ha_client: HAClient, registry: RegistryManager) -> None:
        self.ha_client = ha_client
        self.registry = registry

    def fetch_users(self) -> List[Dict]:
        """Fetch the latest users list via WebSocket."""
        return self.ha_client.fetch_users()

    def sync_users(self) -> bool:
        """Fetch users, store them in raw data, and update the registry."""
        try:
            users = self.fetch_users()
        except Exception as exc:
            log.error(f"Failed to fetch users from Home Assistant: {exc}")
            return False

        try:
            self.ha_client.upsert_raw_users(users)
        except Exception as exc:
            log.warn(f"Failed to save users to raw snapshot: {exc}")

        return self.registry.set_users(users)

    def list_users(self) -> List[Dict]:
        """Return users from the registry."""
        return self.registry.list_users()
