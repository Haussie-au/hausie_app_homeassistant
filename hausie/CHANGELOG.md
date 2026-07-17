# Changelog

## 0.2.86

- Allow the local installer to save credentials for users it has already provisioned without attempting a second password reset.
- Verify the existing Hausie administrator and support accounts before accepting installer-provided credentials.

## 0.2.85

- Report installed Browser Mod and Button Card versions with every heartbeat.
- Process cloud component-version updates through Home Assistant and HACS, with verified official-release fallback for unmanaged installations.
- Verify each installed version before reporting success and apply bounded retry backoff after failures.
- Restart Home Assistant only after Browser Mod changes and reload browser frontends after Button Card-only updates.

## 0.2.84

- Stop registering the main Hausie dashboard as a YAML dashboard in `configuration.yaml`.
- Preserve the storage-managed Hausie dashboard provided by the base Home Assistant backup.
- Remove any legacy `hausie-dashboard` YAML registration during the next configuration sync.

## 0.2.83

- Allow an initialized Hausie installation to save and verify missing local credentials without rebuilding dashboards or automations.
- Report initialization as complete only when the required Home Assistant credentials are also valid.
- Reduce idle setup status polling and keep internal status requests out of the add-on log while preserving real requests and errors.
- Use a waiting cursor only while initialization is actively running.

## 0.2.82

- Change existing Hausie account passwords through Home Assistant's administrator WebSocket API using stable user IDs.
- Remove the dedicated Supervisor Auth API permission because password rotation no longer calls the role-restricted `/auth/reset` endpoint.
- Keep the add-on on the narrower `manager` Supervisor role while preserving users, owner status, permissions, and account relationships.

## 0.2.81

- Grant the add-on the dedicated Home Assistant authentication permission required to update existing local account passwords.
- Remove the host port mapping so the Hausie web interface is available only through authenticated Home Assistant Ingress.
- Restrict setup, credential, pairing, status, and log pages to authenticated Ingress requests from the Supervisor proxy.
- Add CSRF protection to credential, initialization, and pairing mutations.
- Store local Hausie state with owner-only file permissions and prevent browser caching of sensitive responses.

## 0.2.80

- Change existing `hausie_admin` and `hausie_support_user` passwords through the Supervisor authentication API without deleting or recreating either account.
- Preserve the Home Assistant owner account, user IDs, permissions, and existing account relationships during credential setup.
- Create a required Hausie account only when it does not already exist.

## 0.2.79

- Verify the local Home Assistant token and required Hausie accounts at startup and after saving credentials.
- Hide the configuration dashboard credential shortcut and credential forms after successful verification.
- Show the local Hausie App portal screen with a link to `https://portal.hausiehome.com` once setup is complete.

## 0.2.78

- Prevent setup from remaining indefinitely in progress when Playwright installation stalls by applying a five-minute installer timeout.
- Log Playwright driver and browser startup milestones so setup logs identify the exact blocked stage.

## 0.2.77

- Add separate local credential fields for `hausie_admin` and `hausie_support_user` in the setup and credentials screens.
- Store only the support-user password required by Playwright; use the administrator password only to update the local Home Assistant account.
- Mark initialization as failed when its background workflow exits unexpectedly, rather than leaving the setup screen in a permanent loading state.

## 0.2.76

- Align the local Hausie App setup, pairing, and credentials screens with the shared Hausie visual identity.

## 0.2.75

- Open the Hausie App setup through Home Assistant's standard `/app/<slug>` route instead of a manually constructed ingress URL.

## 0.2.74

- Fix ingress setup, pairing, and credentials pages so their requests remain inside the Home Assistant ingress proxy.

## 0.2.73

- Resolve the installed Hausie app slug from Supervisor and use it for generated app, setup, credentials, and pairing navigation paths.

## 0.2.72

- Do not block add-on startup while downloading Playwright browsers; install Chromium lazily only when a dashboard UI operation requires it.

## 0.2.71

