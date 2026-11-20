# AGENTS.md

ha-git-sync deploys a Home Assistant configuration from Git. [README.md](./README.md) explains how people use it.

## Layout

```text
Dockerfile          The image: git-sync plus the files in image/, with git-sync defaults set through ENV
image/hook          git-sync's exechook: links the checkout into /config, installs, reloads, notifies
image/ready         Startup probe and Docker health check
check/action.yaml   Composite action: gitleaks, build the deploy tree, check_config, upload it
promote/action.yaml Composite action: download the deploy tree and run promote.sh
ha_git_sync/        Python used by the check action: build (hacs.yaml downloads) and check (check_config)
default.json        Renovate preset for configuration repositories
tests/              Unit tests, and test_sidecar.py, which runs the image in Docker
```

## The contract

The actions and the image change together, so keep them consistent in one change:

- `promote` writes the `deployed` branch; the image follows it (`GITSYNC_REF`).
- Each deploy commit carries a `Source-Commit:` trailer. The hook only installs and reloads commits with one.
- The deploy tree is the repository without `.github`, plus `custom_components/` and `www/community/`.
- `ha_git_sync/check.py` `layout()` mirrors how the hook links the checkout into `/config`. `test_sidecar.py`
  compares the two.

The image is public and used by others: keep its settings few, and treat a change to a default, a path or the hook's
behavior as a breaking change that needs a note in the release.

## Rules

- Run `mise run test` and `mise run lint` before committing. Run `mise run e2e` when the image or hook changes; it
  needs Docker.
- Pin actions by commit SHA with a version comment, and images by tag and digest. Renovate updates them; do not bump
  versions by hand unless asked.
- No explanatory comments in workflows, action files or the Dockerfile.
- Commit messages are conventional commits with a scope, such as `fix(hook): retry when Home Assistant is down`.
- Release by pushing a `vX.Y.Z` tag, only when asked.
