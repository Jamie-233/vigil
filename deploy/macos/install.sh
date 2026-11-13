#!/usr/bin/env bash
# Install Vigil and start it in the menu bar. Nothing is added to login items;
# "Launch at Login" is a toggle in the menu.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOME_DIR="$HOME/.vigil"

# Use $PYTHON if set, otherwise the newest python3.x found on PATH or in Homebrew.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 \
           /opt/homebrew/bin/python3.1{4,3,2,1,0} /usr/local/bin/python3.1{4,3,2,1,0} python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "Python 3.10+ is required (brew install python)." >&2
  exit 1
fi

mkdir -p "$HOME_DIR"
[ -d "$HOME_DIR/venv" ] || "$PY" -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/venv/bin/pip" install -q "$REPO[macos]"

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

# Older versions installed a background service; remove it.
launchctl bootout "gui/$(id -u)/io.github.jamie-233.vigil" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/io.github.jamie-233.vigil.plist"

pkill -f "vigil.cli menubar" 2>/dev/null || true
pkill -f "vigil.cli run" 2>/dev/null || true
sleep 1
nohup "$HOME_DIR/venv/bin/python" -m vigil.cli menubar -c "$HOME_DIR/vigil.yml" \
  >>"$HOME_DIR/menubar.log" 2>&1 &
disown

echo
echo "Vigil is in the menu bar. Dashboard: http://127.0.0.1:8080"