- Use stable Home Assistant app ingress routes in persistent dashboards instead of expiring Supervisor ingress tokens.
- Refresh the temporary setup dashboard URL during startup when it has not yet been replaced by the Cloud-generated configuration dashboard.

## 0.2.70

- Remove runtime credentials, pairing, Cloud endpoint, and Tailscale fields from the add-on configuration screen; setup and credentials ingress flows now manage those values.
- Migrate legacy Home Assistant runtime credentials from add-on options into the private local device state when present.

## 0.2.69

- Update generated Hausie App and ingress navigation paths to the definitive `5d76f103_hausie` Home Assistant add-on slug.

## 0.2.68

- Replace the add-on icon with the Hausie teal brand mark.

## 0.2.67

- Add a mobile-first local setup flow that saves missing Home Assistant credentials, pairs the Pi with a Hausie home, and runs the initial base and inventory generation from the add-on ingress UI.
- Create a local Configuration bootstrap dashboard only when no Cloud-generated dashboard exists, so installers always have an entry point after restoring the base backup.

## 0.2.66

- Use `https://api.hausiehome.com` as the default Hausie Cloud endpoint for new installations and when no custom endpoint is configured.

## 0.2.65

- Stop registering or generating the internal Test dashboard in Home Assistant while keeping test helpers and automations available for development.

## 0.2.64

- Add a local Hausie credentials screen inside the add-on ingress UI so the Home Assistant token and support-user password can be stored internally instead of relying only on add-on options.
- Show a large `Set Hausie credentials` button in the configuration dashboard only while the required token or support password is missing, and hide it automatically after saving valid values.
- Hardcode the Playwright support username to `hausie_support_user` for the new local credential flow and reuse saved credentials across refresh and repair flows.

## 0.2.63

- Replace deprecated `armv7` add-on architecture metadata with `armhf` so Supervisor no longer warns while validating the store manifest.
- Shut the add-on HTTP server down cleanly on `SIGTERM` so Supervisor stops the app without reporting an unclean exit.

## 0.2.62

- Remove advanced log and Tailscale management fields from the add-on user-facing options so end users no longer configure internal support settings manually.
- Keep Hausie logs in the internal `/data/hausie_addon.log` file with built-in rotation, without clearing the log on startup.
- Autodetect the Tailscale add-on slug from Supervisor and always let Hausie manage Tailscale for remote support.

## 0.2.61

- Add label catalog sync from Hausie Cloud into Home Assistant so labels can be created and updated centrally and applied on the next heartbeat.
- Force label catalog sync before `Refresh Hausie`, `Repair Hausie`, and base rebuild flows so regenerated helpers and dashboards use the latest labels immediately.

## 0.2.60

- Add a mobile-first local ZHA pairing wizard in the add-on ingress UI so plan 2+ homes can start pairing, wait for devices to finish configuring, assign room and labels, and refresh Hausie from the Raspberry Pi.
- Patch the `Add Device` dashboard shortcut locally to the add-on pairing wizard ingress path for unlocked plans.
- Allow the device-save flow to apply multiple labels in one pass.

## 0.2.59

- Change the `Hausie App` button in the configuration dashboard to use a direct internal URL action so it reliably opens `/config/app/c5bb2897_hausie/info`.

## 0.2.58

- Wait for rebuilt helper entities to exist again after `Repair Hausie` before restoring their values, so blinds toggle and selected-day helpers are restored after Home Assistant fully reloads them.

## 0.2.57

- Restore persisted helper values only after Home Assistant comes back from the final `Repair Hausie` restart so blinds automation helpers such as enabled toggles and selected days are not reset by the restart.

## 0.2.56

- Remove the `Add Zigbee Device` button from the configuration dashboard while keeping the underlying Zigbee onboarding logic available in the add-on.
- Keep the `Hausie App` dashboard shortcut pointed at `/config/app/c5bb2897_hausie/info`.

## 0.2.55

- Accept cloud subscription payloads that return `tier` instead of `plan` so the add-on keeps the correct licensed plan instead of falling back to `plan_1`.

## 0.2.54

- Stop tracking the generated `__pycache__/addon_server.cpython-310.pyc` file so local Python bytecode no longer shows up as a pending Git change.

