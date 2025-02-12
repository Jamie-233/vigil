"""Configuration model.

Every target carries its own interval, timeout and thresholds because a single
global setting is wrong for a mixed fleet: a WAN gateway deserves a 10 s check
and a hair trigger, while a flaky guest-Wi-Fi captive portal needs a longer
window before anyone gets paged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROBE_TYPES = ("icmp", "tcp", "http", "dns")


class ConfigError(ValueError):
    pass


@dataclass
class Target:
    name: str
    type: str
    address: str
    interval: float = 30.0
    timeout: float = 5.0
    fail_after: int = 3
    recover_after: int = 2
    latency_slo_ms: float | None = None
    availability_slo: float = 99.9
    labels: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Webhook:
    url: str
    format: str = "slack"


@dataclass
class Config:
    targets: list[Target]
    webhooks: list[Webhook] = field(default_factory=list)
    database: str = "vigil.db"
    retention_days: int = 30
    listen: str = "127.0.0.1:8080"
    max_concurrency: int = 64


def _target(raw: dict, defaults: dict) -> Target:
    data = {**defaults, **raw}
    for key in ("name", "type", "address"):
        if key not in data:
            raise ConfigError(f"target {raw!r} is missing '{key}'")
    if data["type"] not in PROBE_TYPES:
        raise ConfigError(f"{data['name']}: unknown probe type '{data['type']}'")

    t = Target(
        name=str(data["name"]),
        type=data["type"],
        address=str(data["address"]),
        interval=float(data.get("interval", 30)),
        timeout=float(data.get("timeout", 5)),
        fail_after=int(data.get("fail_after", 3)),
        recover_after=int(data.get("recover_after", 2)),
        latency_slo_ms=data.get("latency_slo_ms"),
        availability_slo=float(data.get("availability_slo", 99.9)),
        labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
        options=data.get("options") or {},
    )
    # A timeout longer than the interval lets slow probes pile up behind each
    # other; the scheduler would then measure its own queueing, not the network.
    if t.timeout >= t.interval:
        raise ConfigError(f"{t.name}: timeout ({t.timeout}s) must be shorter than interval ({t.interval}s)")
    if t.fail_after < 1 or t.recover_after < 1:
        raise ConfigError(f"{t.name}: fail_after and recover_after must be >= 1")
    return t


def parse_config(data: dict) -> Config:
    if not isinstance(data, dict) or not data.get("targets"):
        raise ConfigError("config needs a non-empty 'targets' list")
    defaults = data.get("defaults") or {}
    targets = [_target(t, defaults) for t in data["targets"]]

    names = [t.name for t in targets]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        # Names key the time series; duplicates would interleave two targets'
        # samples and produce a plausible-looking but meaningless graph.
        raise ConfigError(f"duplicate target names: {', '.join(sorted(dupes))}")

    return Config(
        targets=targets,
        webhooks=[Webhook(**w) for w in data.get("webhooks") or []],
        database=data.get("database", "vigil.db"),
        retention_days=int(data.get("retention_days", 30)),
        listen=data.get("listen", "127.0.0.1:8080"),
        max_concurrency=int(data.get("max_concurrency", 64)),
    )


def load_config(path: str | Path) -> Config:
    with Path(path).open(encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh))
