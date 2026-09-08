"""Probe scheduler.

Each target runs in its own task on a fixed-rate schedule — the next run is
anchored to when the previous one was *due*, not when it finished. Fixed-delay
scheduling (sleep N after each probe) drifts by the probe's own latency, so a
target with 4 s timeouts on a 30 s interval would quietly sample every 34 s
and under-count failures by ~12% exactly when the network is degraded.

Start times are spread across the interval. If 200 targets all fire at t=0,
the probes contend for the same uplink and DNS resolver and measure the
monitor's own burst rather than the network.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable

from .config import Config, Target
from .probes import PROBES, Probe, ProbeResult
from .state import TargetState, Transition
from .store import Store

log = logging.getLogger(__name__)

Listener = Callable[[str, ProbeResult, TargetState], None]


class Scheduler:
    def __init__(self, config: Config, store: Store, probes: dict[str, Probe] | None = None,
                 on_transition: Callable[[Transition], None] | None = None):
        self.config = config
        self.store = store
        self.probes = probes or PROBES
        self.on_transition = on_transition
        self.states = {
            t.name: TargetState(t.name, t.fail_after, t.recover_after) for t in config.targets
        }
        self.latest: dict[str, ProbeResult] = {}
        self.listeners: list[Listener] = []
        self.counters: dict[str, dict[str, int]] = {t.name: {"ok": 0, "fail": 0} for t in config.targets}
        self._sem = asyncio.Semaphore(config.max_concurrency)
        self._tasks: list[asyncio.Task] = []

    async def run_once(self, target: Target) -> ProbeResult:
        probe = self.probes[target.type]
        async with self._sem:
            try:
                # Belt and braces: each probe enforces its own timeout, but a
                # library bug must not wedge this target's loop forever.
                result = await asyncio.wait_for(probe(target), target.timeout + 1)
            except asyncio.TimeoutError:
                result = ProbeResult(False, None, "probe exceeded hard deadline")
            except Exception as exc:
                log.exception("probe %s crashed", target.name)
                result = ProbeResult(False, None, f"probe error: {type(exc).__name__}: {exc}")
        self._record(target, result)
        return result

    def _record(self, target: Target, result: ProbeResult) -> None:
        now = time.time()
        self.latest[target.name] = result
        self.counters[target.name]["ok" if result.ok else "fail"] += 1
        # Writes are a few ms on a slow SD card; doing them inline on the loop
        # is acceptable at this volume and keeps ordering trivially correct.
        self.store.add_sample(target.name, result.ok, result.latency_ms, result.detail, result.extra, now)

        state = self.states[target.name]
        transition = state.observe(result.ok, result.detail, now)
        # Startup discovery of a healthy target is not news and would bury real
        # transitions after every restart; starting out *down* still is.
        if transition and not (transition.old.value == "unknown" and transition.new.value == "up"):
            self.store.add_event(target.name, transition.old.value, transition.new.value,
                                 transition.reason, transition.flapping, transition.at)
        if transition and self.on_transition:
            self.on_transition(transition)
        for listener in self.listeners:
            listener(target.name, result, state)

    async def _loop(self, target: Target) -> None:
        loop = asyncio.get_running_loop()
        next_due = loop.time() + random.uniform(0, target.interval)
        while True:
            await asyncio.sleep(max(0.0, next_due - loop.time()))
            await self.run_once(target)
            next_due += target.interval
            # If we fell more than one interval behind (laptop sleep, CPU
            # starvation), skip the backlog rather than firing a burst.
            if loop.time() - next_due > target.interval:
                next_due = loop.time() + target.interval

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(t), name=f"probe:{t.name}") for t in self.config.targets]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
