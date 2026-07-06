"""Job management for asynchronous dashboard generation."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from app.config import Settings, get_settings
from app.exceptions import FileValidationError, JobNotFoundError, JobNotReadyError
from app.models.api import JobStatusResponse
from app.models.intent import IntentResult
from app.services.data_handler import DataHandler
from app.services.intent_parser import IntentParser
from app.services.job_store import JobRecord, JobStore, create_job_store
from app.services.pbix_builder import PBIXBuilder
from app.utils.helpers import ensure_directory, safe_filename
from app.utils.validators import is_allowed_extension, validate_prompt

try:  # pragma: no cover - optional dependency in this environment
    from app.celery_app import celery
except Exception:  # pragma: no cover
    celery = None


class JobManager:
    """Create, track, and finalize asynchronous PBIX generation jobs."""

    def __init__(
        self,
        settings: Settings | None = None,
        parser: IntentParser | None = None,
        data_handler: DataHandler | None = None,
        pbix_builder: PBIXBuilder | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.parser = parser or IntentParser(provider=self.settings.llm_provider)
        self.data_handler = data_handler or DataHandler(settings=self.settings)
        self.pbix_builder = pbix_builder or PBIXBuilder(settings=self.settings)
        self.logger = logger or logging.getLogger(__name__)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prompt2pbi")
        self._store: JobStore = create_job_store(self.settings.redis_url, logger=self.logger)

        ensure_directory(self.settings.upload_dir)
        ensure_directory(self.settings.output_dir)

    def submit_generation(
        self,
        prompt: str,
        template: str = "general",
        uploaded_file: Path | None = None,
        include_sample_data: bool = True,
        preparsed_intent: IntentResult | None = None,
    ) -> JobRecord:
        """Create a job and schedule it for background execution.

        preparsed_intent lets a caller that already ran parse_intent() (and
        let the user review/edit the result, e.g. the Gradio confirmation
        step) skip re-parsing the prompt -- run_job() builds directly from it.
        """

        normalized_prompt = validate_prompt(prompt)
        job = JobRecord(
            job_id=uuid4().hex,
            prompt=normalized_prompt,
            template=template or "general",
            status="pending",
            preparsed_intent_json=preparsed_intent.model_dump_json() if preparsed_intent else None,
        )
        if uploaded_file is not None:
            job.temp_files.append(uploaded_file)

        self._store.create(job)

        self._schedule(job.job_id, include_sample_data=include_sample_data)
        return job

    def get_status(self, job_id: str) -> JobStatusResponse:
        """Return a typed job status response."""

        job = self._get_job(job_id)
        return JobStatusResponse(
            job_id=job.job_id,
            status=job.status,  # type: ignore[arg-type]
            progress=job.progress,
            result_url=job.result_url,
            error=job.error,
            message=self._status_message(job),
            updated_at=job.updated_at,
        )

    def get_job(self, job_id: str) -> JobRecord:
        """Return the raw job record."""

        return self._get_job(job_id)

    def get_download_path(self, job_id: str) -> Path:
        """Return the generated PBIX artifact path."""

        job = self._get_job(job_id)
        if job.status == "failed":
            raise JobNotReadyError(f"Job '{job_id}' failed: {job.error or 'unknown error'}")
        if job.status != "complete" or job.result_path is None:
            raise JobNotReadyError(f"Job '{job_id}' is not ready yet (status: {job.status}).")
        return job.result_path

    def cleanup_files(self, paths: list[Path]) -> None:
        """Remove temporary files after response delivery."""

        for path in paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                self.logger.warning("Failed to clean up temporary file", extra={"path": str(path)})

    def save_upload_file(self, filename: str, content_type: str | None, data: bytes) -> Path:
        """Validate and persist an uploaded file to the upload directory."""

        self._validate_upload(filename, data)
        safe_name = safe_filename(Path(filename).stem)
        suffix = Path(filename).suffix.lower()
        temp_path = self.settings.upload_dir / f"{safe_name}_{uuid4().hex}{suffix}"
        temp_path.write_bytes(data)
        return temp_path

    async def save_upload_from_stream(
        self,
        filename: str,
        content_type: str | None,
        stream_reader,
    ) -> Path:
        """Save an uploaded file from a FastAPI stream while enforcing size limits."""

        suffix = Path(filename).suffix.lower()
        if not is_allowed_extension(filename, self.settings.allowed_extensions):
            raise FileValidationError("Unsupported file type.")
        safe_name = safe_filename(Path(filename).stem)
        temp_path = self.settings.upload_dir / f"{safe_name}_{uuid4().hex}{suffix}"
        total = 0
        with temp_path.open("wb") as handle:
            while True:
                chunk = await stream_reader.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.settings.max_file_size:
                    handle.close()
                    if temp_path.exists():
                        temp_path.unlink()
                    raise FileValidationError("Uploaded file exceeds the configured size limit.")
                handle.write(chunk)
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise FileValidationError("Uploaded file could not be saved.")
        return temp_path

    def _validate_upload(self, filename: str, data: bytes) -> None:
        if not is_allowed_extension(filename, self.settings.allowed_extensions):
            raise FileValidationError("Unsupported file type.")
        if not data:
            raise FileValidationError("Uploaded file is empty.")
        if len(data) > self.settings.max_file_size:
            raise FileValidationError("Uploaded file exceeds the configured size limit.")

    def _schedule(self, job_id: str, include_sample_data: bool) -> None:
        if celery is not None and getattr(celery, "send_task", None):
            try:
                celery.send_task(
                    "app.tasks.generate_dashboard",
                    args=[job_id, include_sample_data],
                )
                self._update_job(job_id, status="queued", progress=5)
                return
            except Exception:
                self.logger.exception("Celery scheduling failed; falling back to local executor")

        self._update_job(job_id, status="processing", progress=5)
        self._executor.submit(self.run_job, job_id, include_sample_data)

    def run_job(self, job_id: str, include_sample_data: bool) -> None:
        """Execute the full generation pipeline for a queued job.

        Called from the local thread-pool fallback and from the Celery task
        entrypoint (app/tasks.py) -- both paths read/write the same job store,
        so this is the single place the pipeline logic lives.
        """
        try:
            job = self._get_job(job_id)
            self._update_job(job_id, status="processing", progress=10)
            data_frame = self._load_dataframe(job, include_sample_data=include_sample_data)
            self._update_job(job_id, progress=30)
            if job.preparsed_intent_json:
                intent = IntentResult.model_validate_json(job.preparsed_intent_json)
            else:
                intent = asyncio.run(
                    self.parser.parse_intent(
                        job.prompt,
                        data_profile=self._data_profile(data_frame),
                        prompt_variant=job.template,
                    )
                )
            self._update_job(job_id, progress=55)
            # Prefer the concise, LLM/heuristic-generated dashboard title over
            # the raw prompt -- a full multi-line prompt used as a filename
            # can exceed Windows' path-length limit and fail with an opaque
            # OSError at save time.
            output_path = self.pbix_builder.create_pbix(
                intent=intent,
                data_frame=data_frame if data_frame is not None else None,
                output_name=job.output_name or intent.dashboard_title,
                source_path=job.temp_files[0] if job.temp_files else None,
                template_name=job.template,
            )
            self._update_job(
                job_id,
                status="complete",
                progress=100,
                result_path=output_path,
                result_url=f"/api/job/{job_id}/download",
            )
        except Exception as exc:
            self.logger.exception("Job failed", extra={"job_id": job_id})
            self._update_job(
                job_id,
                status="failed",
                progress=100,
                error=str(exc),
            )

    def _load_dataframe(self, job: JobRecord, include_sample_data: bool) -> pd.DataFrame | None:
        if not job.temp_files:
            return None
        upload_path = job.temp_files[0]
        if not upload_path.exists():
            raise FileValidationError(f"Uploaded file missing: {upload_path}")
        return self.data_handler.read_dataframe(upload_path)

    def _data_profile(self, data_frame: pd.DataFrame | None) -> dict[str, Any] | None:
        if data_frame is None:
            return None
        # "columns"/"sample_columns" stay a flat list of names -- the
        # heuristic fallback parser's _extract_profile_columns() depends on
        # that exact shape. "schema" is additive: real per-column dtype and
        # sample values, so metric/dimension extraction (LLM or heuristic)
        # resolves against the user's actual columns instead of guessing at
        # naming conventions.
        return {
            "columns": list(data_frame.columns),
            "row_count": int(len(data_frame)),
            "sample_columns": list(data_frame.columns[:6]),
            "schema": self.data_handler.infer_schema(data_frame),
        }

    def _get_job(self, job_id: str) -> JobRecord:
        job = self._store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job '{job_id}' not found.")
        return job

    def _update_job(self, job_id: str, **updates: Any) -> None:
        try:
            self._store.update(job_id, **updates)
        except KeyError as exc:
            raise JobNotFoundError(f"Job '{job_id}' not found.") from exc

    def _status_message(self, job: JobRecord) -> str | None:
        if job.status == "complete":
            return "Generation complete."
        if job.status == "failed":
            return job.error or "Generation failed."
        if job.status == "processing":
            return "Dashboard is being generated."
        if job.status == "queued":
            return "Dashboard generation is queued."
        return "Dashboard generation pending."
