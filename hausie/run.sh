#!/bin/sh
set -e

OPTIONS_FILE="/data/options.json"

echo "=============================="
echo "HAUSIE APP HAS STARTED"
echo "=============================="

if [ -f "$OPTIONS_FILE" ]; then
  eval "$(python - <<'PY'
import json
import shlex

mapping = {
    "ha_token": "HA_TOKEN",
    "ha_ui_username": "HA_UI_USERNAME",
    "ha_ui_password": "HA_UI_PASSWORD",
    "hausie_cloud_url": "HAUSIE_CLOUD_URL",
    "pairing_code": "HAUSIE_PAIRING_CODE",
    "tailscale_ip": "HAUSIE_TAILSCALE_IP",
}

with open("/data/options.json", "r", encoding="utf-8") as f:
    data = json.load(f) or {}

for key, env in mapping.items():
    if key not in data:
        continue
    val = data.get(key)
    if isinstance(val, bool):
        val = "true" if val else "false"
    if val is None:
        continue
    if isinstance(val, str) and not val.strip():
        continue
    print(f"export {env}={shlex.quote(str(val))}")
PY
)"
fi

if [ -z "${HAUSIE_LOCAL_MODE:-}" ]; then
  export HAUSIE_LOCAL_MODE="true"
fi

export PI_HA_CONFIG_DIR="${PI_HA_CONFIG_DIR:-/homeassistant}"
export PI_DASHBOARD_DIR="${PI_DASHBOARD_DIR:-/homeassistant/dashboards}"
export PI_CONFIG_PATH="${PI_CONFIG_PATH:-/homeassistant/configuration.yaml}"

export HAUSIE_LOG_TO_STDOUT="true"
export TEST_LOG_CLEAR_ON_START="true"

export HAUSIE_LOG_FILE=""

export PLAYWRIGHT_BROWSERS_PATH="/data/playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

is_pkg_installed() {
  dpkg -s "$1" >/dev/null 2>&1 || dpkg -s "${1}t64" >/dev/null 2>&1
}

MISSING_PACKAGES=""
for pkg in \
  libglib2.0-0 \
  libnss3 \
  libnspr4 \
  libdbus-1-3 \
  libatk1.0-0 \
  libatk-bridge2.0-0 \
  libcups2 \
  libexpat1 \
  libdrm2 \
  libxcb1 \
  libxkbcommon0 \
  libatspi2.0-0 \
  libx11-6 \
  libxcomposite1 \
  libxdamage1 \
  libxext6 \
  libxfixes3 \
  libxrandr2 \
  libgbm1 \
  libpango-1.0-0 \
  libcairo2 \
  libasound2; do
  if ! is_pkg_installed "$pkg"; then
    MISSING_PACKAGES="$MISSING_PACKAGES $pkg"
  fi
done

if [ -n "$MISSING_PACKAGES" ]; then
  echo "Missing system packages:$MISSING_PACKAGES"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends $MISSING_PACKAGES
    rm -rf /var/lib/apt/lists/*
  else
    echo "apt-get not available; skipping system package install."
  fi
fi

PLAYWRIGHT_MISSING="$(python - <<'PY'
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("skip")
    raise SystemExit(0)

with sync_playwright() as p:
    executable = p.chromium.executable_path

if not executable or not Path(executable).exists():
    print("missing")
PY
)"

if ls "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* >/dev/null 2>&1; then
  echo "Playwright browsers already present; skipping install."
elif [ "$PLAYWRIGHT_MISSING" = "missing" ]; then
  echo "Playwright browsers missing; installing Chromium."
  python -m playwright install chromium || true
fi

python -m hausie_addon.addon_server
