"""End-to-end coverage for the real-data path: an uploaded CSV goes through
JobManager -> IntentParser -> PBIXBuilder without ever touching the
synthetic/hardcoded sample-data generator. This is the path the Gradio UI
drives; these tests exercise the same JobManager entrypoint directly so they
don't require gradio to be installed to run in CI.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from app.config import Settings
from app.services import job_manager as job_manager_module
from app.services.data_handler import DataHandler
from app.services.intent_parser import IntentParser
from app.services.job_manager import JobManager
from app.services.pbix_builder import PBIXBuilder


def _job_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobManager:
    # Force the local ThreadPoolExecutor fallback instead of Celery. In CI a
    # real Redis is reachable, so celery.send_task() happily enqueues the job
    # -- but with no worker running to consume it, the job sits in "queued"
    # forever and _run_job_and_wait() times out. These tests care about the
    # generation pipeline itself, not which executor runs it.
    monkeypatch.setattr(job_manager_module, "celery", None)

    settings = Settings()
    settings.output_dir = tmp_path / "output"
    settings.upload_dir = tmp_path / "uploads"
    settings.sample_data_dir = tmp_path / "sample"
    settings.ensure_directories()
    return JobManager(
        settings=settings,
        parser=IntentParser(provider="gemini", settings=settings),
        data_handler=DataHandler(settings=settings),
        pbix_builder=PBIXBuilder(settings=settings),
    )


def _write_sample_csv(path: Path) -> Path:
    pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=6, freq="MS"),
            "Region": ["East", "West", "North", "South", "East", "West"],
            "Revenue": [15000, 17000, 12000, 19500, 13500, 21000],
            "Profit": [3000, 3400, 1800, 4200, 2100, 4800],
            "Orders": [120, 135, 90, 150, 110, 160],
        }
    ).to_csv(path, index=False)
    return path


def _run_job_and_wait(job_manager: JobManager, job_id: str, timeout_seconds: float = 15.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = job_manager.get_status(job_id)
        if status.status in {"complete", "failed"}:
            return status
        time.sleep(0.1)
    raise TimeoutError(f"Job {job_id} did not finish in time")


def test_uploaded_csv_generates_pbix_without_synthetic_data(tmp_path, monkeypatch):
    """Prompt + a real uploaded CSV -> a working .pbix, never touching
    DataHandler.generate_sample_data_from_intent()."""

    job_manager = _job_manager(tmp_path, monkeypatch)
    csv_path = _write_sample_csv(tmp_path / "uploads" / "real_sales.csv")

    job = job_manager.submit_generation(
        prompt="Create a sales dashboard showing total revenue and profit by region, with a monthly trend.",
        template="sales",
        uploaded_file=csv_path,
        include_sample_data=False,  # real data only -- no synthetic fallback rows
    )

    status = _run_job_and_wait(job_manager, job.job_id)
    assert status.status == "complete", status.error

    output_path = job_manager.get_download_path(job.job_id)
    assert output_path.exists()
    assert output_path.stat().st_size > 0

    # The job's own record should reflect the real uploaded file, not sample data.
    record = job_manager.get_job(job.job_id)
    assert record.temp_files and record.temp_files[0] == csv_path


def test_uploaded_csv_with_mismatched_dimension_name_does_not_crash(tmp_path, monkeypatch):
    """Regression test for the live bug found while testing the Gradio flow:
    the (fallback) parser suggested dimension names "Product"/"Category" that
    don't exist in the real data (the actual column is "Product_Category"),
    which crashed _build_dimension_tables with a pandas KeyError. Must degrade
    gracefully instead."""

    job_manager = _job_manager(tmp_path, monkeypatch)
    csv_path = tmp_path / "uploads" / "products.csv"
    pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=4, freq="MS"),
            "Region": ["East", "West", "North", "South"],
            "Product_Category": ["Electronics", "Furniture", "Electronics", "Furniture"],
            "Revenue": [1000, 1200, 1300, 1100],
        }
    ).to_csv(csv_path, index=False)

    job = job_manager.submit_generation(
        prompt="Build a dashboard showing revenue by region and product category.",
        template="sales",
        uploaded_file=csv_path,
        include_sample_data=False,
    )

    status = _run_job_and_wait(job_manager, job.job_id)
    assert status.status == "complete", status.error
    assert job_manager.get_download_path(job.job_id).exists()
