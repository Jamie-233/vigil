"""Webhook notifications.

Delivery runs on its own queue so a slow or dead chat webhook can never delay
probing. The worst monitoring failure is the one where the alerting path is
coupled to the thing it reports on: if Slack is down and the scheduler awaits
it, you lose visibility exactly when you need it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .config import Webhook
from .state import Health, Transition

log = logging.getLogger(__name__)

_EMOJI = {Health.DOWN: "🔴", Health.UP: "🟢", Health.UNKNOWN: "⚪"}


def render_message(tr: Transition) -> str:
    when = datetime.fromtimestamp(tr.at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"{_EMOJI[tr.new]} *{tr.target}* is {tr.new.value.upper()} — {tr.reason} ({when})"
    if tr.flapping:
        text += "\n⚠️ flapping: state changed repeatedly in the last 10 minutes"
    return text


def payload(hook: Webhook, tr: Transition) -> dict:
    text = render_message(tr)
    if hook.format == "discord":
        return {"content": text.replace("*", "**")}
    if hook.format == "raw":
        return {"target": tr.target, "old": tr.old.value, "new": tr.new.value,
                "reason": tr.reason, "at": tr.at, "flapping": tr.flapping}
    return {"text": text}


class Notifier:
    def __init__(self, hooks: list[Webhook], max_attempts: int = 5, base_delay: float = 1.0):
        self.hooks = hooks
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        # Bounded: during a site-wide outage hundreds of targets transition at
        # once, and an unbounded queue would grow while the webhook rate-limits us.
        self.queue: asyncio.Queue[Transition] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    def notify(self, tr: Transition) -> None:
        if tr.old is Health.UNKNOWN:
            return  # startup discovery is not news
        try:
            self.queue.put_nowait(tr)
        except asyncio.QueueFull:
            log.warning("alert queue full, dropping notification for %s", tr.target)

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    async def _run(self) -> None:
        while True:
            tr = await self.queue.get()
            await asyncio.gather(*(self._deliver(h, tr) for h in self.hooks))

    async def _deliver(self, hook: Webhook, tr: Transition) -> bool:
        assert self._session is not None
        body = payload(hook, tr)
        for attempt in range(self.max_attempts):
            try:
                async with self._session.post(hook.url, json=body) as resp:
                    if resp.status < 300:
                        return True
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                        await asyncio.sleep(max(retry_after, self._backoff(attempt)))
                        continue
                    if 400 <= resp.status < 500:
                        # A 4xx other than 429 is our fault (bad URL, revoked
                        # token); retrying just hammers the endpoint.
                        log.error("webhook %s rejected alert: HTTP %s", hook.url, resp.status)
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("webhook %s attempt %d failed: %s", hook.url, attempt + 1, exc)
            await asyncio.sleep(self._backoff(attempt))
        return False

    def _backoff(self, attempt: int) -> float:
        return self.base_delay * (2 ** attempt)
