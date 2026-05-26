Hausie HAOS Add-on

Folder contents:
 - config.yaml
 - Dockerfile
 - run.sh
 - hausie_addon/ (code copied from repo root)
 - requirements.txt
 - deploy.ps1 (sync + restart on Pi)

Deploy to Pi (PowerShell):
  $env:PI_HOST="192.168.1.108"
  $env:PI_USER="root"
  $env:PI_PORT="22"
  $env:PI_SSH_KEY="C:\\Users\\mapet\\.ssh\\id_ed25519_hausie"
  $env:HAUSIE_ADDON_REMOTE_DIR="/addons/hausie"
  $env:HAUSIE_ADDON_SLUG="hausie"
  .\\deploy.ps1

Notes:
 - HAOS custom add-ons live under /addons/<folder>.
 - After deploy, the script reloads add-ons and restarts the add-on.
 - Update config in HA UI: Settings -> Add-ons -> Hausie -> Configuration.
