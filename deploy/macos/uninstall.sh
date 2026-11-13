#!/usr/bin/env bash
# Quit Vigil and remove its login item, if enabled. Data in ~/.vigil is kept.
set -euo pipefail
pkill -f "vigil.cli menubar" 2>/dev/null || true
pkill -f "vigil.cli run" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/io.github.jamie-233.vigil.menubar.plist"
echo "Stopped. History is still in ~/.vigil/vigil.db"
