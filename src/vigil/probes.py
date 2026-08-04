"""Probe implementations.

All probes measure with ``time.perf_counter`` rather than wall-clock time: an
NTP step during a check would otherwise show up as a negative or multi-second
latency spike that never happened.

Each probe answers a different question, which is why a target usually wants
more than one: ICMP says the host is routed, TCP says the service socket is
listening, HTTP says the application answers correctly, DNS says clients can
find it at all. An outage report that only has one of these is a guess.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

from .config import Target


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float | None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


Probe = Callable[[Target], Awaitable[ProbeResult]]


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


async def probe_tcp(t: Target) -> ProbeResult:
    host, _, port = t.address.rpartition(":")
    if not port.isdigit():
        return ProbeResult(False, None, f"address must be host:port, got {t.address!r}")
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), t.timeout)
    except asyncio.TimeoutError:
        # A timeout and a refusal mean different things: refused = host up,
        # nothing listening; timeout = filtered or host unreachable.
        return ProbeResult(False, None, "timeout (filtered or unreachable)")
    except ConnectionRefusedError:
        return ProbeResult(False, _elapsed_ms(start), "connection refused (host up, port closed)")
    except OSError as exc:
        return ProbeResult(False, None, f"{type(exc).__name__}: {exc.strerror or exc}")
    latency = _elapsed_ms(start)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return ProbeResult(True, latency, "connected")


async def probe_http(t: Target, session: aiohttp.ClientSession | None = None) -> ProbeResult:
    opts = t.options
    expect = opts.get("expect_status", [200])
    expect = expect if isinstance(expect, list) else [expect]
    needle = opts.get("expect_body")

    own_session = session is None
    session = session or aiohttp.ClientSession()
    start = time.perf_counter()
    try:
        async with session.request(
            opts.get("method", "GET"),
            t.address,
            timeout=aiohttp.ClientTimeout(total=t.timeout),
            allow_redirects=opts.get("follow_redirects", True),
            ssl=None if opts.get("verify_tls", True) else False,
        ) as resp:
            # Reading the body is part of the latency on purpose — time to
            # headers alone hides slow backends that stream a large response.
            body = await resp.text(errors="replace")
            latency = _elapsed_ms(start)
            extra: dict[str, Any] = {"status": resp.status}
            cert_days = _cert_days_left(resp)
            if cert_days is not None:
                extra["cert_days_left"] = cert_days

            if resp.status not in expect:
                return ProbeResult(False, latency, f"HTTP {resp.status}, expected {expect}", extra)
            if needle and needle not in body:
                return ProbeResult(False, latency, f"body does not contain {needle!r}", extra)
            if cert_days is not None and cert_days < opts.get("cert_warn_days", 14):
                # Still "up" for availability, but surfaced so expiry never
                # becomes an outage at 09:00 on a Monday.
                return ProbeResult(True, latency, f"TLS certificate expires in {cert_days} days", extra)
            return ProbeResult(True, latency, f"HTTP {resp.status}", extra)
    except asyncio.TimeoutError:
        return ProbeResult(False, None, "timeout")
    except aiohttp.ClientSSLError as exc:
        return ProbeResult(False, _elapsed_ms(start), f"TLS error: {exc}")
    except aiohttp.ClientError as exc:
        return ProbeResult(False, None, f"{type(exc).__name__}: {exc}")
    finally:
        if own_session:
            await session.close()


def _cert_days_left(resp: aiohttp.ClientResponse) -> int | None:
    if urlsplit(str(resp.url)).scheme != "https" or resp.connection is None:
        return None
    transport = resp.connection.transport
    sslobj = transport.get_extra_info("ssl_object") if transport else None
    if not isinstance(sslobj, ssl.SSLObject):
        return None
    cert = sslobj.getpeercert()
    if not cert or "notAfter" not in cert:
        return None
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    return (expires - datetime.now(timezone.utc)).days


async def probe_dns(t: Target) -> ProbeResult:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    opts = t.options
    resolver = dns.asyncresolver.Resolver(configure=not opts.get("server"))
    if opts.get("server"):
        resolver.nameservers = [opts["server"]]
    resolver.lifetime = t.timeout
    rtype = opts.get("record", "A")

    start = time.perf_counter()
    try:
        answer = await resolver.resolve(t.address, rtype)
    except dns.resolver.NXDOMAIN:
        return ProbeResult(False, _elapsed_ms(start), "NXDOMAIN")
    except dns.resolver.NoAnswer:
        return ProbeResult(False, _elapsed_ms(start), f"no {rtype} record")
    except dns.exception.Timeout:
        return ProbeResult(False, None, "timeout")
    except dns.exception.DNSException as exc:
        return ProbeResult(False, None, f"{type(exc).__name__}: {exc}")

    latency = _elapsed_ms(start)
    records = sorted(r.to_text() for r in answer)
    expected = opts.get("expect")
    if expected:
        expected = sorted(expected if isinstance(expected, list) else [expected])
        if records != expected:
            # Catches the failure ICMP/HTTP can't: a hijacked or stale record
            # that resolves fine but points somewhere else.
            return ProbeResult(False, latency, f"got {records}, expected {expected}", {"records": records})
    return ProbeResult(True, latency, ", ".join(records), {"records": records})


async def probe_icmp(t: Target) -> ProbeResult:
    from icmplib import async_ping
    from icmplib.exceptions import ICMPLibError, SocketPermissionError

    count = int(t.options.get("count", 3))
    try:
        # Unprivileged datagram sockets, so Vigil never needs root. On Linux
        # this requires net.ipv4.ping_group_range to include the running GID.
        host = await async_ping(t.address, count=count, interval=0.2, timeout=t.timeout, privileged=False)
    except SocketPermissionError:
        return ProbeResult(False, None, "ICMP not permitted: add this GID to net.ipv4.ping_group_range or run with CAP_NET_RAW")
    except ICMPLibError as exc:
        return ProbeResult(False, None, f"{type(exc).__name__}: {exc}")

    extra = {"packet_loss": host.packet_loss, "jitter_ms": round(host.jitter, 2)}
    if not host.is_alive:
        return ProbeResult(False, None, "no reply", extra)
    # Partial loss is "up but degraded": the host answers, users still feel it.
    detail = f"{count - host.packets_received}/{count} lost" if host.packet_loss else "reply"
    return ProbeResult(True, round(host.avg_rtt, 2), detail, extra)


PROBES: dict[str, Probe] = {
    "tcp": probe_tcp,
    "http": probe_http,
    "dns": probe_dns,
    "icmp": probe_icmp,
}
