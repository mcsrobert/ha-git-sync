# ha-git-sync

Deploy a Home Assistant configuration from a Git repository, without restarting Home Assistant.

ha-git-sync has two parts that share one contract:

- **GitHub Actions** for your configuration repository. `check` downloads the custom integrations and dashboard cards
  pinned in `hacs.yaml`, scans the history for leaked secrets, and runs Home Assistant's `check_config`. `promote`
  commits that checked tree to a `deployed` branch.
- **A container image** that runs next to Home Assistant. It is [git-sync](https://github.com/kubernetes/git-sync)
  plus a hook. git-sync follows `deployed`, and the hook links the files into `/config`, installs the integrations and
  cards, and calls `homeassistant.reload_all`.

```text
pull request ──> check ──> merge to main ──> check ──> promote ──> deployed branch
                                                                         │
                                       ha-git-sync sidecar (git-sync) <──┘
                                         │ links the checkout into /config
                                         │ installs custom_components/ and www/community/
                                         └─> POST /api/services/homeassistant/reload_all
```

Git is the only way to change the configuration. Home Assistant never writes to the repository, and nothing restarts
Home Assistant for you. When integrations change, the hook tells you to restart.

## Configuration repository

Keep the configuration the way Home Assistant expects it, with `configuration.yaml` at the root. The sidecar links
every top-level entry into `/config`, except hidden ones, so `!include automations/lights.yaml` works as usual.

`check` enforces these rules:

- No `secrets.yaml` anywhere. Keep it on the Home Assistant volume; `check` validates with placeholder values.
- No `automations.yaml`, `scripts.yaml` or `scenes.yaml` at the root. Home Assistant's UI editor writes those files
  in `/config`, so keep your configuration in other files, such as `automations/`. A file the UI saves later is not
  loaded, and the hook raises a notification about it.
- No `www/`. Pin dashboard cards in `hacs.yaml`.
- If you have `blueprints/`, it needs a blueprint in each of `automation/`, `script/` and `template/`. Otherwise Home
  Assistant tries to write its default blueprints into the read-only checkout.

Pin custom integrations and dashboard cards in `hacs.yaml`. Each entry is either a release asset, which `check`
verifies against the SHA-256 digest GitHub publishes, or a tagged commit:

```yaml
integrations:
  - repository: amitfin/retry
    version: v4.2.0
    asset: retry.zip
  - repository: custom-components/nordpool
    version: 0.0.18
    commit: 1eaa2490943bb41a2e322e9349ba2d7c6746a94b

cards:
  - repository: RomRider/apexcharts-card
    version: v2.2.3
    asset: apexcharts-card.js
  - repository: mlamberts78/weather-chart-card
    version: V2.4.11
    commit: 71be08feeced42427ac69954168ff4ba05990dfc
    path: dist/weather-chart-card.js
```

Integrations install into `custom_components/<domain>/`, and cards into `www/community/<repository>/`. Load cards from
`/local/community/<repository>/<file>`. You can also commit your own integrations to `custom_components/`.

### Workflow

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
      - uses: mcsrobert/ha-git-sync/check@v0.1.0
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
      - uses: mcsrobert/ha-git-sync/promote@v0.1.0
```

`check` takes two inputs:

| Input | |
| --- | --- |
| `home-assistant-version` | Required. The release that runs your configuration, such as `2026.10.4`. |
| `secrets` | Optional. A YAML file with typed values for secrets that must not be strings, such as `home_latitude: 52.3`. Every other secret gets a placeholder string. |

`promote` only moves `deployed` forward and commits only when the files change. Each commit records the main commit
it was built from in a `Source-Commit:` trailer; the hook only installs and reloads commits that have one. The nightly
run replaces commits on `deployed` that CI did not make.

### Renovate

The preset updates the pins in `hacs.yaml` and `home-assistant-version` in your workflows:

```json
{
  "extends": ["config:recommended", "github>mcsrobert/ha-git-sync"]
}
```

## Sidecar

### Kubernetes

Run the image as a native sidecar in the Home Assistant pod. Its startup probe holds Home Assistant back until the
first deploy is in place.

```yaml
spec:
  securityContext:
    fsGroup: 1000 # Home Assistant's group
  initContainers:
    - name: git-sync
      image: ghcr.io/mcsrobert/ha-git-sync:0.1.0
      restartPolicy: Always
      env:
        - name: GITSYNC_REPO
          value: git@github.com:you/home-assistant-config.git
      startupProbe:
        exec:
          command: [/ha-git-sync/ready]
        periodSeconds: 5
        failureThreshold: 120
      securityContext:
        readOnlyRootFilesystem: true
      volumeMounts:
        - { name: config, mountPath: /config }
        - { name: git-secret, mountPath: /etc/git-secret, readOnly: true }
        - { name: tmp, mountPath: /tmp }
  containers:
    - name: home-assistant
      volumeMounts:
        - { name: config, mountPath: /config }
  volumes:
    - { name: config, persistentVolumeClaim: { claimName: home-assistant-config } }
    - { name: git-secret, secret: { secretName: home-assistant-git-sync, defaultMode: 0440 } }
    - { name: tmp, emptyDir: {} }
```

The image runs as git-sync's user, 65533. With `fsGroup` set to Home Assistant's group it can write to `/config`, and
Home Assistant can read but not change what it installs.

### Docker Compose

Run it as the same user as Home Assistant, which is root for the official image:

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
    image: ghcr.io/mcsrobert/ha-git-sync:0.1.0
    user: '0:0'
    network_mode: host
    environment:
      GITSYNC_REPO: git@github.com:you/home-assistant-config.git
    volumes:
      - ./config:/config
      - ./git-secret:/etc/git-secret:ro
```

For any other user, also set `GITSYNC_ADD_USER: "true"`, because SSH needs the user in `/etc/passwd`.

### Settings

| Setting | Default | |
| --- | --- | --- |
| `GITSYNC_REPO` | | Your configuration repository. |
| `/etc/git-secret/ssh` | | A read-only deploy key for the repository. This is git-sync's `GITSYNC_SSH_KEY_FILE`. For HTTPS, use git-sync's `GITSYNC_USERNAME` and `GITSYNC_PASSWORD_FILE` instead. |
| `HA_TOKEN_FILE` | `/etc/git-secret/ha-token` | A long-lived access token of an administrator, which `reload_all` requires. |
| `HA_URL` | `http://localhost:8123` | Where the hook reaches Home Assistant. |

The image trusts GitHub's SSH host keys. For another Git host, mount its keys and set `GITSYNC_SSH_KNOWN_HOSTS_FILE`.
Other [git-sync settings](https://github.com/kubernetes/git-sync#parameters), such as `GITSYNC_PERIOD` (default `1m`),
work too. Keep the image's `GITSYNC_ROOT`, `GITSYNC_LINK`, `GITSYNC_REF` and `GITSYNC_EXECHOOK_COMMAND`.

## What the hook does

git-sync runs the hook after every sync and when it starts, and retries it until it succeeds.

1. It links each top-level entry of the checkout into `/config`. An existing file or folder in its place moves to
   `<name>.pre-git`. On the first deploy it also moves `automations.yaml`, `scripts.yaml` and `scenes.yaml` aside.
2. It replaces `custom_components/` and `www/community/` with the ones in the checkout. Folders it did not create, such
   as HACS's, move to `.pre-git` first.
3. Once Home Assistant runs, it calls `homeassistant.reload_all`, which first validates the configuration.

It raises a persistent notification when:

- the reload fails, with Home Assistant's answer;
- integrations or cards changed, so Home Assistant needs a restart;
- the UI saved `automations.yaml`, `scripts.yaml` or `scenes.yaml`, which Home Assistant does not load;
- `deployed` points at a commit CI did not build.

Its state is in `/config/.git-sync/`.

## Moving from HACS and the UI editors

1. Copy the automations, scripts and scenes the UI made into the repository, for example from `automations.yaml` to
   `automations/imported.yaml`. To keep scenes without loading them, name the file `scenes/imported.yaml.disable`.
2. Pin your HACS integrations and cards in `hacs.yaml`, and remove HACS.
3. Change dashboard resources from `/hacsfiles/<repository>/<file>` to `/local/community/<repository>/<file>`.
4. Start the sidecar. Once Home Assistant runs on the new configuration, delete the `.pre-git` files you no longer need.

## Recovery

- Revert the change on main. CI checks and promotes the revert.
- To go back without waiting for CI, push an earlier deploy: `git push --force origin <commit>:deployed`. Commits CI
  made keep their trailer, so the hook reloads them.

## Development

```sh
mise run test   # unit tests
mise run e2e    # builds the image and runs it against a fake Home Assistant in Docker
mise run lint
mise run check-config -- ../home-assistant-config 2026.10.4  # run the check action locally
```

Releases come from tags: `git tag v0.1.1 && git push origin v0.1.1` builds, tests and publishes the image, then creates
the GitHub release.
