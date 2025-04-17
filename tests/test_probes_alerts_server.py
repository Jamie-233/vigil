import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vigil.alerts import Notifier, payload
from vigil.config import Target, Webhook, parse_config
from vigil.probes import probe_http, probe_tcp
from vigil.scheduler import Scheduler
from vigil.server import build_app
from vigil.state import Health, Transition
from vigil.store import Store


async def test_tcp_open_port():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        res = await probe_tcp(Target("t", "tcp", f"127.0.0.1:{port}", timeout=2))
    assert res.ok and res.latency_ms is not None


async def test_tcp_refused_is_distinguished_from_timeout():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    res = await probe_tcp(Target("t", "tcp", f"127.0.0.1:{port}", timeout=2))
    assert not res.ok and "refused" in res.detail


async def _http_app(handler):
    app = web.Application()
    app.router.add_get("/", handler)
    srv = TestServer(app)
    await srv.start_server()
    return srv


async def test_http_status_and_body_assertions():
    async def ok(_):
        return web.Response(text="service healthy")

    srv = await _http_app(ok)
    try:
        url = str(srv.make_url("/"))
        good = await probe_http(Target("w", "http", url, options={"expect_body": "healthy"}))
        bad = await probe_http(Target("w", "http", url, options={"expect_body": "degraded"}))
        wrong = await probe_http(Target("w", "http", url, options={"expect_status": 204}))
    finally:
        await srv.close()
    assert good.ok and good.extra["status"] == 200
    assert not bad.ok and "does not contain" in bad.detail
    assert not wrong.ok and "expected [204]" in wrong.detail


async def test_http_timeout():
    async def slow(_):
        await asyncio.sleep(2)
        return web.Response(text="late")

    srv = await _http_app(slow)
    try:
        res = await probe_http(Target("w", "http", str(srv.make_url("/")), timeout=0.3))
    finally:
        await srv.close()
    assert not res.ok and res.detail == "timeout"


def test_payload_formats():
    tr = Transition("gw", Health.UP, Health.DOWN, "timeout", 0.0, flapping=True)
    assert "*gw* is DOWN" in payload(Webhook("u", "slack"), tr)["text"]
    assert "**gw** is DOWN" in payload(Webhook("u", "discord"), tr)["content"]
    assert payload(Webhook("u", "raw"), tr)["flapping"] is True


async def test_notifier_retries_on_rate_limit_then_delivers():
    calls = []

    async def hook(request):
        calls.append(await request.json())
        if len(calls) == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/hook", hook)
    srv = TestServer(app)
    await srv.start_server()
    n = Notifier([Webhook(str(srv.make_url("/hook")))], base_delay=0.01)
    await n.start()
    try:
        n.notify(Transition("gw", Health.UP, Health.DOWN, "timeout", 0.0))
        n.notify(Transition("gw", Health.UNKNOWN, Health.UP, "startup", 0.0))  # ignored
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        await n.stop()
        await srv.close()
    assert len(calls) == 2
    assert all("DOWN" in c["text"] for c in calls)


@pytest.fixture
async def client():
    config = parse_config({"targets": [{"name": "gw", "type": "tcp", "address": "127.0.0.1:1",
                                        "interval": 10, "timeout": 1}]})
    store = Store()
    sched = Scheduler(config, store)
    await sched.run_once(config.targets[0])
    c = TestClient(TestServer(build_app(sched, store)))
    await c.start_server()
    yield c
    await c.close()


async def test_api_targets_and_metrics(client):
    data = await (await client.get("/api/targets?range=1h")).json()
    assert data[0]["name"] == "gw" and data[0]["health"] == "down"
    assert data[0]["summary"]["availability"] == 0.0

    assert (await client.get("/api/targets?range=2y")).status == 400
    assert (await client.get("/api/targets/nope/series")).status == 404

    text = await (await client.get("/metrics")).text()
    assert 'vigil_target_up{target="gw",type="tcp"} 0' in text

    html = await (await client.get("/")).text()
    assert "EventSource" in html


async def test_event_stream_sends_retry_hint(client):
    resp = await client.get("/api/stream")
    first = await resp.content.readuntil(b"\n\n")
    assert first.startswith(b"retry:")
    resp.close()
