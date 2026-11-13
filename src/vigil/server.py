"""HTTP API, live event stream and dashboard.

Live updates use Server-Sent Events rather than WebSockets. The data only
flows server → browser, SSE reconnects automatically with ``Last-Event-ID``,
and it passes through corporate proxies that break WebSocket upgrades — the
exact environment a network dashboard tends to be opened in.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from . import metrics
from .config import ConfigError
from .config_file import ConfigFile
from .scheduler import Scheduler
from .stats import summarize
from .store import Store

STATIC = Path(__file__).parent / "static"
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


class EventHub:
    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()

    def publish(self, payload: dict) -> None:
        for q in list(self.clients):
            # A browser tab in the background stops reading; drop its oldest
            # update instead of letting one slow client grow memory forever.
            if q.full():
                q.get_nowait()
            q.put_nowait(payload)


def _range(request: web.Request) -> float:
    key = request.query.get("range", "24h")
    if key not in RANGES:
        raise web.HTTPBadRequest(text=f"range must be one of {', '.join(RANGES)}")
    return time.time() - RANGES[key]


LOOPBACK = {"127.0.0.1", "::1"}


def _guard_write(request: web.Request) -> None:
    """Writes are for the person at this machine only.

    Loopback source alone is not enough: any web page the user has open can
    send requests to 127.0.0.1. Requiring a custom header forces a CORS
    preflight, which this server never answers, so cross-site pages can't
    write. Checking Host defeats DNS rebinding, where an attacker's domain
    resolves to 127.0.0.1 and passes as same-origin.
    """
    if request.remote not in LOOPBACK:
        raise web.HTTPForbidden(text="editing is only allowed from this machine")
    if request.headers.get("X-Vigil") != "1":
        raise web.HTTPForbidden(text="missing X-Vigil header")
    host = request.host.rsplit(":", 1)[0].strip("[]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise web.HTTPForbidden(text="bad Host")


def build_app(scheduler: Scheduler, store: Store, config_path: str | None = None) -> web.Application:
    hub = EventHub()
    config_file = ConfigFile(config_path) if config_path else None

    def targets() -> dict:
        return {t.name: t for t in scheduler.config.targets}

    def on_result(name, result, state):
        hub.publish({
            "target": name, "ok": result.ok, "latency_ms": result.latency_ms,
            "detail": result.detail, "health": state.health.value,
            "flapping": state.is_flapping, "ts": time.time(),
        })

    scheduler.listeners.append(on_result)

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def list_targets(request):
        since = _range(request)
        out = []
        for name, t in targets().items():
            samples = store.samples(name, since)
            summary = summarize([s.ok for s in samples], [s.latency_ms for s in samples],
                                t.availability_slo, t.latency_slo_ms)
            state = scheduler.states[name]
            latest = scheduler.latest.get(name)
            out.append({
                "name": name, "type": t.type, "address": t.address, "labels": t.labels,
                "interval": t.interval, "timeout": t.timeout, "options": t.options,
                "health": state.health.value, "since": state.since, "flapping": state.is_flapping,
                "detail": state.last_detail, "latency_ms": latest.latency_ms if latest else None,
                "slo": {"availability": t.availability_slo, "latency_ms": t.latency_slo_ms},
                "summary": summary.__dict__,
            })
        return web.json_response(out)

    async def target_series(request):
        name = request.match_info["name"]
        if name not in targets():
            raise web.HTTPNotFound(text=f"unknown target {name}")
        return web.json_response(store.series(name, _range(request)))

    async def list_events(request):
        return web.json_response(store.events(_range(request)))

    async def prom(_):
        return web.Response(text=metrics.render(scheduler),
                            content_type="text/plain", charset="utf-8",
                            headers={"X-Content-Type-Options": "nosniff"})

    async def stream(request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # Without this, nginx buffers the stream and the dashboard looks frozen.
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        hub.clients.add(queue)
        try:
            await resp.write(b"retry: 3000\n\n")
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    await resp.write(f"data: {json.dumps(item)}\n\n".encode())
                except asyncio.TimeoutError:
                    # Comment line as heartbeat: keeps idle-timeout proxies
                    # from cutting the connection and is ignored by EventSource.
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            hub.clients.discard(queue)
        return resp

    async def health(_):
        return web.json_response({"status": "ok", "targets": len(targets()), "editable": config_file is not None})

    async def _body(request):
        _guard_write(request)
        if config_file is None:
            raise web.HTTPConflict(text="this instance was started without a config file")
        try:
            return await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="body must be JSON")

    def _bad(exc: Exception) -> web.Response:
        return web.json_response({"error": str(exc)}, status=400)

    async def create_target(request):
        data = await _body(request)
        try:
            target = config_file.add(data)
        except ConfigError as exc:
            return _bad(exc)
        scheduler.add(target)
        return web.json_response({"name": target.name}, status=201)

    async def update_target(request):
        name = request.match_info["name"]
        data = await _body(request)
        try:
            target = config_file.replace(name, data)
        except KeyError:
            raise web.HTTPNotFound(text=f"unknown target {name}")
        except ConfigError as exc:
            return _bad(exc)
        # Restart the target so a new interval or address takes effect now.
        scheduler.remove(name)
        scheduler.add(target)
        return web.json_response({"name": target.name})

    async def delete_target(request):
        _guard_write(request)
        if config_file is None:
            raise web.HTTPConflict(text="this instance was started without a config file")
        name = request.match_info["name"]
        try:
            config_file.remove(name)
        except KeyError:
            raise web.HTTPNotFound(text=f"unknown target {name}")
        except ConfigError as exc:
            return _bad(exc)
        scheduler.remove(name)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/targets", list_targets)
    app.router.add_post("/api/targets", create_target)
    app.router.add_put("/api/targets/{name}", update_target)
    app.router.add_delete("/api/targets/{name}", delete_target)
    app.router.add_get("/api/targets/{name}/series", target_series)
    app.router.add_get("/api/events", list_events)
    app.router.add_get("/api/stream", stream)
    app.router.add_get("/metrics", prom)
    app.router.add_get("/healthz", health)
    app.router.add_static("/static", STATIC)
    return app
