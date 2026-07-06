"""Celery application scaffold for future async tasks."""

from __future__ import annotations

from app.config import get_settings

try:
    from celery import Celery
except ImportError:  # pragma: no cover - optional dependency in scaffold phase
    Celery = None  # type: ignore[assignment]


def create_celery_app():
    """Create a Celery app if the dependency is available."""

    settings = get_settings()
    if Celery is None:
        return None

    celery_app = Celery(
        "prompt2pbi",
        broker=settings.redis_url,
        backend=settings.redis_url,
    )
    celery_app.conf.task_default_queue = "prompt2pbi"
    celery_app.conf.task_track_started = True
    return celery_app


celery = create_celery_app()

