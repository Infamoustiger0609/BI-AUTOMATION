from __future__ import annotations

import app.tasks as tasks_module


class RecordingJobManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def run_job(self, job_id: str, include_sample_data: bool) -> None:
        self.calls.append((job_id, include_sample_data))


def test_generate_dashboard_task_actually_runs_the_pipeline(monkeypatch):
    """Regression test: this task used to be a placeholder that returned a
    dict without ever calling JobManager.run_job, so jobs scheduled through
    Celery would sit at "queued" forever. It must now execute the real
    pipeline via the shared JobManager.run_job entrypoint."""

    recorder = RecordingJobManager()
    monkeypatch.setattr(tasks_module, "_get_job_manager", lambda: recorder)

    result = tasks_module.generate_dashboard("job-42", include_sample_data=False)

    assert recorder.calls == [("job-42", False)]
    assert result == {"job_id": "job-42", "include_sample_data": False}
