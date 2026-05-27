# Hausie App

Hausie connects a Home Assistant OS installation with Hausie Cloud and manages
the generated Hausie dashboards, helpers, scripts, groups, covers, and support flows.

## Features

- Connect a Home Assistant device to Hausie Cloud with a pairing code.
- Create and maintain Hausie dashboards and generated YAML assets.
- Keep a persistent device state inside the add-on data directory.
- Enable Cloud-controlled remote support with SSH and Tailscale add-on integration.

## Configuration

Set these values in the add-on configuration UI:

```yaml
ha_token: ""
ha_ui_username: ""
ha_ui_password: ""
hausie_cloud_url: ""
pairing_code: ""
tailscale_ip: ""
```

Optional logging and support settings are also exposed in the add-on schema.

If you want Hausie to recreate the main Hausie dashboard through Playwright,
configure a dedicated Home Assistant UI user in `ha_ui_username` and
`ha_ui_password`.

## Storage model

- Add-on-owned files are stored in the add-on config directory.
- Home Assistant configuration files are accessed through the mapped Home Assistant config directory.
- Runtime state is persisted in `/data`.

## Support

Production users should install Hausie App from this repository through the Home Assistant Add-on Store.
Local SSH deploy workflows are intentionally kept out of this repository.
