# Changelog

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
