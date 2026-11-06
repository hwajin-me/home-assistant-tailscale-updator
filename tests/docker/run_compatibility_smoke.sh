#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image_tag="tailscale-updator-hass-smoke:${GITHUB_SHA:-local}"

docker build --pull --tag "$image_tag" --file "$repo_dir/tests/docker/Dockerfile" "$repo_dir"
docker run --rm "$image_tag"
