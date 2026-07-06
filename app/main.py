"""FastAPI application entry point for Prompt2PBI."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings, get_settings
from app.exceptions import APIKeyError, FileValidationError, JobNotFoundError, JobNotReadyError, RateLimitExceededError
from app.models import (
    ErrorResponse,
    GenerateRequest,
    GenerateResponse,
    GenerateWithPlanRequest,
    HealthResponse,
    JobStatusResponse,
    TemplateInfo,
)
from app.models.intent import IntentResult
from app.services.dashboard_review import extraction_notices, friendly_error_message, intent_to_tables, tables_to_intent
from app.services.data_handler import DataHandler
from app.services.intent_parser import IntentParser
from app.services.job_manager import JobManager
from app.services.pbix_builder import PBIXBuilder
from app.utils.helpers import utc_now_iso
from app.utils.logging_config import configure_logging, request_id_var
from app.utils.validators import validate_prompt

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

try:  # pragma: no cover - optional dependency
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except Exception:  # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain"
    Counter = None  # type: ignore[assignment]
    Histogram = None  # type: ignore[assignment]
    generate_latest = None  # type: ignore[assignment]

REQUEST_COUNT = Counter("prompt2pbi_requests_total", "Total HTTP requests", ["method", "path", "status"]) if Counter else None
REQUEST_LATENCY = Histogram("prompt2pbi_request_duration_seconds", "HTTP request duration", ["method", "path"]) if Histogram else None


@dataclass(slots=True)
class RateLimitState:
    """Simple in-memory rate limit buckets."""

    window_seconds: int
    max_requests: int
    requests: defaultdict[str, deque[float]]


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Attach request ids and emit structured request logs."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        logger = logging.getLogger("prompt2pbi.request")
        logger.info("request.start", extra={"method": request.method, "path": request.url.path})
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.error",
                extra={"method": request.method, "path": request.url.path},
            )
            request_id_var.reset(token)
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        if REQUEST_COUNT is not None:
            REQUEST_COUNT.labels(request.method, request.url.path, str(response.status_code)).inc()
        if REQUEST_LATENCY is not None:
            REQUEST_LATENCY.labels(request.method, request.url.path).observe(time.perf_counter() - started)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        logger.info(
            "request.complete",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        request_id_var.reset(token)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting middleware."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        super().__init__(app)
        self.state = RateLimitState(
            window_seconds=settings.rate_limit_window_seconds,
            max_requests=settings.rate_limit_requests,
            requests=defaultdict(deque),
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/templates"}:
            client_key = request.headers.get("x-api-key") or (request.client.host if request.client else "anonymous")
            now = time.time()
            bucket = self.state.requests[client_key]
            while bucket and now - bucket[0] > self.state.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.state.max_requests:
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        error="rate_limit_exceeded",
                        message="Rate limit exceeded. Please try again later.",
                        request_id=getattr(request.state, "request_id", None),
                    ).model_dump(),
                )
            bucket.append(now)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings)
    app.state.settings = settings
    app.state.intent_parser = IntentParser(provider=settings.llm_provider, settings=settings)
    app.state.data_handler = DataHandler(settings=settings)
    app.state.pbix_builder = PBIXBuilder(settings=settings)
    app.state.job_manager = JobManager(
        settings=settings,
        parser=app.state.intent_parser,
        data_handler=app.state.data_handler,
        pbix_builder=app.state.pbix_builder,
    )
    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Prompt2PBI - generate Power BI dashboards from natural language prompts.",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "generation", "description": "Create and monitor dashboard generation jobs."},
            {"name": "system", "description": "System health and template metadata."},
        ],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=settings)

    @app.exception_handler(APIKeyError)
    async def api_key_error_handler(request: Request, exc: APIKeyError):
        return _error_response(request, 401, "api_key_invalid", str(exc))

    @app.exception_handler(RateLimitExceededError)
    async def rate_limit_handler(request: Request, exc: RateLimitExceededError):
        return _error_response(request, 429, "rate_limit_exceeded", str(exc))

    @app.exception_handler(FileValidationError)
    async def file_validation_handler(request: Request, exc: FileValidationError):
        return _error_response(request, 400, "file_validation_error", str(exc))

    @app.exception_handler(JobNotFoundError)
    async def job_not_found_handler(request: Request, exc: JobNotFoundError):
        return _error_response(request, 404, "job_not_found", str(exc))

    @app.exception_handler(JobNotReadyError)
    async def job_not_ready_handler(request: Request, exc: JobNotReadyError):
        return _error_response(request, 409, "job_not_ready", str(exc))

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return _error_response(request, 400, "invalid_request", str(exc))

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        logging.getLogger("prompt2pbi.error").exception(
            "unhandled.exception",
            extra={"path": request.url.path},
        )
        return _error_response(
            request,
            500,
            "internal_server_error",
            "An unexpected error occurred while processing your request.",
        )

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="healthy", version=settings.app_version, service=settings.app_name)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> JSONResponse:
        if generate_latest is None:
            return JSONResponse(content={"detail": "Metrics are unavailable."}, status_code=503)
        payload = generate_latest()
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/templates", response_model=list[TemplateInfo], tags=["system"])
    async def list_templates() -> list[TemplateInfo]:
        builder: PBIXBuilder = app.state.pbix_builder
        template_map = {
            "sales": ("Sales Dashboard", "Revenue and pipeline analytics", "sales"),
            "financial": ("Financial Dashboard", "Budget, profit, and variance reporting", "finance"),
            "marketing": ("Marketing Dashboard", "Campaign and funnel performance", "marketing"),
            "hr": ("HR Dashboard", "People and workforce analytics", "hr"),
            "operations": ("Operations Dashboard", "Operational throughput and service levels", "operations"),
            "general": ("Prompt2PBI Dashboard", "Flexible multi-purpose template", "general"),
        }
        return [
            TemplateInfo(id=key, name=value[0], description=value[1], category=value[2])
            for key, value in template_map.items()
        ]

    @app.post("/api/generate", response_model=GenerateResponse, tags=["generation"])
    async def generate_dashboard(
        request: GenerateRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> GenerateResponse:
        _validate_api_key(settings, x_api_key)
        job = app.state.job_manager.submit_generation(
            prompt=request.prompt,
            template=request.template,
            include_sample_data=True,
        )
        return GenerateResponse(
            job_id=job.job_id,
            status="pending",
            progress=job.progress,
            message="Generation job accepted.",
        )

    @app.post("/api/generate-with-data", response_model=GenerateResponse, tags=["generation"])
    async def generate_with_data(
        prompt: str = Form(...),
        template: str = Form(default="general"),
        file: UploadFile | None = File(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> GenerateResponse:
        _validate_api_key(settings, x_api_key)
        if file is None:
            raise FileValidationError("A CSV, Excel, or JSON file is required for this endpoint.")
        saved_path = await _save_upload_file(settings, app.state.job_manager, file)
        job = app.state.job_manager.submit_generation(
            prompt=prompt,
            template=template,
            uploaded_file=saved_path,
            include_sample_data=False,
        )
        return GenerateResponse(
            job_id=job.job_id,
            status="pending",
            progress=job.progress,
            message="Data-backed generation job accepted.",
        )

    @app.post("/api/extract", tags=["generation"])
    async def extract_plan(
        prompt: str = Form(...),
        template: str = Form(default="general"),
        file: UploadFile | None = File(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JSONResponse:
        """Parse a prompt (+ optional uploaded file) into a reviewable plan.

        Thin orchestration only -- calls the same IntentParser/DataHandler/
        dashboard_review functions the Gradio UI called directly in-process.
        """
        _validate_api_key(settings, x_api_key)
        job_manager: JobManager = app.state.job_manager
        try:
            normalized_prompt = validate_prompt(prompt)
            upload_path: Path | None = None
            data_frame = None
            if file is not None:
                upload_path = await _save_upload_file(settings, job_manager, file)
                data_frame = job_manager.data_handler.read_dataframe(upload_path)

            data_profile = job_manager._data_profile(data_frame)
            intent = await job_manager.parser.parse_intent(
                normalized_prompt, data_profile=data_profile, prompt_variant=template
            )
            notices = extraction_notices(intent, data_frame, job_manager.pbix_builder)
            metrics_df, dimensions_df, visuals_df = intent_to_tables(intent)

            return JSONResponse(
                {
                    "base_intent": intent.model_dump(mode="json"),
                    "metrics": metrics_df.to_dict(orient="records"),
                    "dimensions": dimensions_df.to_dict(orient="records"),
                    "visuals": visuals_df.to_dict(orient="records"),
                    "notices": notices,
                    "upload_path": str(upload_path) if upload_path else None,
                }
            )
        except Exception as exc:
            logging.getLogger("prompt2pbi.error").exception("extract.failed")
            return JSONResponse(status_code=400, content={"error": friendly_error_message(str(exc))})

    @app.post("/api/generate-with-plan", response_model=GenerateResponse, tags=["generation"])
    async def generate_with_plan(
        payload: GenerateWithPlanRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> GenerateResponse | JSONResponse:
        """Generate from a plan the user has already reviewed (and possibly
        edited) via /api/extract -- never re-parses the prompt from scratch."""

        _validate_api_key(settings, x_api_key)
        job_manager: JobManager = app.state.job_manager
        try:
            base_intent = IntentResult.model_validate(payload.base_intent)
            edited_intent = tables_to_intent(base_intent, payload.metrics, payload.dimensions, payload.visuals)
            upload_path = Path(payload.upload_path) if payload.upload_path else None
            job = job_manager.submit_generation(
                prompt=payload.prompt,
                template=payload.template,
                uploaded_file=upload_path,
                include_sample_data=upload_path is None,
                preparsed_intent=edited_intent,
            )
            return GenerateResponse(
                job_id=job.job_id,
                status="pending",
                progress=job.progress,
                message="Generation job accepted.",
            )
        except Exception as exc:
            logging.getLogger("prompt2pbi.error").exception("generate_with_plan.failed")
            return JSONResponse(status_code=400, content={"error": friendly_error_message(str(exc))})

    @app.get("/api/job/{job_id}/status", response_model=JobStatusResponse, tags=["generation"])
    async def job_status(
        job_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JobStatusResponse:
        _validate_api_key(settings, x_api_key)
        status = app.state.job_manager.get_status(job_id)
        if status.status in {"pending", "queued"}:
            return status
        if status.status == "complete" and not status.result_url:
            status.result_url = str(request.url_for("download_job", job_id=job_id))
        return status

    @app.get("/api/job/{job_id}/download", name="download_job", tags=["generation"])
    async def download_job(
        job_id: str,
        background_tasks: BackgroundTasks,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> FileResponse:
        _validate_api_key(settings, x_api_key)
        path = app.state.job_manager.get_download_path(job_id)
        job = app.state.job_manager.get_job(job_id)
        if job.temp_files:
            background_tasks.add_task(app.state.job_manager.cleanup_files, list(job.temp_files))
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
        )

    @app.websocket("/ws/status")
    async def websocket_status(websocket: WebSocket) -> None:
        job_id = websocket.query_params.get("job_id")
        api_key = websocket.query_params.get("api_key")
        try:
            _validate_api_key(settings, api_key)
        except APIKeyError:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        if not job_id:
            await websocket.send_json({"error": "job_id query parameter is required."})
            await websocket.close(code=1008)
            return

        try:
            while True:
                status = app.state.job_manager.get_status(job_id)
                await websocket.send_json(status.model_dump(mode="json"))
                if status.status in {"complete", "failed"}:
                    break
                await asyncio.sleep(0.75)
            await websocket.close()
        except WebSocketDisconnect:
            return
        except JobNotFoundError as exc:
            await websocket.send_json({"status": "failed", "error": str(exc)})
            await websocket.close(code=1008)

    # Mounted last so it never shadows the explicit /api/* and /ws/* routes
    # registered above -- Starlette matches routes in registration order.
    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


def _validate_api_key(settings: Settings, provided_key: str | None) -> None:
    expected = settings.app_api_key.get_secret_value() if settings.app_api_key else None
    if expected and provided_key != expected:
        raise APIKeyError("A valid X-API-Key header is required.")


async def _save_upload_file(settings: Settings, job_manager: JobManager, file: UploadFile) -> Path:
    filename = file.filename or "upload.bin"
    if not filename:
        raise FileValidationError("The uploaded file is missing a filename.")
    data = await file.read()
    if len(data) > settings.max_file_size:
        raise FileValidationError("Uploaded file exceeds the configured size limit.")
    return job_manager.save_upload_file(filename, file.content_type, data)


def _error_response(request: Request, status_code: int, error_code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(
        error=error_code,
        message=message,
        request_id=getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


app = create_app()
