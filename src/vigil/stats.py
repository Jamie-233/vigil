"""Latency and availability statistics.

Mean latency is deliberately absent. A link that answers in 2 ms 95% of the
time and 800 ms the rest has a mean of ~42 ms — a number no user ever
experiences. Percentiles describe what users actually see, and p95/p99 are
where congestion and bufferbloat show up first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (the 'R-7' method).

    Nearest-rank would make p99 of 50 samples jump between two raw values as
    single samples arrive, which reads as a spike on a dashboard.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class Summary:
    samples: int
    availability: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    error_budget_remaining: float | None
    latency_slo_compliance: float | None


def summarize(
    oks: Sequence[bool],
    latencies: Sequence[float | None],
    availability_slo: float,
    latency_slo_ms: float | None = None,
) -> Summary:
    n = len(oks)
    if n == 0:
        return Summary(0, None, None, None, None, None, None)

    good = sum(oks)
    availability = good / n * 100
    measured = [lat for ok, lat in zip(oks, latencies) if ok and lat is not None]

    # Error budget: the share of allowed failures not yet spent. At 99.9% over
    # 10,000 checks the budget is 10 failures; 4 failures leaves 60%. Negative
    # means the SLO is already breached for this window.
    allowed = n * (1 - availability_slo / 100)
    budget = None if allowed <= 0 else round((allowed - (n - good)) / allowed * 100, 2)

    compliance = None
    if latency_slo_ms is not None and measured:
        # Failed probes count against the latency SLO too; otherwise a service
        # that times out looks perfectly fast.
        within = sum(1 for ok, lat in zip(oks, latencies) if ok and lat is not None and lat <= latency_slo_ms)
        compliance = round(within / n * 100, 3)

    return Summary(
        samples=n,
        availability=round(availability, 3),
        p50=_r(percentile(measured, 50)),
        p95=_r(percentile(measured, 95)),
        p99=_r(percentile(measured, 99)),
        error_budget_remaining=budget,
        latency_slo_compliance=compliance,
    )


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)