## 0.2.53

- Run long-lived Hausie add-on actions such as refresh, repair, restart, and base rebuild in the background so the UI no longer gets stuck waiting for a long HTTP response.
- Return an immediate accepted response for manual Hausie actions and reject overlapping runs cleanly when another Hausie workflow is already in progress.

## 0.2.52

- Treat expected Home Assistant restart disconnects as transient so rebuild logs do not report false restart failures.
- Reduce remote-support warning noise while Home Assistant is temporarily unavailable during restart.
- Ignore client disconnects cleanly when the add-on finishes responding after a long rebuild request.

## 0.2.51

- Stop auto-syncing inventory when the Home Assistant inventory changes after boot.
- Keep inventory-change detection local only by updating the stored baseline without triggering cloud generation or showing a visual notice.

## 0.2.50

- Stop auto-running `sync_inventory` on add-on startup when no previous inventory signature exists.
- Store a local inventory baseline at startup and only sync to cloud when the inventory changes after boot or when a manual refresh is requested.

## 0.2.49

- Fix Home Assistant service reload detection after applying cloud artifacts so helpers, groups, and YAML-backed entities are reloaded instead of being skipped as unavailable.
- Restore `homeassistant.reload_core_config` during post-apply reloads so dashboard entities such as YAML cover groups are picked up reliably after rebuilds.

## 0.2.48

- Preserve the remote support helper during cleanup flows and restore it on add-on startup so `input_boolean.allow_remote_support` does not become unavailable in the config dashboard.

## 0.2.47

- Detect Home Assistant inventory changes in the add-on and automatically run `sync_inventory` so newly added devices are pushed to Hausie Cloud without waiting for a manual refresh.

## 0.2.37

- Refresh the add-on license state from cloud before rebuild and cloud asset generation so dashboards are rebuilt with the current paid plan instead of stale cached plan data.

## 0.2.36

- Remove generated cover YAMLs during Hausie cleanup so stale global blinds groups do not survive rebuilds.

## 0.2.35

- Send the add-on's current license plan and status when requesting cloud-generated artifacts.
- Resolve config-dashboard plan gating from the authoritative cloud license context before falling back to defaults.
- Reset cached feature flags when the dashboard subscription plan changes.

## 0.2.27

- Remove legacy local generation paths from the add-on so Hausie Cloud is the only artifact generator.
- Keep the add-on focused on Home Assistant execution, artifact application, Browser Mod, Playwright, and support flows.
- Align help-message ownership so product defaults come from Cloud while the add-on only persists and applies local state.

## 0.2.14

- Start the Tailscale add-on when remote support opens.
- Stop the Tailscale add-on when remote support closes or expires.
- Keep Tailscale management configurable with `HAUSIE_SUPPORT_MANAGE_TAILSCALE`.

## 0.2.13

- Remove the temporary Home Assistant support user automatically when remote support closes.
- Disable local remote support when Cloud closes or expires the active support session.
- Queue support-user removal from Cloud when an admin deactivates support.

## 0.2.12

- Require an active Cloud remote-support session before opening remote support.
- Add persisted Cloud remote-support session support for admin authorization/audit.

## 0.2.11

- Add Cloud-controlled remote support session policy.
- Fetch support public keys, timeout, and expiry from `/api/device/support-session`.
- Keep local support key fallback if Cloud session policy is unavailable.

## 0.2.10

- Normalize heartbeat actions from Cloud with a schema-versioned payload contract.
- Validate heartbeat actions against an add-on allowlist before execution.
- Ignore expired heartbeat actions instead of executing stale commands.

## 0.2.9

- Delegate `rebuild_hausie` execution-plan decisions to Hausie Cloud when available.
- Preserve local rebuild decision fallback if Cloud is unavailable or returns an invalid plan.

## 0.2.8

- Send heartbeat frequently while remote support is open.
- Support temporary Home Assistant UI support sessions.
- Sync support public keys into the SSH add-on options.
- Report Tailscale IP and IP source in heartbeat.
