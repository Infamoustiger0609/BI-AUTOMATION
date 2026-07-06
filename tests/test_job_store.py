from __future__ import annotations

from pathlib import Path

import pytest

from app.services.job_store import (
    InMemoryJobStore,
    JobRecord,
    RedisJobStore,
    create_job_store,
)


class FakeRedisClient:
    """Minimal in-process stand-in for redis.Redis, covering only what RedisJobStore uses."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def ping(self) -> bool:
        return True

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    def get(self, key: str) -> str | None:
        return self._data.get(key)


def _sample_job(job_id: str = "job-1") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        prompt="Build a sales dashboard",
        template="sales",
        temp_files=[Path("uploads/data.csv")],
    )


def test_in_memory_job_store_round_trip():
    store = InMemoryJobStore()
    job = _sample_job()
    store.create(job)

    fetched = store.get(job.job_id)
    assert fetched is not None
    assert fetched.prompt == "Build a sales dashboard"

    updated = store.update(job.job_id, status="complete", progress=100)
    assert updated.status == "complete"
    assert updated.progress == 100
    assert store.get(job.job_id).status == "complete"


def test_in_memory_job_store_missing_job_raises_keyerror():
    store = InMemoryJobStore()
    with pytest.raises(KeyError):
        store.update("does-not-exist", status="complete")
    assert store.get("does-not-exist") is None


def test_redis_job_store_round_trip_preserves_paths_and_status():
    store = RedisJobStore(FakeRedisClient())
    job = _sample_job("job-redis")
    store.create(job)

    fetched = store.get("job-redis")
    assert fetched is not None
    assert fetched.temp_files == [Path("uploads/data.csv")]
    assert fetched.prompt == job.prompt

    updated = store.update("job-redis", status="complete", result_path=Path("output/dashboard.pbix"))
    assert updated.status == "complete"
    assert updated.result_path == Path("output/dashboard.pbix")

    reloaded = store.get("job-redis")
    assert reloaded.status == "complete"
    assert reloaded.result_path == Path("output/dashboard.pbix")


def test_redis_job_store_missing_job_raises_keyerror():
    store = RedisJobStore(FakeRedisClient())
    with pytest.raises(KeyError):
        store.update("missing", status="complete")


def test_create_job_store_falls_back_to_memory_when_redis_unreachable():
    store = create_job_store("redis://127.0.0.1:1/0")
    assert isinstance(store, InMemoryJobStore)
