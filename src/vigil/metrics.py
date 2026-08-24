"""Prometheus text exposition.

Written by hand rather than with prometheus_client because Vigil already owns
the state; mirroring it into a second registry would mean two sources of
truth that can disagree. The format is simple enough that correctness is
mostly about escaping and about choosing the right metric types:

* ``vigil_probe_success_total`` / ``..._failure_total`` are counters, so
  Prometheus can compute availability over any window with ``increase()``
  and survive Vigil restarts via counter-reset detection.
* ``vigil_target_up`` is the debounced health, not the last raw probe —
  alert rules built on it inherit the hysteresis instead of reinventing it.
"""

from __future__ import annotations

from .scheduler import Scheduler
from .state import Health


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in kv.items()) + "}"


def _target_labels(t) -> str:
    # Built-in labels win over user labels: a user label called "target" would
    # otherwise silently merge two series into one in Prometheus.
    return _labels(**{**t.labels, "target": t.name, "type": t.type})


def render(scheduler: Scheduler) -> str:
    lines: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    targets = scheduler.config.targets

    family("vigil_target_up", "gauge", "Debounced health: 1 up, 0 down, -1 unknown.")
    for t in targets:
        health = scheduler.states[t.name].health
        value = {Health.UP: 1, Health.DOWN: 0, Health.UNKNOWN: -1}[health]
        lines.append(f"vigil_target_up{_target_labels(t)} {value}")

    family("vigil_probe_latency_ms", "gauge", "Latency of the most recent successful probe.")
    for t in targets:
        res = scheduler.latest.get(t.name)
        if res and res.ok and res.latency_ms is not None:
            lines.append(f"vigil_probe_latency_ms{_target_labels(t)} {res.latency_ms}")

    family("vigil_probe_success_total", "counter", "Successful probes since start.")
    for t in targets:
        lines.append(f"vigil_probe_success_total{_labels(target=t.name)} {scheduler.counters[t.name]['ok']}")

    family("vigil_probe_failure_total", "counter", "Failed probes since start.")
    for t in targets:
        lines.append(f"vigil_probe_failure_total{_labels(target=t.name)} {scheduler.counters[t.name]['fail']}")

    family("vigil_target_flapping", "gauge", "1 if the target is flapping.")
    for t in targets:
        lines.append(f"vigil_target_flapping{_labels(target=t.name)} {int(scheduler.states[t.name].is_flapping)}")

    family("vigil_tls_cert_days_left", "gauge", "Days until the TLS certificate expires.")
    for t in targets:
        res = scheduler.latest.get(t.name)
        if res and "cert_days_left" in res.extra:
            lines.append(f"vigil_tls_cert_days_left{_labels(target=t.name)} {res.extra['cert_days_left']}")

    return "\n".join(lines) + "\n"
