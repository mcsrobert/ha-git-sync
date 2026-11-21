#!/usr/bin/env bash
# Commit a checked deploy tree onto the deployed branch: promote/promote.sh DEPLOY_DIR SOURCE_COMMIT
set -euo pipefail

deploy=$(cd "${1:?Expected the deploy tree}" && pwd)
if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
  echo "::error::promote needs the full history; check out with fetch-depth: 0" >&2
  exit 1
fi
source_commit=$(git rev-parse --verify "${2:?Expected the source commit}^{commit}")
remote=${PROMOTE_REMOTE:-origin}
branch=deployed
ref=refs/heads/$branch

built_from() {
  git log -1 --format='%(trailers:key=Source-Commit,valueonly)' "$1" | head -n 1
}

tip=
base=
if git ls-remote --exit-code --heads "$remote" "$branch" > /dev/null; then
  git fetch --quiet "$remote" "+$ref:refs/remotes/$remote/$branch"
  tip=$(git rev-parse "refs/remotes/$remote/$branch")
  while read -r commit; do
    if [[ -n $(built_from "$commit") ]]; then
      base=$commit
      break
    fi
  done < <(git rev-list --first-parent "$tip")
fi

target=
if [[ -n $base ]] && git merge-base --is-ancestor "$source_commit" "$(built_from "$base")" 2> /dev/null; then
  echo "$branch already contains $source_commit"
  target=$base
else
  index=$(mktemp -d)
  trap 'rm -rf "$index"' EXIT
  git_dir=$(git rev-parse --absolute-git-dir)
  GIT_INDEX_FILE=$index/index git -C "$deploy" --git-dir="$git_dir" --work-tree=. add --all --force .
  tree=$(GIT_INDEX_FILE=$index/index git --git-dir="$git_dir" write-tree)
  parents=()
  [[ -n $base ]] && parents=(-p "$base")
  target=$(git -c user.name='github-actions[bot]' \
    -c user.email='41898299+github-actions[bot]@users.noreply.github.com' \
    commit-tree "$tree" "${parents[@]}" \
    -m "Deploy ${source_commit:0:7}: $(git log -1 --format=%s "$source_commit")" \
    -m "Source-Commit: $source_commit")
fi

if [[ $target == "$tip" ]]; then
  echo "$branch is up to date at $tip"
  exit 0
fi
if [[ -n $tip && $tip != "$base" ]]; then
  echo "::warning::Replacing $branch at $tip, which CI did not build, with a build on ${base:-a new root}"
fi
git push --quiet --force-with-lease="$ref:$tip" "$remote" "$target:$ref"
echo "$branch is now $target"
