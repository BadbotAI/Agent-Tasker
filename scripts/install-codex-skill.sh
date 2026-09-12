#!/usr/bin/env bash
# Install (or refresh) the agenttasker skill for OpenAI Codex.
# Codex requires a real file (it does not follow symlinks) and loads
# skills at session start, so restart Codex after installing.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${CODEX_HOME:-$HOME/.codex}/skills/agenttasker"

mkdir -p "$dest"
cp "$repo/SKILL.md" "$dest/SKILL.md"
echo "installed skill -> $dest/SKILL.md"
echo "restart Codex, then invoke with \$agenttasker (or match its description)"
