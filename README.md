# ha-git-sync

Deploy a Home Assistant configuration from a Git repository. Home Assistant reloads each change without a restart.

```text
pull request ─> check
main ─────────> check ─> promote ─> deployed ─> sidecar ─> /config ─> reload_all
```

- **`check`**, a GitHub Action, scans for leaked secrets, downloads the custom integrations and cards pinned in
  `hacs.yaml`, and runs Home Assistant's `check_config` on the result.
- **`promote`**, a GitHub Action, commits the tree `check` built on `main` to the `deployed` branch.
- **The sidecar** runs next to Home Assistant. It is [git-sync](https://github.com/kubernetes/git-sync) following
  `deployed`, plus a hook that puts the checkout in `/config` and calls `homeassistant.reload_all`. It never restarts
  Home Assistant; when integrations or cards change, it asks you to.

## What gets written where

| Where | Written by | What |
| --- | --- | --- |
| `main` | you | Your configuration and `hacs.yaml`. No `secrets.yaml`, and no downloaded integrations or cards. |
| `deployed` | `promote` | `main` without `.github/`, plus `custom_components/<domain>/` and `www/community/<repository>/` from `hacs.yaml`. Each commit names its `main` commit in a `Source-Commit:` trailer. |
| `/config/.git-sync/` | sidecar | git-sync's clone, `current` (a link to the checked-out commit) and the hook's state. |
| `/config/<name>` | sidecar | A link to `.git-sync/current/<name>` for each top-level file and folder in `deployed`, except hidden ones, `custom_components` and `www`. |
| `/config/custom_components/`<br>`/config/www/community/` | sidecar | Copies from `deployed`, replaced whenever they change. |
| `/config/<name>.pre-git` | sidecar | Anything the sidecar replaced: a file or folder where a link goes, a `custom_components/` or `www/community/` it did not create, such as HACS's, and on the first deploy `automations.yaml`, `scripts.yaml` and `scenes.yaml`. |

The rest of `/config` stays Home Assistant's: `secrets.yaml`, `.storage/`, the database, logs and the rest of `www/`.
The UI editors still save `automations.yaml`, `scripts.yaml` and `scenes.yaml` there, but your configuration does not
load them. The hook raises a notification when one appears, so you can move the change into Git and delete the file.

The hook also raises a notification when a reload fails, and when `deployed` points at a commit `promote` did not make.
It does not install or reload such a commit, and the next run on `main` restores `deployed`.

## Your repository

Lay it out the way Home Assistant expects `/config`, with `configuration.yaml` at the root, so includes such as
`!include_dir_merge_list automations` work as usual. `check` fails when the repository contains:

- `secrets.yaml`. Keep it in `/config`; `check` validates with placeholder values.
- `automations.yaml`, `scripts.yaml` or `scenes.yaml` at the root, which belong to the UI editors. Use folders such as
  `automations/` instead.
- `www/`. Pin cards in `hacs.yaml`.
- `blueprints/` without a blueprint in each of `automation/`, `script/` and `template/`, because Home Assistant would
  write its defaults into the checkout.
- Symlinks or submodules.

Commit your own integrations to `custom_components/`. Pin the rest in `hacs.yaml`, either as a release asset, which
`check` verifies against the SHA-256 digest GitHub publishes, or as the commit a tag points to:

```yaml
integrations:
  - repository: amitfin/retry
    version: v4.2.0
    asset: retry.zip

cards:
  - repository: mlamberts78/weather-chart-card
    version: V2.4.11
    commit: 71be08feeced42427ac69954168ff4ba05990dfc
    path: dist/weather-chart-card.js
```

Load cards from `/local/community/<repository>/<file>`.

## Setup

Each release publishes the image and the actions under one version. Replace `X.Y.Z` below with the
[latest release](https://github.com/mcsrobert/ha-git-sync/releases/latest); its notes list the image digest.

### 1. Workflow

Add `.github/workflows/deploy.yaml` to your repository:

```yaml
name: Deploy

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: '23 4 * * *'
  workflow_dispatch:

permissions: {}

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: mcsrobert/ha-git-sync/check@vX.Y.Z
        with:
          home-assistant-version: 2026.10.4

  promote:
    if: github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
    needs: check
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: mcsrobert/ha-git-sync/promote@vX.Y.Z
```

Set `home-assistant-version` to the release you run. The optional `secrets` input names a YAML file with typed values
for secrets that must not be strings, such as `home_latitude: 52.3`.

With Renovate, extend the preset to keep `hacs.yaml` and `home-assistant-version` up to date:

```json
{ "extends": ["config:recommended", "github>mcsrobert/ha-git-sync"] }
```

### 2. Sidecar

The sidecar mounts Home Assistant's `/config` volume and reads two files from `/etc/git-secret`:

- `ssh`: a read-only deploy key for the repository. For HTTPS, use git-sync's `GITSYNC_USERNAME` and
  `GITSYNC_PASSWORD_FILE` instead.
- `ha-token`: a long-lived access token of an administrator, which `reload_all` requires.

On Kubernetes, add it to the Home Assistant pod as a native sidecar, with a Secret at `/etc/git-secret` and an
`emptyDir` at `/tmp`. Its startup probe holds Home Assistant back until the first deploy is in place. The image runs as
user 65533, so set the pod's `fsGroup` to Home Assistant's group.

```yaml
initContainers:
  - name: git-sync
    image: ghcr.io/mcsrobert/ha-git-sync:X.Y.Z
    restartPolicy: Always
    env:
      - name: GITSYNC_REPO
        value: git@github.com:you/home-assistant-config.git
    startupProbe:
      exec: { command: [/ha-git-sync/ready] }
      periodSeconds: 5
      failureThreshold: 120
    securityContext:
      readOnlyRootFilesystem: true
    volumeMounts:
      - { name: config, mountPath: /config }
      - { name: git-secret, mountPath: /etc/git-secret, readOnly: true }
      - { name: tmp, mountPath: /tmp }
```

With Docker Compose, run it as Home Assistant's user, which is root for the official image. For any other user, also
set `GITSYNC_ADD_USER: "true"`, because SSH needs the user in `/etc/passwd`.

```yaml
services:
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:2026.10.4
    network_mode: host
    volumes:
      - ./config:/config
    depends_on:
      git-sync:
        condition: service_healthy
  git-sync:
    image: ghcr.io/mcsrobert/ha-git-sync:X.Y.Z
    user: '0:0'
    network_mode: host
    environment:
      GITSYNC_REPO: git@github.com:you/home-assistant-config.git
    volumes:
      - ./config:/config
      - ./git-secret:/etc/git-secret:ro
```

`GITSYNC_REPO` is the only required setting. `HA_URL` (default `http://localhost:8123`) and `HA_TOKEN_FILE` (default
`/etc/git-secret/ha-token`) tell the hook where Home Assistant and its token are. Other
[git-sync settings](https://github.com/kubernetes/git-sync#parameters) work too, such as `GITSYNC_PERIOD` (default
`1m`), but keep the image's `GITSYNC_ROOT`, `GITSYNC_LINK`, `GITSYNC_REF` and `GITSYNC_EXECHOOK_COMMAND`. The image
trusts GitHub's SSH host keys; for another Git host, mount its keys and set `GITSYNC_SSH_KNOWN_HOSTS_FILE`.

## Moving from HACS and the UI editors

1. Copy the automations, scripts and scenes the UI made into folders in the repository, for example from
   `automations.yaml` to `automations/imported.yaml`.
2. Pin your HACS integrations and cards in `hacs.yaml`, and remove HACS.
3. Change dashboard resources from `/hacsfiles/<repository>/<file>` to `/local/community/<repository>/<file>`.
4. Start the sidecar. Once Home Assistant runs on the new configuration, delete the `.pre-git` files you no longer need.

## Recovery

- Revert the change on `main`. CI checks and promotes the revert.
- To go back without waiting for CI, push an earlier deploy: `git push --force origin <commit>:deployed`. It keeps its
  `Source-Commit:` trailer, so the hook installs and reloads it. The next run on `main`, at the latest the nightly
  one, deploys `main` again.

## Development

```sh
mise run test   # unit tests
mise run e2e    # builds the image and runs it against a fake Home Assistant in Docker
mise run lint
mise run check-config -- ../home-assistant-config 2026.10.4  # runs the check action locally
```

[release-please](https://github.com/googleapis/release-please) keeps a release pull request open from the conventional
commits on `main`: `feat` and `fix` create a release, and `!` marks a breaking change. Merging it tags `vX.Y.Z`, and the
tag builds, tests and publishes the image, then adds it to the GitHub release.
