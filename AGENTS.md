# AGENTS.md

ha-git-sync deploys a Home Assistant configuration from Git. [README.md](./README.md) explains how people use it.

## Scope

The sidecar has one job: put the tree `promote` built into `/config`, reload or restart Home Assistant, and report what
failed. Keep it that simple. Weigh every new feature or edge case against that job, and prefer removing a case to
handling it. Checks belong in the `check` action, before anything reaches the sidecar.

## Layout

```text
Dockerfile          The image: git-sync plus the files in image/, with git-sync defaults set through ENV
image/hook          git-sync's exechook: links the checkout into /config, installs, reloads or restarts, notifies
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
- Each deploy commit carries a `Source-Commit:` trailer. The hook only installs and reloads commits with one. Keep this
  check: it stands in for branch protection, which GitHub Free does not offer private repositories.
- The deploy tree is the repository without `.github`, plus `custom_components/` and `www/community/`.
- `ha_git_sync/check.py` `layout()` mirrors how the hook links the checkout into `/config`. `test_sidecar.py`
  compares the two.

The image is public and used by others: keep its settings few, and treat a change to a default, a path or the hook's
behavior as a breaking change.

## Rules

- Run `mise run test` and `mise run lint` before committing. Run `mise run e2e` when the image or hook changes; it
  needs Docker.
- Pin actions by commit SHA with a version comment, and images by tag and digest. Renovate updates them; do not bump
  versions by hand unless asked.
- No explanatory comments in workflows, action files or the Dockerfile.
- Commit messages are conventional commits with a scope, such as `fix(hook): retry when Home Assistant is down`.
- Commit types decide releases: `feat` and `fix` create one, so use another type, such as `test`, `ci` or `docs`, for
  changes that reach neither the image nor the actions. Mark a breaking change with `!` and a `BREAKING CHANGE:` footer
  that tells users what to change; release-please puts it in the release notes.
- `default.json` reaches users from `main` as soon as it merges, not through a release.
- Releases come from merging the pull request release-please keeps open. Do not push tags by hand: the release workflow
  adds the image to the GitHub release that release-please creates.
- Do not edit `CHANGELOG.md` or `.release-please-manifest.json`. Release-please maintains them.
