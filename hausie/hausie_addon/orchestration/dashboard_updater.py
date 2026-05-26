from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from playwright.sync_api import sync_playwright, TimeoutError
from ..core.flow_logger import get_logger


class DashboardUpdater:
    """Update and read Home Assistant dashboards via the raw YAML editor."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        headless: bool = False,
        storage_state_path: str | None = None,
    ):
        """Initialize the Playwright automation session."""
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.headless = headless
        self.storage_state_path = None  # force new session each time

        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self._log = get_logger("ui")

    # -----------------------
    # Navigation / Session
    # -----------------------
    def _launch_browser(self):
        """Start the browser and page if not already open."""
        if self.page is None:
            self._ensure_playwright_browser()
            self.browser = self.playwright.chromium.launch(headless=self.headless)
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
            self.page.on("pageerror", self._log_page_error)
            self.page.on("requestfailed", self._log_request_failed)

    def _ensure_playwright_browser(self):
        """Install Chromium if Playwright browsers are missing."""
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

    def _log_page_error(self, exc):
        """Log page errors from Playwright."""
        self._log.error(f"page-error: {exc}")

    def _log_request_failed(self, request):
        """Log failed requests from Playwright."""
        failure = request.failure
        error_text = getattr(failure, "error_text", str(failure)) if failure else "Unknown error"
        self._log.warn(f"request-failed: {request.url} - {error_text}")

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

    def _login(self):
        """Perform the login flow in the HA UI."""
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

    def _check_and_login(self):
        """Log in only if the login form is present."""
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

    # -----------------------
    # Raw editor helpers
    # -----------------------
    def _open_raw_editor(self, dashboard_path: str):
        """Navigate to the raw dashboard editor view."""
        self._launch_browser()
        dashboard_edit_url = (
            dashboard_path
            if dashboard_path.startswith("http://") or dashboard_path.startswith("https://")
            else f"{self.base_url}/{dashboard_path}?edit=1"
        )
        self.page.goto(dashboard_edit_url)
        self._check_and_login()
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_load_state("networkidle", timeout=25000)

        menu_selectors = [
            'ha-icon-button#dashboardmenu',
            'ha-icon-button[aria-label="Open dashboard menu"]',
            'button[aria-label="Open dashboard menu"]',
            'mwc-icon-button[title="Open dashboard menu"]',
            "ha-button-menu",
            'button[title="Open dashboard menu"]',
            'ha-icon-button[title="Edit dashboard"]',
            'ha-icon-button[aria-label="Edit dashboard"]',
            'button[aria-label="Edit dashboard"]',
            'button[title="Edit dashboard"]',
            'ha-icon-button:has-text("Edit dashboard")',
            'mwc-icon-button:has-text("Edit dashboard")',
        ]
        for sel in menu_selectors:
            try:
                self.page.wait_for_selector(sel, timeout=8000)
                self.page.click(sel)
                break
            except TimeoutError:
                continue
        else:
            raise TimeoutError("Dashboard menu button not found.")

        raw_selectors = [
            'ha-list-item:has-text("Raw configuration editor")',
            'mwc-list-item:has-text("Raw configuration editor")',
            'ha-dropdown-item:has-text("Raw configuration editor")',
            'ha-md-menu-item:has-text("Raw configuration editor")',
            'ha-list-item:has-text("Edit in YAML")',
            'mwc-list-item:has-text("Edit in YAML")',
            'ha-dropdown-item:has-text("Edit in YAML")',
            'ha-md-menu-item:has-text("Edit in YAML")',
            'ha-list-item:has-text("Editar en YAML")',
            'mwc-list-item:has-text("Editar en YAML")',
            'ha-dropdown-item:has-text("Editar en YAML")',
            'ha-md-menu-item:has-text("Editar en YAML")',
            'ha-list-item:has-text("Editor de configuración sin procesar")',
            'mwc-list-item:has-text("Editor de configuración sin procesar")',
            'ha-dropdown-item:has-text("Editor de configuración sin procesar")',
            'ha-md-menu-item:has-text("Editor de configuración sin procesar")',
            'button:has-text("Edit in YAML")',
            'button:has-text("Editar en YAML")',
            'mwc-button:has-text("Edit in YAML")',
            'mwc-button:has-text("Editar en YAML")',
        ]
        for sel in raw_selectors:
            try:
                self.page.wait_for_selector(sel, timeout=8000)
                self.page.click(sel)
                break
            except TimeoutError:
                continue
        else:
            raise TimeoutError("Raw configuration editor option not found in menu.")

        self.page.wait_for_selector('div.cm-content[contenteditable="true"]', timeout=10000)

    def _read_raw_yaml(self) -> str:
        """Read YAML content from the raw editor."""
        return self.page.inner_text('div.cm-content[contenteditable="true"]')

    def _write_raw_yaml(self, yaml_text: str):
        """Replace YAML content in the raw editor."""
        self.page.click('div.cm-content[contenteditable="true"]')
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Backspace")
        self.page.keyboard.insert_text(yaml_text)

    def _save_raw_editor(self):
        """Click the save button in the raw editor."""
        selectors = [
            lambda: self.page.get_by_role("button", name="Save"),
            lambda: self.page.get_by_role("button", name="Guardar"),
            lambda: self.page.locator('mwc-button[raised]:has-text("Save")'),
            lambda: self.page.locator('mwc-button[raised]:has-text("Guardar")'),
        ]
        for sel_fn in selectors:
            btn = sel_fn()
            if btn.count() == 0:
                continue
            btn.click(timeout=10000)
            return
        raise TimeoutError("Save button not found (tried role/text selectors).")

    def _reset_raw_editor(self):
        """Click the reset button in the raw editor, if present."""
        selectors = [
            lambda: self.page.get_by_role("button", name="Reset"),
            lambda: self.page.get_by_role("button", name="Restablecer"),
            lambda: self.page.get_by_role("button", name="Reiniciar"),
            lambda: self.page.locator('mwc-button:has-text("Reset")'),
            lambda: self.page.locator('mwc-button:has-text("Restablecer")'),
            lambda: self.page.locator('mwc-button:has-text("Reiniciar")'),
            lambda: self.page.locator('button:has-text("Reset")'),
            lambda: self.page.locator('button:has-text("Restablecer")'),
            lambda: self.page.locator('button:has-text("Reiniciar")'),
        ]
        for sel_fn in selectors:
            btn = sel_fn()
            if btn.count() == 0:
                continue
            btn.click(timeout=10000)
            return True
        return False

    # -----------------------
    # Public API
    # -----------------------
    def write_yaml_to_ui(self, dashboard_path: str, yaml_text: str) -> None:
        """Open the raw editor and replace its YAML content."""
        self._open_raw_editor(dashboard_path)
        self._write_raw_yaml(yaml_text)
        self._save_raw_editor()

    def read_yaml_from_ui(self, dashboard_path: str) -> str:
        """Open the raw editor and return its YAML content."""
        self._open_raw_editor(dashboard_path)
        return self._read_raw_yaml()

    def clear_yaml_and_reset(self, dashboard_path: str) -> None:
        """Clear the raw YAML, save, then press reset if available."""
        self._open_raw_editor(dashboard_path)
        self._write_raw_yaml("")
        self._save_raw_editor()
        self._reset_raw_editor()

    def replace_yaml_and_reset(self, dashboard_path: str, yaml_text: str) -> None:
        """Replace YAML content, save, then press reset if available."""
        self._open_raw_editor(dashboard_path)
        self._write_raw_yaml(yaml_text)
        self._save_raw_editor()
        self._reset_raw_editor()

    # -----------------------
    # Teardown
    # -----------------------
    def close(self):
        """Close the browser and Playwright resources."""
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
