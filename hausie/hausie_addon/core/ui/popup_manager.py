from __future__ import annotations


class PopupManager:
    """Build Lovelace button cards that trigger Browser Mod popups."""

    @staticmethod
    def _spinner_content(spinner_icon: str) -> dict:
        return {
            "type": "custom:button-card",
            "icon": spinner_icon,
            "show_name": False,
            "show_state": False,
            "styles": {
                "card": [
                    "height: 120px",
                    "background: transparent",
                    "box-shadow: none",
                ],
                "icon": [
                    "width: 48px",
                    "height: 48px",
                    "animation: spin 1s linear infinite",
                ],
            },
            "extra_styles": (
                "@keyframes spin {\n"
                "  from { transform: rotate(0deg); }\n"
                "  to { transform: rotate(360deg); }\n"
                "}\n"
            ),
        }

    def build_spinner_popup_data(
        self,
        *,
        popup_title: str = "Procesando…",
        spinner_icon: str = "mdi:loading",
        size: str = "narrow",
    ) -> dict:
        """Return Browser Mod popup data for a spinner."""
        return {
            "title": popup_title,
            "size": size,
            "content": self._spinner_content(spinner_icon),
        }

    def build_popup_button(
        self,
        *,
        name: str,
        icon: str,
        title: str,
        content: dict,
        size: str = "narrow",
    ) -> dict:
        """Return a button card that opens a Browser Mod popup."""
        return {
            "type": "button",
            "name": name,
            "icon": icon,
            "tap_action": {
                "action": "fire-dom-event",
                "browser_mod": {
                    "service": "browser_mod.popup",
                    "data": {
                        "title": title,
                        "size": size,
                        "content": content,
                    },
                },
            },
        }

    def build_spinner_popup_button(
        self,
        *,
        name: str,
        icon: str,
        popup_title: str = "Procesando…",
        spinner_icon: str = "mdi:loading",
        size: str = "narrow",
    ) -> dict:
        """Return a button card that opens a spinner popup."""
        content = self._spinner_content(spinner_icon)
        return self.build_popup_button(
            name=name,
            icon=icon,
            title=popup_title,
            content=content,
            size=size,
        )
