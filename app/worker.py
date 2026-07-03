"""Celery worker entrypoint.

Start with:   celery -A app.worker.celery_app worker -l info
"""
from __future__ import annotations

from .celery_app import celery_app

# Register Celery task modules (side-effect: each module's @celery_app.task
# decorators bind the tasks to the Celery instance above).
from .services import (  # noqa: E402,F401
    eval_process_task,
    provision_task,
    stale_queue_escalation_task,
)

__all__ = ["celery_app"]
