"""API request and response models for the Prompt2PBI web layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    """JSON request for dashboard generation."""

    prompt: str = Field(..., min_length=3, max_length=5000)
    template: str = Field(default="general")


class GenerateResponse(BaseModel):
    """Response returned immediately after starting generation."""

    job_id: str
    status: Literal["pending", "processing", "queued"] = "pending"
    progress: int = Field(default=0, ge=0, le=100)
    message: str | None = None
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class JobStatusResponse(BaseModel):
    """Status payload for polling job progress."""

    job_id: str
    status: Literal["pending", "processing", "complete", "failed", "queued"]
    progress: int = Field(default=0, ge=0, le=100)
    result_url: str | None = None
    error: str | None = None
    message: str | None = None
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class GenerateWithPlanRequest(BaseModel):
    """JSON request to generate from a (possibly user-edited) extracted plan."""

    prompt: str = Field(..., min_length=3, max_length=5000)
    template: str = Field(default="general")
    upload_path: str | None = None
    base_intent: dict = Field(default_factory=dict)
    metrics: list[dict] = Field(default_factory=list)
    dimensions: list[dict] = Field(default_factory=list)
    visuals: list[dict] = Field(default_factory=list)


class TemplateInfo(BaseModel):
    """A dashboard template exposed by the API."""

    id: str
    name: str
    description: str | None = None
    category: str | None = None


class HealthResponse(BaseModel):
    """Health-check response."""

    status: Literal["healthy"] = "healthy"
    version: str = "1.0.0"
    service: str = "Prompt2PBI"


class ErrorResponse(BaseModel):
    """Standard error response payload."""

    error: str
    message: str
    request_id: str | None = None
    details: dict[str, str] | None = None

