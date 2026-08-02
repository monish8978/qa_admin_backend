"""Periodic Celery broker (Redis) queue-depth poller.

Replaces apps/api/src/health/queue-metrics-collector.service.ts.

Celery in Redis-broker mode stores each queue as a Redis LIST whose key is the
queue name. LLEN gives the backlog (equivalent to BullMQ's `waiting`). We
expose 0 for `active`/`delayed`/`failed` since the Redis broker doesn't track
those natively without `celery.app.control.inspect()` round-trips (slow + needs
workers online).

Recommended-replica computation mirrors Nest exactly so that any consumer
(HPA, Grafana panel, etc.) keeps the same target backlog and clamp policy.
"""
from __future__ import annotations

import asyncio
import logging
import math

from ..config import Settings, get_settings
from ..redis_client import get_redis
from .metrics import set_queue_depth, set_queue_recommended_replicas

log = logging.getLogger("qa.queue_metrics")

QUEUE_METRIC_INTERVAL_SECONDS = 15

EVAL_QUEUE = "eval.process"
TENANT_QUEUE = "tenant.provision"
_QUEUE_NAMES = (EVAL_QUEUE, TENANT_QUEUE)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, max(1, value or 1)))


def _recommended_replicas(queue: str, backlog: int, settings: Settings) -> int:
    if queue == EVAL_QUEUE:
        return _clamp(
            math.ceil(backlog / settings.AUTOSCALE_EVAL_TARGET_BACKLOG_PER_REPLICA),
            settings.AUTOSCALE_EVAL_MIN_REPLICAS,
            settings.AUTOSCALE_EVAL_MAX_REPLICAS,
        )
    if queue == TENANT_QUEUE:
        return _clamp(
            math.ceil(backlog / settings.AUTOSCALE_TENANT_PROVISION_TARGET_BACKLOG_PER_REPLICA),
            settings.AUTOSCALE_TENANT_PROVISION_MIN_REPLICAS,
            settings.AUTOSCALE_TENANT_PROVISION_MAX_REPLICAS,
        )
    return 1


def poll_once() -> None:
    """Synchronous single poll — sets the redis-driven gauges."""
    redis = get_redis()
    settings = get_settings()
    if redis is None:
        for queue in _QUEUE_NAMES:
            set_queue_depth(queue, waiting=0, active=0, delayed=0, failed=0)
            set_queue_recommended_replicas(queue, 1)
        return

    for queue in _QUEUE_NAMES:
        try:
            waiting = int(redis.llen(queue) or 0)
        except Exception as err:  # noqa: BLE001
            log.warning("queue depth poll failed for %s: %s", queue, err)
            continue
        set_queue_depth(queue, waiting=waiting, active=0, delayed=0, failed=0)
        set_queue_recommended_replicas(queue, _recommended_replicas(queue, waiting, settings))


async def run_forever(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            poll_once()
        except Exception:  # noqa: BLE001
            log.warning("queue metrics tick failed", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=QUEUE_METRIC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
