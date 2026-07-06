"""Celery task entrypoints for Prompt2PBI."""

from __future__ import annotations

from typing import Any

from app.celery_app import celery
from app.config import get_settings
from app.services.data_handler import DataHandler
from app.services.intent_parser import IntentParser
from app.services.job_manager import JobManager
from app.services.pbix_builder import PBIXBuilder

_job_manager: JobManager | None = None


def _get_job_manager() -> JobManager:
    """Build (once per worker process) the JobManager used to execute jobs.

    A Celery worker runs in a separate process from the web app -- possibly on
    a different machine -- so it needs its own JobManager instance. Because
    JobManager's job store is Redis-backed whenever REDIS_URL is reachable, it
    reads and writes the exact same job records the web process created.
    """

    global _job_manager
    if _job_manager is None:
        settings = get_settings()
        _job_manager = JobManager(
            settings=settings,
            parser=IntentParser(provider=settings.llm_provider, settings=settings),
            data_handler=DataHandler(settings=settings),
            pbix_builder=PBIXBuilder(settings=settings),
        )
    return _job_manager


def generate_dashboard(job_id: str, include_sample_data: bool = True) -> dict[str, Any]:
    """Run the full generation pipeline for a job queued via Celery."""

    _get_job_manager().run_job(job_id, include_sample_data)
    return {"job_id": job_id, "include_sample_data": include_sample_data}


if celery is not None:  # pragma: no cover - optional dependency
    @celery.task(name="app.tasks.generate_dashboard")
    def generate_dashboard_task(job_id: str, include_sample_data: bool = True) -> dict[str, Any]:
        return generate_dashboard(job_id, include_sample_data)

