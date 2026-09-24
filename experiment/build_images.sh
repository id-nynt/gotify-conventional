#!/usr/bin/env bash
# Package the actual Gotify source/UI; no registry publication or deployment.
set -euo pipefail
if [ -s "$HOME/.nvm/nvm.sh" ]; then source "$HOME/.nvm/nvm.sh"; fi
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
source_sha=74b75e931e61e44b72eb9cfeb8489e61f60b5ace
git diff --exit-code "$source_sha" -- . ':!experiment' ':!.github/workflows' ':!bdi-cicd-framework'
test -f ui/build/index.html || { echo 'Build the UI with make build-js first' >&2; exit 1; }
builder_version=$(tr -d '\r\n' < GO_VERSION)
mkdir -p experiment/build
build_date=$(date -u +%Y-%m-%dT%H:%M:%SZ)
context=$(mktemp -d)
git archive "$source_sha" | tar -x -C "$context"
mkdir -p "$context/ui/build"
cp -r ui/build/. "$context/ui/build/"
registry=${GOTIFY_REGISTRY:-}
if [ -n "$registry" ] && [[ ! "$registry" =~ ^ghcr\.io/id-nynt/gotify-(conventional|bdi)-experiment$ ]]; then
  echo 'Only experiment-owned registry namespaces are allowed' >&2; exit 1
fi
for release in v1 v2; do
  tag="gotify-study:$source_sha-$release"
  options=(--load)
  if [ -n "$registry" ]; then
    tag="$registry:${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}-$release"
    options=(--push)
  fi
  docker buildx build --progress plain --provenance=false --platform linux/amd64 "${options[@]}" \
    --metadata-file "experiment/build/$release-metadata.json" -f "$context/docker/Dockerfile" \
    --build-arg GO_VERSION="$builder_version" \
    --build-arg LD_FLAGS="-w -s -X main.Version=experiment-$release -X main.Commit=$source_sha -X main.BuildDate=$build_date -X main.Mode=prod" \
    --label "org.opencontainers.image.revision=$source_sha" \
    --label "experiment.release=$release" \
    -t "$tag" "$context"
done
python3 - "$source_sha" "$build_date" "$registry" "$context" <<'PY'
import json, subprocess, sys
sha, date, registry, context = sys.argv[1:]
images = {release: (registry + '@' + json.load(open('experiment/build/' + release + '-metadata.json'))['containerimage.digest'])
          if registry else subprocess.check_output(['docker', 'image', 'inspect',
          'gotify-study:' + sha + '-' + release, '--format', '{{.Id}}'], text=True).strip()
          for release in ('v1', 'v2')}
with open('experiment/build/images.json', 'w') as f:
    json.dump({'source_sha': sha, 'build_date': date, 'images': images, 'build_context': context}, f, indent=2)
print(json.dumps({'source_sha': sha, 'images': images}))
PY
