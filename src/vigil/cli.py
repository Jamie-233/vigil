"""Entry point: ``vigil run`` for the daemon, ``vigil check`` for a one-shot sweep.

``check`` exits non-zero when anything is down, so the same config doubles as
a post-change smoke test: run it right after a maintenance window and the exit
code tells you whether to roll back.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from aiohttp import web

from .alerts import Notifier
from .config import ConfigError, load_config
from .scheduler import Scheduler
from .server import build_app
from .store import Store

log = logging.getLogger("vigil")


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    store = Store(config.database)
    notifier = Notifier(config.webhooks)
    scheduler = Scheduler(config, store, on_transition=notifier.notify)

    app = build_app(scheduler, store)
    runner = web.AppRunner(app)
    await runner.setup()
    host, _, port = config.listen.rpartition(":")
    await web.TCPSite(runner, host or "0.0.0.0", int(port)).start()

    await notifier.start()
    scheduler.start()
    log.info("monitoring %d targets, dashboard on http://%s", len(config.targets), config.listen)

    async def prune_forever():
        while True:
            removed = await asyncio.to_thread(store.prune, config.retention_days)
            if removed:
                log.info("pruned %d samples older than %d days", removed, config.retention_days)
            await asyncio.sleep(3600)

    pruner = asyncio.create_task(prune_forever())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    pruner.cancel()
    await scheduler.stop()
    await notifier.stop()
    await runner.cleanup()
    store.close()


async def _check(config_path: str) -> int:
    config = load_config(config_path)
    scheduler = Scheduler(config, Store(":memory:"))
    results = await asyncio.gather(*(scheduler.run_once(t) for t in config.targets))
    width = max(len(t.name) for t in config.targets)
    failed = 0
    for t, r in zip(config.targets, results):
        mark = "\033[32m✓\033[0m" if r.ok else "\033[31m✗\033[0m"
        lat = f"{r.latency_ms:>8.1f} ms" if r.latency_ms is not None else " " * 11
        print(f"{mark} {t.name:<{width}}  {t.type:<4} {lat}  {r.detail}")
        failed += not r.ok
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vigil", description="Self-hosted network monitoring")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("run", "start the monitor and dashboard"), ("check", "probe every target once"),
                            ("menubar", "macOS: run the monitor from the menu bar")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-c", "--config", default="vigil.yml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        if args.cmd == "run":
            asyncio.run(_run(args.config))
        elif args.cmd == "menubar":
            if sys.platform != "darwin":
                sys.exit("vigil menubar only runs on macOS")
            try:
                from .menubar import run as run_menubar
            except ImportError:
                sys.exit("menu bar support is not installed: pip install 'vigil[macos]'")
            run_menubar(args.config)
        else:
            sys.exit(asyncio.run(_check(args.config)))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError as exc:
        print(f"config not found: {exc.filename}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
