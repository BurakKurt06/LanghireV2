#!/usr/bin/env bash
# Build & Release: decide whether to build the installer and whether to publish a release.
# Usage: plan-release.sh <event> <ref_name> <release_input>. Writes to $GITHUB_OUTPUT.
# Needs GH_TOKEN and GITHUB_REPOSITORY (gh), run from the checked-out commit.
set -euo pipefail
event="$1"; ref_name="${2:-}"; release_input="${3:-}"
version=$(jq -r .version src-tauri/tauri.conf.json)
tag="v$version"
sha=$(git rev-parse HEAD)
build=true
release=false
case "$event" in
  workflow_run)
    # Tests passed on a push to main: ship only when this version has no release yet.
    if gh release view "$tag" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
      echo "Release $tag already exists: the version wasn't raised, so nothing is built."
      build=false
    else
      release=true
    fi ;;
  push)
    if [ "$ref_name" != "$tag" ]; then
      echo "::error::Tag $ref_name doesn't match the app's version $version (src-tauri/tauri.conf.json)."
      exit 1
    fi
    release=true ;;
  *)
    [ "$release_input" = "true" ] && release=true ;;
esac
{
  echo "sha=$sha"; echo "version=$version"; echo "tag=$tag"
  echo "build=$build"; echo "release=$release"
} >> "$GITHUB_OUTPUT"
echo "Version $version ($sha): build=$build release=$release"
