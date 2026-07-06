"""Persistence backends for asynchronous job state.

An in-process dict only works for a single web process. The documented
production deployment runs multiple web replicas and separate Celery
workers, all of which need to see the same job state -- so job records are
persisted in Redis when it is reachable, with an in-memory fallback for
single-process/local-dev/test environments where Redis isn't running.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:  # pragma: no cover - optional dependency in this environment
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]

JOB_KEY_PREFIX = "prompt2pbi:job:"
JOB_TTL_SECONDS = 24 * 60 * 60


@dataclass(slots=True)
class JobRecord:
    """State for a dashboard generation job."""

    job_id: str
    prompt: str
    template: str
    status: str = "pending"
    progress: int = 0
    result_path: Path | None = None
    result_url: str | None = None
    error: str | None = None
    temp_files: list[Path] = field(default_factory=list)
    output_name: str | None = None
    # Serialized IntentResult (model_dump_json) when the caller already parsed
    # -- and the user may have edited -- the intent before requesting
    # generation (the Gradio confirm-then-generate flow). When present,
    # run_job() skips re-parsing the prompt and builds from this directly.
    preparsed_intent_json: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> str:
        payload = asdict(self)
        payload["result_path"] = str(self.result_path) if self.result_path else None
        payload["temp_files"] = [str(path) for path in self.temp_files]
        payload["created_at"] = self.created_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        return json.dumps(payload)

    @classmethod
    def from_json(cls, raw: str) -> JobRecord:
        payload = json.loads(raw)
        payload["result_path"] = Path(payload["result_path"]) if payload.get("result_path") else None
        payload["temp_files"] = [Path(item) for item in payload.get("temp_files", [])]
        payload["created_at"] = datetime.fromisoformat(payload["created_at"])
        payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
        return cls(**payload)


class JobStore(Protocol):
    """Storage contract used by JobManager, satisfied by both backends below."""

    def create(self, job: JobRecord) -> None: ...

    def get(self, job_id: str) -> JobRecord | None: ...

    def update(self, job_id: str, **updates: Any) -> JobRecord: ...


class InMemoryJobStore:
    """Single-process job store. Only safe when there is exactly one worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}

    def create(self, job: JobRecord) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **updates: Any) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            for key, value in updates.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            return job


class RedisJobStore:
    """Redis-backed job store shared across web replicas and Celery workers."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def _key(self, job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"

    def create(self, job: JobRecord) -> None:
        self._client.set(self._key(job.job_id), job.to_json(), ex=JOB_TTL_SECONDS)

    def get(self, job_id: str) -> JobRecord | None:
        raw = self._client.get(self._key(job_id))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return JobRecord.from_json(text)

    def update(self, job_id: str, **updates: Any) -> JobRecord:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = datetime.now(timezone.utc)
        self._client.set(self._key(job_id), job.to_json(), ex=JOB_TTL_SECONDS)
        return job


def create_job_store(redis_url: str, logger: logging.Logger | None = None) -> JobStore:
    """Return a Redis-backed store if Redis is reachable, else an in-memory fallback."""

    log = logger or logging.getLogger(__name__)
    if redis is not None:
        try:
            client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.3, socket_timeout=0.3)
            client.ping()
            log.info("Using Redis-backed job store.", extra={"redis_url": redis_url})
            return RedisJobStore(client)
        except Exception:
            log.warning(
                "Redis is not reachable; falling back to an in-memory job store. "
                "Multi-replica web deployments and Celery workers require a reachable Redis.",
            )
    return InMemoryJobStore()
