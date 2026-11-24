#!/bin/sh
set -eu
cd "$(dirname "$0")"
. ./dependencies.lock
archive=${1:?Pass the absolute path to the pinned Yogurt source archive}
printf '%s  %s\n' "$YOGURT_SOURCE_SHA256" "$archive" | shasum -a 256 --check
source_dir=$(mktemp -d)
trap 'rm -rf "$source_dir"' EXIT
tar -xf "$archive" -C "$source_dir" --strip-components=1
node "$source_dir/bin/yogurt.mjs" add button --target native
mkdir -p assets
cp "$source_dir/design/assets/projects/sourdough.svg" assets/
cp "$source_dir/design/assets/fonts/rethink-sans/OFL.txt" assets/rethink-sans-OFL.txt
