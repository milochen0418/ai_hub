#!/bin/sh
# Sets up the AI Hub's channel for Claude Code on this computer, for your own development:
#
#   claude_channel/setup.sh <hub url> <connection token from the hub's page>
#
# Everything goes to AI_HUB_SESSION_DIR (default ~/.local/share/ai_hub/claude-session): the
# channel and its npm packages, mcp.json with the token (readable only by you), and start.sh.
set -eu

HUB="${1:?usage: setup.sh <hub url> <token>}"
TOKEN="${2:?usage: setup.sh <hub url> <token>}"
HUB="${HUB%/}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${AI_HUB_SESSION_DIR:-$HOME/.local/share/ai_hub/claude-session}"

command -v node >/dev/null || { echo "Node.js is needed (22.15 or later): https://nodejs.org" >&2; exit 1; }
# --use-system-ca: trust what the system trusts, such as the re-ddns CA in the macOS keychain.
node --use-system-ca -e 0 2>/dev/null || { echo "Node.js $(node --version) is too old: 22.15 or later is needed" >&2; exit 1; }
NODE="$(node -p process.execPath)" # the binary itself: version managers' per-shell links don't last

mkdir -p "$DIR"
chmod 700 "$DIR"
cp "$HERE/bridge.mjs" "$HERE/package.json" "$HERE/package-lock.json" "$HERE/start.sh" "$DIR/"
chmod +x "$DIR/start.sh"
(cd "$DIR" && npm ci --omit=dev --no-audit --no-fund --loglevel=error)

umask 077
cat > "$DIR/mcp.json" <<EOF
{
  "mcpServers": {
    "apps": {
      "type": "http",
      "url": "$HUB/api/mcp",
      "headers": { "Authorization": "Bearer $TOKEN" }
    },
    "ai-hub": {
      "command": "$NODE",
      "args": ["--use-system-ca", "$DIR/bridge.mjs"],
      "env": { "AI_HUB_URL": "$HUB", "AI_HUB_TOKEN": "$TOKEN" }
    }
  }
}
EOF

echo "Ready. Start Claude Code with the hub's channel (each time you want it):"
echo "  $DIR/start.sh"
