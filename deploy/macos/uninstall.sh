#!/usr/bin/env bash
# Stop and remove the launchd agent. Data in ~/.vigil is kept.
set -euo pipefail
LABEL="io.github.jamie-233.vigil"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Stopped. History is still in ~/.vigil/vigil.db"
