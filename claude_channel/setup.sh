#!/bin/sh
# Sets up the AI Hub's channel for Claude Code on this computer, for your own development:
#
#   claude_channel/setup.sh <hub url> <connection token from the hub's page>
#
# Everything goes to AI_HUB_SESSION_DIR (default ~/.local/share/ai_hub/claude-session): the
# channel and its npm packages, mcp.json with the token (readable only by you), the session's
# hooks in .claude/settings.local.json (the token for them in .claude/hub.headers), and start.sh.
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

# The session's hooks tell the hub what it does besides the apps' tools (its web searches, the
# pages it reads, what it writes, when it waits for you, when its turn ends), for the assistant
# to show while it works. They are this folder's project settings, so they apply only to the
# Claude Code that start.sh runs here. Each pipes its event to curl: Claude Code's own HTTP
# hooks may reach only loopback and public addresses, not a hub on the local network. A post
# that fails changes nothing in the session (|| true: not even a hook error).
mkdir -p "$DIR/.claude"
printf 'Authorization: Bearer %s\n' "$TOKEN" > "$DIR/.claude/hub.headers"
REPORT="curl -s -o /dev/null --connect-timeout 1 -m 2 -H @'$DIR/.claude/hub.headers' -H 'Content-Type: application/json' --data-binary @- '$HUB/api/session/activity' || true"
HOOK="{ \"type\": \"command\", \"command\": \"$REPORT\", \"timeout\": 3 }"
cat > "$DIR/.claude/settings.local.json" <<EOF
{
  "hooks": {
    "SessionStart": [{ "hooks": [$HOOK] }],
    "UserPromptSubmit": [{ "hooks": [$HOOK] }],
    "PreToolUse": [{ "matcher": "^(?!mcp__)", "hooks": [$HOOK] }],
    "PostToolUse": [{ "matcher": "^(?!mcp__)", "hooks": [$HOOK] }],
    "PostToolUseFailure": [{ "matcher": "^(?!mcp__)", "hooks": [$HOOK] }],
    "MessageDisplay": [{ "hooks": [$HOOK] }],
    "Notification": [{ "hooks": [$HOOK] }],
    "PreCompact": [{ "hooks": [$HOOK] }],
    "PostCompact": [{ "hooks": [$HOOK] }],
    "Stop": [{ "hooks": [$HOOK] }],
    "StopFailure": [{ "hooks": [$HOOK] }],
    "SessionEnd": [{ "hooks": [$HOOK] }]
  }
}
EOF

echo "Ready. Start Claude Code with the hub's channel (each time you want it):"
echo "  $DIR/start.sh"
