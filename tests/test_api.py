from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import app
from app.models.api import JobStatusResponse


@dataclass
class FakeJob:
    job_id: str
    status: str = "complete"
    progress: int = 100
    result_path: Path | None = None
    result_url: str | None = "/api/job/job-123/download"
    error: str | None = None
    temp_files: list[Path] | None = None


class FakeJobManager:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.download_path = tmp_path / "output.pbix"
        self.download_path.write_bytes(b"FAKE PBIX")
        self.upload_path = tmp_path / "upload.csv"
        self.upload_path.write_text("Date,Value\n2025-01-01,10\n", encoding="utf-8")

    def submit_generation(self, prompt: str, template: str = "general", uploaded_file: Path | None = None, include_sample_data: bool = True):
        return FakeJob(job_id="job-123", status="pending", progress=0, temp_files=[uploaded_file] if uploaded_file else [])

    def get_status(self, job_id: str) -> JobStatusResponse:
        return JobStatusResponse(job_id=job_id, status="complete", progress=100, result_url=f"/api/job/{job_id}/download")

    def get_download_path(self, job_id: str) -> Path:
        return self.download_path

    def get_job(self, job_id: str) -> FakeJob:
        return FakeJob(job_id=job_id, temp_files=[self.upload_path])

    def save_upload_file(self, filename: str, content_type: str | None, data: bytes) -> Path:
        target = self.tmp_path / filename
        target.write_bytes(data)
        return target

    def cleanup_files(self, paths):
        for path in paths:
            if path.exists():
                path.unlink()


def test_generate_endpoint_returns_job(tmp_path):
    with TestClient(app) as client:
        client.app.state.job_manager = FakeJobManager(tmp_path)
        response = client.post("/api/generate", json={"prompt": "Build a sales dashboard", "template": "sales"})

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-123"
    assert body["status"] == "pending"
    assert body["progress"] == 0


def test_generate_with_data_endpoint_returns_job(tmp_path):
    with TestClient(app) as client:
        client.app.state.job_manager = FakeJobManager(tmp_path)
        files = {"file": ("sales.csv", b"Date,Value\n2025-01-01,10\n", "text/csv")}
        response = client.post(
            "/api/generate-with-data",
            data={"prompt": "Create a sales dashboard", "template": "sales"},
            files=files,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-123"


def test_status_and_download_endpoints(tmp_path):
    with TestClient(app) as client:
        client.app.state.job_manager = FakeJobManager(tmp_path)
        status_response = client.get("/api/job/job-123/status")
        download_response = client.get("/api/job/job-123/download")

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "complete"
    assert download_response.status_code == 200
    assert download_response.content == b"FAKE PBIX"


def test_templates_and_health_endpoints():
    with TestClient(app) as client:
        templates_response = client.get("/api/templates")
        health_response = client.get("/api/health")

    assert templates_response.status_code == 200
    assert len(templates_response.json()) >= 3
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "healthy"
    assert health_response.json()["version"] == "1.0.0"


def test_api_key_validation(tmp_path):
    with TestClient(app) as client:
        client.app.state.job_manager = FakeJobManager(tmp_path)
        client.app.state.settings.app_api_key = SecretStr("secret")
        try:
            response = client.post("/api/generate", json={"prompt": "Build a sales dashboard", "template": "sales"})
        finally:
            client.app.state.settings.app_api_key = None

    assert response.status_code == 401
    assert response.json()["error"] == "api_key_invalid"


def test_status_and_download_endpoints_require_api_key_when_configured(tmp_path):
    with TestClient(app) as client:
        client.app.state.job_manager = FakeJobManager(tmp_path)
        client.app.state.settings.app_api_key = SecretStr("secret")
        try:
            status_response = client.get("/api/job/job-123/status")
            download_response = client.get("/api/job/job-123/download")
            authorized_response = client.get(
                "/api/job/job-123/status", headers={"X-API-Key": "secret"}
            )
        finally:
            client.app.state.settings.app_api_key = None

    assert status_response.status_code == 401
    assert download_response.status_code == 401
    assert authorized_response.status_code == 200
