#!/usr/bin/env bash
# Link claudekeeper into ~/.local/bin so it is on your PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_dir="${CLAUDEKEEPER_BIN_DIR:-$HOME/.local/bin}"
src="$here/bin/claudekeeper.js"

mkdir -p "$target_dir"
chmod +x "$src"
ln -sf "$src" "$target_dir/claudekeeper"

echo "Linked $target_dir/claudekeeper -> $src"

if ! command -v claudekeeper >/dev/null 2>&1; then
  echo
  echo "Note: $target_dir is not on your PATH yet."
  echo "Add this to your shell profile:"
  echo "  export PATH=\"$target_dir:\$PATH\""
fi

if ! command -v claude >/dev/null 2>&1; then
  echo
  echo "Note: the claude CLI was not found on your PATH."
  echo "claudekeeper needs Claude Code installed to run."
fi
