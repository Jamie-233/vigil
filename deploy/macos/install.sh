#!/usr/bin/env bash
# Install Vigil as a launchd agent on macOS: starts at login, restarts on crash.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOME_DIR="$HOME/.vigil"
LABEL="io.github.jamie-233.vigil"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10+ is required (brew install python)." >&2
  exit 1
fi

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents"
[ -d "$HOME_DIR/venv" ] || "$PY" -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/venv/bin/pip" install -q "$REPO"

# The config is local only and is never overwritten. Pass a file to use it:
#   bash install.sh path/to/vigil.yml
if [ "${1:-}" ]; then
  cp "$1" "$HOME_DIR/vigil.yml"
elif [ ! -f "$HOME_DIR/vigil.yml" ]; then
  cp "$REPO/deploy/targets.example.yml" "$HOME_DIR/vigil.yml"
  echo "Created $HOME_DIR/vigil.yml from the template."
  echo "Edit the targets in it, then run this script again."
  exit 0
fi

echo "One-shot check of every target:"
(cd "$HOME_DIR" && "$HOME_DIR/venv/bin/vigil" check -c vigil.yml) || true

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME_DIR/venv/bin/vigil</string>
    <string>run</string>
    <string>-c</string>
    <string>$HOME_DIR/vigil.yml</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/vigil.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/vigil.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Vigil is running: http://127.0.0.1:8080"
echo "Logs: $HOME_DIR/vigil.log"
echo "Keep the Mac awake: System Settings > Energy > Prevent automatic sleeping."
