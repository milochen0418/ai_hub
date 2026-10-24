#!/bin/sh
# Starts Claude Code with the AI Hub's channel (set up by setup.sh): what you type into the
# hub's assistant arrives in this session, and Claude answers there. The session has only the
# apps' tools and the channel's reply: no files, no shell. Extra arguments go to claude
# (e.g. --model sonnet).
DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ]; then # VS Code's Claude Code extension brings its own
  CLAUDE="$(ls -d "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | tail -1)"
fi
[ -n "$CLAUDE" ] && [ -x "$CLAUDE" ] || { echo "Claude Code isn't installed: https://code.claude.com/docs/en/setup" >&2; exit 1; }

cd "$DIR" || exit 1
exec "$CLAUDE" --dangerously-load-development-channels server:ai-hub \
  --strict-mcp-config --mcp-config "$DIR/mcp.json" \
  --tools "" --allowedTools "mcp__apps mcp__ai-hub" \
  --name "AI Hub" "$@"
