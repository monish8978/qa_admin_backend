"""Prometheus metrics — ports apps/api/src/health/metrics.service.ts.

Module-level singletons so repeated FastAPI startup (tests) doesn't double-
register collectors. Mirrors metric names + label sets exactly so existing
Grafana dashboards keep working when the Nest API is retired.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

if TYPE_CHECKING:  # pragma: no cover
    pass


_DEFAULT_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


def _get_or_create_counter(
    registry: CollectorRegistry, name: str, doc: str, labels: tuple[str, ...]
) -> Counter:
    existing = registry._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if existing is not None:
        return existing  # type: ignore[return-value]
    return Counter(name, doc, labels, registry=registry)


def _get_or_create_gauge(
    registry: CollectorRegistry, name: str, doc: str, labels: tuple[str, ...]
) -> Gauge:
    existing = registry._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if existing is not None:
        return existing  # type: ignore[return-value]
    return Gauge(name, doc, labels, registry=registry)


def _get_or_create_histogram(
    registry: CollectorRegistry,
    name: str,
    doc: str,
    labels: tuple[str, ...],
    buckets: tuple[float, ...],
) -> Histogram:
    existing = registry._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if existing is not None:
        return existing  # type: ignore[return-value]
    return Histogram(name, doc, labels, buckets=buckets, registry=registry)


_REGISTRY = DEFAULT_REGISTRY

http_requests_total = _get_or_create_counter(
    _REGISTRY,
    "http_requests_total",
    "Total number of HTTP requests",
    ("method", "route", "status_code"),
)

http_request_duration_ms = _get_or_create_histogram(
    _REGISTRY,
    "http_request_duration_ms",
    "HTTP request duration in milliseconds",
    ("method", "route", "status_code"),
    _DEFAULT_BUCKETS,
)

queue_jobs_waiting = _get_or_create_gauge(
    _REGISTRY,
    "queue_jobs_waiting",
    "Number of waiting jobs in a queue",
    ("queue",),
)
queue_jobs_active = _get_or_create_gauge(
    _REGISTRY,
    "queue_jobs_active",
    "Number of active jobs in a queue",
    ("queue",),
)
queue_jobs_delayed = _get_or_create_gauge(
    _REGISTRY,
    "queue_jobs_delayed",
    "Number of delayed jobs in a queue",
    ("queue",),
)
queue_jobs_failed = _get_or_create_gauge(
    _REGISTRY,
    "queue_jobs_failed",
    "Number of failed jobs in a queue",
    ("queue",),
)
queue_autoscale_recommended_replicas = _get_or_create_gauge(
    _REGISTRY,
    "queue_autoscale_recommended_replicas",
    "Recommended worker replicas for each queue based on backlog policy",
    ("queue",),
)


def record_http_request(method: str, route: str, status_code: int, duration_ms: float) -> None:
    labels = (method.upper(), route, str(status_code))
    http_requests_total.labels(*labels).inc()
    http_request_duration_ms.labels(*labels).observe(duration_ms)


def set_queue_depth(
    queue: str, *, waiting: int, active: int, delayed: int, failed: int
) -> None:
    queue_jobs_waiting.labels(queue).set(waiting)
    queue_jobs_active.labels(queue).set(active)
    queue_jobs_delayed.labels(queue).set(delayed)
    queue_jobs_failed.labels(queue).set(failed)


def set_queue_recommended_replicas(queue: str, replicas: int) -> None:
    queue_autoscale_recommended_replicas.labels(queue).set(replicas)


def render_metrics_text() -> bytes:
    return generate_latest(_REGISTRY)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
