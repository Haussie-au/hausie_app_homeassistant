# Hausie Add-ons Repository

This folder is the source template for the public Home Assistant add-on repository.

Target repository:

```text
https://github.com/Haussie-au/hausie-addons
```

Expected published structure:

```text
hausie-addons/
  repository.yaml
  hausie/
    config.yaml
    Dockerfile
    run.sh
    requirements.txt
    README.md
    DOCS.md
    CHANGELOG.md
    hausie_addon/
```

Home Assistant users add the repository URL under:

```text
Settings -> Add-ons -> Add-on Store -> Repositories
```

Then install:

```text
Hausie Add-on
```

## Release Flow

1. Update the add-on code in `hausie_haos_addon/`.
2. Bump `hausie_haos_addon/config.yaml` version.
3. Copy `hausie_haos_addon/` into this repository as `hausie/`.
4. Commit and push to `Haussie-au/hausie-addons`.
5. Home Assistant will detect the new version from `hausie/config.yaml`.

## Development vs Production

Development:

```text
deploy.ps1 over SSH to one Pi
```

Production:

```text
Home Assistant add-on repository
```

Do not deploy customer updates by SSH once devices are in the field.
