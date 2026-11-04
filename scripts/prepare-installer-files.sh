#!/usr/bin/env bash
# Copy the built installer into <out_dir>, plus the same file as LangHireV2-setup.exe: the README
# links to releases/latest/download/LangHireV2-setup.exe, a name that never changes.
# Usage: prepare-installer-files.sh <nsis_dir> <out_dir>
set -euo pipefail
nsis_dir="$1"; out_dir="$2"
shopt -s nullglob
installers=("$nsis_dir"/*-setup.exe)
if [ "${#installers[@]}" -ne 1 ]; then
  echo "::error::Expected exactly one installer in $nsis_dir, found ${#installers[@]}."
  exit 1
fi
mkdir -p "$out_dir"
cp "${installers[0]}" "$out_dir/"
cp "${installers[0]}" "$out_dir/LangHireV2-setup.exe"
ls -l "$out_dir"
