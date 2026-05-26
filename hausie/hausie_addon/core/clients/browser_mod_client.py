from __future__ import annotations

from .ha_client import HAClient


class BrowserModClient:
    """Call Browser Mod services through the Home Assistant API."""

    def __init__(self, ha_client: HAClient, browser_id: str | None = None):
        self.ha = ha_client
        self.browser_id = browser_id

    def _with_browser(self, data: dict) -> dict:
        if self.browser_id:
            data = dict(data)
            data["browser_id"] = self.browser_id
        return data

    def navigate(self, path: str) -> dict:
        data = {"path": path}
        return self.ha.call_service("browser_mod", "navigate", self._with_browser(data))

    def refresh(self) -> dict:
        return self.ha.call_service("browser_mod", "refresh", self._with_browser({}))
