"""macOS menu bar front end: ``vigil menubar``.

Starts the monitor as a child process and shows its state in the menu bar,
so there is nothing to keep open and no service installed behind the user's
back. Starting at login is an explicit toggle in the menu, off by default.

The menu bar reads the same HTTP API as the dashboard instead of sharing the
monitor's process, so a hung UI can never stall probing and vice versa.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from .config import load_config

LABEL = "io.github.jamie-233.vigil.menubar"
AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
POLL_SECONDS = 10


def _title(targets: list[dict]) -> str:
    if not targets:
        return "○ Vigil"
    control = [t for t in targets if t["labels"].get("role") == "control"]
    others = [t for t in targets if t["labels"].get("role") != "control"]
    # If every control target is down the problem is our own uplink, and the
    # other failures say nothing about the services.
    if control and all(t["health"] == "down" for t in control):
        return "▲ offline"
    down = [t for t in others if t["health"] == "down"]
    if down:
        return f"▲ {len(down)} down"
    up = sum(t["health"] == "up" for t in others)
    return f"● {up}/{len(others)}" if up == len(others) else f"○ {up}/{len(others)}"


def _line(t: dict) -> str:
    mark = {"up": "✓", "down": "✗"}.get(t["health"], "·")
    latency = f"{t['latency_ms']:.0f} ms" if t.get("latency_ms") is not None else t.get("detail") or "waiting"
    return f"{mark}  {t['name']}    {latency}"


def run(config_path: str) -> None:
    import rumps

    config_file = Path(config_path).expanduser().resolve()
    config = load_config(config_file)
    host, _, port = config.listen.rpartition(":")
    base = f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{port}"
    log = open(config_file.parent / "vigil.log", "ab")

    class App(rumps.App):
        def __init__(self) -> None:
            super().__init__("Vigil", title="○ Vigil", quit_button=None)
            try:  # no Dock icon: this is a menu bar utility
                from AppKit import NSApplication
                NSApplication.sharedApplication().setActivationPolicy_(1)
            except Exception:
                pass
            self.proc: subprocess.Popen | None = None
            self.items = {t.name: rumps.MenuItem(t.name) for t in config.targets}
            self.login = rumps.MenuItem("Launch at Login", callback=self.toggle_login)
            self.login.state = int(AGENT.exists())
            self.menu = [
                *self.items.values(), None,
                rumps.MenuItem("Open Dashboard", callback=lambda _: webbrowser.open(base)),
                self.login, None,
                rumps.MenuItem("Quit Vigil", callback=self.quit),
            ]
            self.start_monitor()
            rumps.Timer(self.refresh, POLL_SECONDS).start()

        def start_monitor(self) -> None:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "vigil.cli", "run", "-c", str(config_file)],
                cwd=config_file.parent, stdout=log, stderr=log,
            )

        def refresh(self, _) -> None:
            if self.proc and self.proc.poll() is not None:
                self.start_monitor()  # crashed: restart, the next poll will show it
            try:
                with urllib.request.urlopen(f"{base}/api/targets?range=1h", timeout=3) as r:
                    targets = json.load(r)
            except OSError:
                self.title = "○ Vigil"
                return
            self.title = _title(targets)
            for t in targets:
                if t["name"] in self.items:
                    self.items[t["name"]].title = _line(t)

        def toggle_login(self, item) -> None:
            if AGENT.exists():
                AGENT.unlink()
            else:
                AGENT.parent.mkdir(parents=True, exist_ok=True)
                AGENT.write_bytes(plistlib.dumps({
                    "Label": LABEL,
                    "ProgramArguments": [sys.executable, "-m", "vigil.cli", "menubar", "-c", str(config_file)],
                    "RunAtLoad": True,
                }))
            item.state = int(AGENT.exists())

        def quit(self, _) -> None:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            rumps.quit_application()

    App().run()
