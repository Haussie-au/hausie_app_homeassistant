# Hausie Add-on

The Hausie add-on connects Home Assistant OS devices to Hausie Cloud.

## Configuration

Required:

```yaml
ha_token: "YOUR_HOME_ASSISTANT_LONG_LIVED_TOKEN"
hausie_cloud_url: "https://YOUR_HAUSIE_CLOUD_URL"
pairing_code: "PAIRING_CODE_FROM_HAUSIE"
```

Optional:

```yaml
ha_ui_username: "hausie_bot"
ha_ui_password: "A_DEDICATED_HOME_ASSISTANT_UI_PASSWORD"
tailscale_ip: "100.x.x.x"
```

Use `tailscale_ip` when the add-on cannot automatically detect the Tailscale IP.
Use `ha_ui_username` and `ha_ui_password` if you want Hausie to recreate or update
the main Hausie dashboard through the Home Assistant UI with Playwright.

## Remote Support

Remote support is closed by default.

When enabled from the Home Assistant dashboard:

- Hausie fetches active support public keys from Hausie Cloud.
- Hausie adds those keys to the SSH add-on configuration.
- Hausie starts the SSH add-on.
- Hausie reports support status, heartbeat, Home Assistant version, add-on version, and Tailscale IP to Hausie Cloud.

When disabled:

- Hausie removes the managed support keys.
- Hausie stops the SSH add-on.
- Hausie removes the temporary Home Assistant UI support user if requested by Cloud.

## Playwright Dashboard Updates

For the main Hausie dashboard, Hausie currently logs into the Home Assistant UI
with Playwright when dashboard UI updates are needed.

Recommended setup:

- Create a dedicated Home Assistant user such as `hausie_bot`.
- Give it the minimum permissions that still allow dashboard editing.
- Store that username and password only in the add-on options.

Without these credentials, Hausie will skip the UI dashboard update step.

## Updates

Production devices should install this add-on from the Hausie Home Assistant add-on repository, not by SSH deploy.
