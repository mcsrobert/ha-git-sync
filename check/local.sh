#!/usr/bin/env bash
# Run the check action locally: check/local.sh CONFIG_REPOSITORY HOME_ASSISTANT_VERSION [SECRETS_FILE]
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
source=$(cd "${1:?Expected the configuration repository}" && pwd)
version=${2:?Expected the Home Assistant release, such as 2026.10.4}
secrets=()
if [[ -n ${3:-} ]]; then
  secrets=(--secrets "$(cd "$(dirname "$3")" && pwd)/$(basename "$3")")
fi

build_token=${GITHUB_TOKEN:-${GH_TOKEN:-$(gh auth token 2> /dev/null || true)}}
export -n build_token
unset GITHUB_TOKEN GH_TOKEN
venv=$root/.venv/home-assistant-$version
if [[ ! -f $venv/.ready ]]; then
  python_version=$("$root/check/resolve-python.sh" "$version")
  echo "Installing Home Assistant ${version} on Python ${python_version} into ${venv}"
  if [[ ! -x $venv/bin/python ]]; then
    uv venv --quiet --python "$python_version" "$venv"
  fi
  uv pip install --quiet --python "$venv/bin/python" "homeassistant==${version}"
  read -ra requirements < <("$venv/bin/python" -P -c \
    'from homeassistant.scripts.check_config import REQUIREMENTS; print(*REQUIREMENTS)')
  uv pip install --quiet --python "$venv/bin/python" "${requirements[@]}"
  : > "$venv/.ready"
fi

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
export PYTHONPATH=$root
GITHUB_TOKEN=$build_token \
  "$venv/bin/python" -P -m ha_git_sync build "$source" "$out/deploy"
"$venv/bin/python" -P -m ha_git_sync check "$out/deploy" "${secrets[@]}"
