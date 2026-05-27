# Hausie Add-ons Repository

This repository is the source of truth for the public Home Assistant add-on.

Public repository:

```text
https://github.com/Haussie-au/hausie_app_homeassistant
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
Hausie App
```

## Release Flow

1. Update the add-on code directly in `hausie/`.
2. Bump `hausie/config.yaml` version.
3. Commit and push to `Haussie-au/hausie_app_homeassistant`.
4. Home Assistant users get the update from `hausie/config.yaml`.

## Development vs Production

Local testing:

```text
Use the internal deploy tooling from the sibling `hausie` repository
```

Production:

```text
Home Assistant add-on repository
```

This repository stays public-ready. SSH deploy scripts and local-only tooling live in the internal `hausie` repository.
