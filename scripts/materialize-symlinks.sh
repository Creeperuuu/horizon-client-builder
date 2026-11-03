#!/usr/bin/env bash
# Materializes tracked git symlinks in a worktree into real files.
#
# actions/checkout can leave tracked symlinks in a state that confuses the
# mixin tooling and cargo (dangling links on Windows, mode-only entries after
# forced checkouts). For every tracked mode-120000 entry this replaces the
# link with a plain copy of whatever it points at, so the workspace works the
# same on Linux, Windows and macOS runners. Called once after checkout and
# again by scripts/probe_revisions.sh after every revision switch.
set -euo pipefail

DIR="${1:?usage: materialize-symlinks.sh <git-worktree>}"
cd "$DIR"

git ls-files -s | awk '$1 == 120000 {print $4}' | while IFS= read -r link; do
  [ -n "$link" ] || continue
  target=$(git cat-file blob "HEAD:$link")
  rm -f "$link"
  cp -L "$target" "$link"
done
