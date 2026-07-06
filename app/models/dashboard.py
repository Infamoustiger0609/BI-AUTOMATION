"""
Pydantic models for dashboard generation requests and responses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .intent import IntentResult


class DashboardRequest(BaseModel):
    """Incoming dashboard generation request."""

    prompt: str = Field(..., min_length=3, max_length=5000)
    template_name: str | None = None
    data_file_name: str | None = None
    include_sample_data: bool = True
    user_notes: str | None = None


class VisualConfig(BaseModel):
    """Configuration for a single report visual."""

    visual_type: str = Field(default="card")
    title: str | None = None
    source_table: str | None = None
    x_axis: str | None = None
    y_axis: str | None = None
    measures: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    position: dict[str, int] = Field(
        default_factory=lambda: {"x": 0, "y": 0, "w": 6, "h": 4}
    )


class PageConfig(BaseModel):
    """Configuration for a report page layout."""

    name: str
    title: str | None = None
    layout: str = Field(default="single_column")
    visuals: list[VisualConfig] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    """Response returned after a dashboard generation request."""

    job_id: str | None = None
    status: str = "queued"
    message: str = "Dashboard generation is scaffolded and not yet implemented."
    file_path: Path | None = None
    output_name: str | None = None
    intent: IntentResult | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

