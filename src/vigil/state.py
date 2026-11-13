"""Hysteresis state machine for target health.

Alerting on every failed probe produces a pager that cries wolf: a single lost
packet on a busy uplink is noise, not an outage. Requiring N consecutive
failures to go DOWN and M consecutive successes to come back UP is the same
idea as a Schmitt trigger — two thresholds instead of one, so a signal hovering
near the boundary can't oscillate the output.

Flapping is tracked separately. A target that keeps crossing both thresholds
is a real problem (a failing link, a duplex mismatch) but a different one from
"down", and paging on every transition would bury the signal.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class Health(str, Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


@dataclass
class Transition:
    target: str
    old: Health
    new: Health
    reason: str
    at: float
    flapping: bool = False


@dataclass
class TargetState:
    name: str
    fail_after: int
    recover_after: int
    flap_window: float = 600.0
    flap_threshold: int = 4
    health: Health = Health.UNKNOWN
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    last_detail: str = ""
    since: float = field(default_factory=time.time)
    _changes: deque = field(default_factory=deque)

    def observe(self, ok: bool, detail: str, now: float | None = None) -> Transition | None:
        now = now if now is not None else time.time()
        self.last_detail = detail
        if ok:
            self.consecutive_ok += 1
            self.consecutive_fail = 0
        else:
            self.consecutive_fail += 1
            self.consecutive_ok = 0

        new = self.health
        if self.health is Health.UNKNOWN:
            # On startup there is no history. One success is enough to show UP,
            # but DOWN still needs fail_after consecutive failures: otherwise a
            # single slow first connection after every restart reads as an outage.
            if ok:
                new = Health.UP
            elif self.consecutive_fail >= self.fail_after:
                new = Health.DOWN
        elif self.health is Health.UP and self.consecutive_fail >= self.fail_after:
            new = Health.DOWN
        elif self.health is Health.DOWN and self.consecutive_ok >= self.recover_after:
            new = Health.UP

        if new is self.health:
            return None

        old, self.health, self.since = self.health, new, now
        if old is Health.UNKNOWN:
            return Transition(self.name, old, new, detail, now)

        self._changes.append(now)
        while self._changes and now - self._changes[0] > self.flap_window:
            self._changes.popleft()
        return Transition(self.name, old, new, detail, now, flapping=self.is_flapping)

    @property
    def is_flapping(self) -> bool:
        return len(self._changes) >= self.flap_threshold
