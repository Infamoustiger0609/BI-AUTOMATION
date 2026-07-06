"""Pydantic models for Prompt2PBI."""

from .api import (
    ErrorResponse,
    GenerateRequest,
    GenerateResponse,
    GenerateWithPlanRequest,
    HealthResponse,
    JobStatusResponse,
    TemplateInfo,
)
from .dashboard import DashboardRequest, DashboardResponse, PageConfig, VisualConfig
from .intent import (
    DataSourceSpec,
    DimensionSpec,
    FilterSpec,
    IntentExtractionPayload,
    IntentResult,
    MetricSpec,
    RelationshipSpec,
    TableSpec,
    VisualSpec,
)

__all__ = [
    "DashboardRequest",
    "DashboardResponse",
    "PageConfig",
    "VisualConfig",
    "GenerateRequest",
    "GenerateResponse",
    "GenerateWithPlanRequest",
    "JobStatusResponse",
    "TemplateInfo",
    "HealthResponse",
    "ErrorResponse",
    "IntentResult",
    "MetricSpec",
    "DimensionSpec",
    "VisualSpec",
    "FilterSpec",
    "DataSourceSpec",
    "TableSpec",
    "RelationshipSpec",
    "IntentExtractionPayload",
]
