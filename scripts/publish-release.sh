#!/usr/bin/env bash
# Publish release <tag> with the installer files in <dir>; a release that already exists gets
# its files replaced (a manual re-run for the same version). Never moves an existing tag.
# Usage: publish-release.sh <tag> <version> <sha> <dir>. Needs GH_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail
tag="$1"; version="$2"; sha="$3"; dir="$4"
shopt -s nullglob
files=("$dir"/*.exe)
[ "${#files[@]}" -ge 1 ] || { echo "::error::No installer files in $dir."; exit 1; }
repo="$GITHUB_REPOSITORY"
if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  gh release upload "$tag" "${files[@]}" --clobber --repo "$repo"
else
  notes=$(cat <<NOTES
## Download

**[LangHireV2-setup.exe](https://github.com/$repo/releases/download/$tag/LangHireV2-setup.exe)** (Windows 10/11, x64). Both files under Assets are the same installer.

Before running it, right-click the file → **Properties**, tick **Unblock** and click **OK** (the installer isn't code-signed). Installing over an older version keeps your settings, profile and jobs.

### Prerequisites
- Windows 10 or 11 (x64). Python is bundled; no separate install.
- The app downloads Chromium (~150 MB) on first launch.
- An AI provider: an API key (OpenRouter, OpenAI, Anthropic, Gemini, Ollama or Bedrock) or a ChatGPT Plus/Pro plan.

Built from commit $sha.
NOTES
)
  gh release create "$tag" "${files[@]}" --repo "$repo" --target "$sha" \
    --title "LangHireV2 v$version" --notes "$notes" --latest
fi
echo "Published $tag: $(basename -a "${files[@]}" | tr '\n' ' ')"
