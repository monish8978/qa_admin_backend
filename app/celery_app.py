"""Celery application. Replaces the BullMQ queues used by the Nest API.

Queues to migrate (see packages/shared/src/index.ts QUEUE_NAMES):
  - tenant.provision
  - eval.process
  - eval.escalate
  - notify.send
  - billing.usage.sync
  - report.export

Task implementations live alongside the modules they belong to and are
imported lazily inside `worker.py` once those modules are ported.
"""
from __future__ import annotations

from celery import Celery
from kombu import Queue

from .config import get_settings
from celery.signals import setup_logging
from .logger import setup_app_logging

@setup_logging.connect
def on_celery_setup_logging(**kwargs):
    setup_app_logging()

_settings = get_settings()

_redis_url = (
    f"redis://:{_settings.REDIS_PASSWORD}@{_settings.REDIS_HOST}:{_settings.REDIS_PORT}/0"
    if _settings.REDIS_PASSWORD
    else f"redis://{_settings.REDIS_HOST}:{_settings.REDIS_PORT}/0"
)

celery_app = Celery(
    "qa_api_py",
    broker=_redis_url,
    backend=_redis_url,
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_queues=[
        Queue("default"),
        Queue("eval.process"),
        Queue("tenant.provision"),
    ],
    task_routes={
        "eval.process": {"queue": "eval.process"},
        "tenant.provision": {"queue": "tenant.provision"},
        "eval.escalate.scan": {"queue": "default"},
    },
    beat_schedule={
        "stale-queue-escalation-scan": {
            "task": "eval.escalate.scan",
            "schedule": 30 * 60,  # every 30 minutes
        },
    },
)
