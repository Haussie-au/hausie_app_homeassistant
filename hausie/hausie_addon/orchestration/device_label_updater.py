from __future__ import annotations

import time
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import TimeoutError, sync_playwright
from ..core.flow_logger import get_logger


class DeviceLabelUpdater:
    """Update device labels through the Home Assistant UI using Playwright."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        headless: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.startswith("http://homeassistant:8123"):
            self.base_url = "http://homeassistant.local:8123"
        elif self.base_url.startswith("https://homeassistant:8123"):
            self.base_url = "https://homeassistant.local:8123"
        self.username = username
        self.password = password
        self.headless = headless
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self._log = get_logger("new_device")

    def _launch_browser(self) -> None:
        if self.page is not None:
            return
        self._ensure_playwright_browser()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def _is_logged_in(self) -> bool:
        try:
            if "/auth/" in self.page.url or "/login" in self.page.url:
                return False
        except Exception:
            pass
        for sel in ("home-assistant", "ha-sidebar", "ha-drawer", "ha-app"):
            try:
                if self.page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _ensure_playwright_browser(self) -> None:
        try:
            executable = self.playwright.chromium.executable_path
        except Exception:
            return
        if executable and Path(executable).exists():
            return
        try:
            self._log.start("Playwright browsers missing; installing Chromium.")
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
            )
            self._log.ok("Playwright Chromium installed.")
        except Exception as exc:
            self._log.warn(f"Playwright install failed: {exc}")

    def _login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("HA_UI_USERNAME y HA_UI_PASSWORD son requeridos para Playwright.")
        if self._is_logged_in():
            self._log.info("Already authenticated.")
            return
        self._log.start("Logging into Home Assistant.")
        self.page.wait_for_selector('input#username, input[name="username"]', timeout=10000)

        user_input = self.page.locator('input#username, input[name="username"], input[autocomplete="username"]').first
        pwd_input = self.page.locator('input#password, input[name="password"], input[autocomplete="current-password"]').first
        user_input.fill(self.username)
        pwd_input.fill(self.password)

        chk = self.page.locator('input[type="checkbox"]')
        if chk.count() > 0:
            try:
                if not chk.first.is_checked():
                    chk.first.check()
            except Exception:
                pass

        for attempt in range(2):
            try:
                btn = self.page.get_by_role("button", name="Log in")
                if btn.count() == 0:
                    btn = self.page.get_by_role("button", name="Sign in")
                if btn.count() == 0:
                    btn = self.page.locator(
                        'mwc-button:has-text("Log in"), mwc-button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Sign in")'
                    ).first

                btn.wait_for(state="visible", timeout=8000)
                btn.click(timeout=5000)

                try:
                    self.page.wait_for_url("**/lovelace/**", timeout=6000)
                except TimeoutError:
                    self.page.keyboard.press("Enter")

                self.page.wait_for_url("**/lovelace/**", timeout=15000)
                self._log.ok("Logged in.")
                return
            except TimeoutError:
                if attempt == 0:
                    continue
                if self._is_logged_in():
                    self._log.ok("Logged in.")
                    return
                self._log.warn("Login form not detected or button click failed.")
                return

    def _check_and_login(self) -> None:
        if self._is_logged_in():
            self._log.info("Already authenticated.")
            return
        try:
            self.page.wait_for_selector('input#username, input[name="username"]', timeout=3000)
            self._log.info("Login form detected, logging in.")
            self._login()
        except TimeoutError:
            if self._is_logged_in():
                self._log.info("Already authenticated.")
            else:
                self._log.warn("Login form not detected and session not authenticated.")

    def _open_device_page(self, device_id: str) -> None:
        self._launch_browser()
        if device_id.startswith("http://") or device_id.startswith("https://"):
            device_url = device_id
        elif "/config/devices/device/" in device_id:
            device_url = f"{self.base_url}{device_id}" if device_id.startswith("/") else device_id
        else:
            device_url = f"{self.base_url}/config/devices/device/{device_id}"
        self._log.start(f"Navigating to {device_url}")

        self.page.goto(self.base_url)
        self._check_and_login()

        self.page.goto(device_url)
        self._check_and_login()
        self.page.wait_for_load_state("domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=25000)
        except TimeoutError:
            pass
        try:
            self.page.wait_for_url("**/config/devices/device/**", timeout=15000)
        except TimeoutError:
            self._log.warn(f"URL after navigation: {self.page.url}")
        try:
            self.page.wait_for_selector("ha-device-info-card, ha-card", timeout=15000)
        except TimeoutError:
            pass

    def _open_edit_dialog(self) -> None:
        try:
            self.page.wait_for_selector("ha-device-info-card, ha-card", timeout=10000)
        except TimeoutError:
            pass
        selectors = [
            'ha-icon-button[title="Edit settings"]',
            'mwc-icon-button[title="Edit settings"]',
            'ha-icon-button[aria-label="Edit settings"]',
            'mwc-icon-button[aria-label="Edit settings"]',
            'button:has-text("Edit settings")',
            'mwc-button:has-text("Edit settings")',
            'ha-button:has-text("Edit settings")',
            'ha-device-info-card ha-icon-button[title="Edit"]',
            'ha-device-info-card mwc-icon-button[title="Edit"]',
            'ha-device-info-card ha-icon-button[aria-label="Edit"]',
            'ha-device-info-card mwc-icon-button[aria-label="Edit"]',
            'button:has-text("Editar configuración")',
            'mwc-button:has-text("Editar configuración")',
            'ha-button:has-text("Editar configuración")',
        ]
        for selector in selectors:
            btn = self.page.locator(selector)
            if btn.count() == 0:
                continue
            btn.first.click()
            return
        menu_selectors = [
            'ha-icon-button[aria-label="More options"]',
            'ha-icon-button[title="More options"]',
            'ha-icon-button[aria-label="Device settings"]',
            'ha-icon-button[title="Device settings"]',
            'mwc-icon-button[aria-label="More options"]',
            'mwc-icon-button[title="More options"]',
            'mwc-icon-button[aria-label="Device settings"]',
            'mwc-icon-button[title="Device settings"]',
        ]
        for selector in menu_selectors:
            menu_btn = self.page.locator(selector)
            if menu_btn.count() == 0:
                continue
            menu_btn.first.click()
            item_selectors = [
                'ha-list-item:has-text("Edit settings")',
                'mwc-list-item:has-text("Edit settings")',
                'ha-list-item:has-text("Editar configuración")',
                'mwc-list-item:has-text("Editar configuración")',
                'ha-list-item:has-text("Edit")',
                'mwc-list-item:has-text("Edit")',
                'ha-list-item:has-text("Editar")',
                'mwc-list-item:has-text("Editar")',
            ]
            for item_sel in item_selectors:
                item = self.page.locator(item_sel)
                if item.count() == 0:
                    continue
                item.first.click()
                return
        raise TimeoutError("No se encontro el boton de editar el dispositivo.")

    def _find_dialog(self):
        dialog = self.page.locator("ha-dialog[open], mwc-dialog[open]").first
        dialog.wait_for(state="attached", timeout=10000)
        try:
            dialog.wait_for(state="visible", timeout=3000)
        except TimeoutError:
            pass
        return dialog

    def _set_label_in_dialog(self, label_name: str) -> None:
        dialog = self._find_dialog()
        add_label_selectors = [
            'button:has-text("Add label")',
            'mwc-button:has-text("Add label")',
            'ha-button:has-text("Add label")',
            'button:has-text("Agregar etiqueta")',
            'mwc-button:has-text("Agregar etiqueta")',
            'ha-button:has-text("Agregar etiqueta")',
        ]
        for selector in add_label_selectors:
            btn = dialog.locator(selector)
            if btn.count() == 0:
                continue
            btn.first.click()
            break

        label_input = dialog.locator('input[type="search"]')
        if label_input.count() == 0:
            label_input = dialog.locator('ha-textfield input')
        if label_input.count() == 0:
            label_input = dialog.locator('input[aria-label="Labels"], input[placeholder="Labels"]')
        if label_input.count() == 0:
            raise TimeoutError("No se encontro el buscador de labels.")

        try:
            label_input.first.click(force=True)
        except Exception:
            pass
        try:
            label_input.first.fill("")
        except Exception:
            pass
        time.sleep(1)
        try:
            label_input.first.fill(label_name)
        except Exception:
            self.page.keyboard.type(label_name)
        if not label_input.first.input_value():
            self.page.keyboard.type(label_name)
        self.page.keyboard.press("Tab")
        self.page.keyboard.press("Enter")

    def _save_dialog(self) -> None:
        selectors = [
            lambda: self.page.get_by_role("button", name="Save"),
            lambda: self.page.get_by_role("button", name="Update"),
            lambda: self.page.get_by_role("button", name="Guardar"),
            lambda: self.page.get_by_role("button", name="Actualizar"),
            lambda: self.page.get_by_role("button", name="Update settings"),
            lambda: self.page.locator('mwc-button:has-text("Save")'),
            lambda: self.page.locator('mwc-button:has-text("Update")'),
            lambda: self.page.locator('mwc-button:has-text("Guardar")'),
            lambda: self.page.locator('mwc-button:has-text("Actualizar")'),
            lambda: self.page.locator('mwc-button:has-text("Update settings")'),
        ]
        for sel_fn in selectors:
            btn = sel_fn()
            if btn.count() == 0:
                continue
            btn.first.click(timeout=10000)
            return
        raise TimeoutError("No se encontro el boton de guardar en el dialogo.")

    def update_device_label(self, device_id: str, label_name: str, *, pause_after_nav: bool = False) -> bool:
        if not device_id or not label_name or label_name.strip().lower() == "none":
            return False
        self._open_device_page(device_id)
        if pause_after_nav:
            input("[label-update] Paused after navigation. Press Enter to continue...")
        self._open_edit_dialog()
        self._set_label_in_dialog(label_name)
        self._save_dialog()
        return True

    def close(self) -> None:
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
