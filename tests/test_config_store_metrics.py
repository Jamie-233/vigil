import time

import pytest

from vigil import metrics
from vigil.config import ConfigError, parse_config
from vigil.probes import ProbeResult
from vigil.scheduler import Scheduler
from vigil.store import Store


def cfg(**target):
    base = {"name": "gw", "type": "tcp", "address": "127.0.0.1:1", "interval": 10, "timeout": 2}
    base.update(target)
    return parse_config({"targets": [base]})


def test_defaults_are_applied_and_overridden():
    c = parse_config({
        "defaults": {"interval": 60, "timeout": 5},
        "targets": [{"name": "a", "type": "icmp", "address": "1.1.1.1"},
                    {"name": "b", "type": "icmp", "address": "1.0.0.1", "interval": 10, "timeout": 2}],
    })
    assert [t.interval for t in c.targets] == [60, 10]


@pytest.mark.parametrize("bad, fragment", [
    ({"type": "smtp"}, "unknown probe type"),
    ({"timeout": 10, "interval": 10}, "shorter than interval"),
    ({"fail_after": 0}, ">= 1"),
])
def test_invalid_targets(bad, fragment):
    with pytest.raises(ConfigError, match=fragment):
        cfg(**bad)


def test_duplicate_names_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"targets": [{"name": "a", "type": "icmp", "address": "x"}] * 2})


def test_series_keeps_spikes_when_downsampling():
    store = Store()
    now = time.time()
    for i in range(100):
        store.add_sample("gw", True, 900.0 if i == 50 else 1.0, "", ts=now - 100 + i)
    series = store.series("gw", since=now - 100, buckets=10)
    assert len(series) == 10
    assert max(b["latency_max"] for b in series) == 900.0


def test_prune_removes_only_old_samples():
    store = Store()
    store.add_sample("gw", True, 1.0, "", ts=time.time() - 40 * 86400)
    store.add_sample("gw", True, 1.0, "", ts=time.time())
    assert store.prune(30) == 1
    assert len(store.samples("gw", 0)) == 1


async def test_scheduler_records_and_exports_metrics():
    async def fake(_t):
        return ProbeResult(True, 12.5, "ok")

    c = cfg(labels={"site": "hq", "target": "spoofed"})
    store = Store()
    sched = Scheduler(c, store, probes={"tcp": fake})
    await sched.run_once(c.targets[0])

    text = metrics.render(sched)
    assert 'vigil_target_up{site="hq",target="gw",type="tcp"} 1' in text
    assert 'vigil_probe_latency_ms{site="hq",target="gw",type="tcp"} 12.5' in text
    assert 'vigil_probe_success_total{target="gw"} 1' in text
    assert "# TYPE vigil_probe_success_total counter" in text
    assert len(store.samples("gw", 0)) == 1


async def test_crashing_probe_becomes_a_failed_sample_not_a_dead_loop():
    async def boom(_t):
        raise RuntimeError("library bug")

    c = cfg()
    sched = Scheduler(c, Store(), probes={"tcp": boom})
    res = await sched.run_once(c.targets[0])
    assert not res.ok and "library bug" in res.detail


def test_label_values_are_escaped():
    assert metrics._labels(a='x"y\\z\n') == '{a="x\\"y\\\\z\\n"}'
