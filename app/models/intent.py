"""
Intent extraction models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class MetricSpec(BaseModel):
    """A metric or measure extracted from the prompt.

    Most metrics map to exactly one real column (sum, count, a plain average
    of one column) and use source_column. Derived/ratio metrics -- "Profit
    Margin %" (Profit/Revenue), "Average Order Value" (Revenue/Orders),
    "Conversion Rate" (Conversions/Visits) -- aren't a value from any single
    column, so they use numerator_column/denominator_column instead. There is
    no correct single source_column for those; asking a user to fill in one
    "Source Column" for a ratio metric has no right answer.
    """

    name: str
    type: Literal[
        "sum", "average", "count", "distinct_count", "min", "max",
        "percentage", "ratio", "custom",
        "measure", "calculated_column", "calculated_table", "custom_column",
        "calculated", "metric", "kpi", "aggregation", "expression"
    ] = "sum"
    description: str | None = None
    source_column: str | None = None
    numerator_column: str | None = None
    denominator_column: str | None = None
    aggregation: str | None = None


class DimensionSpec(BaseModel):
    """A grouping or slicing field extracted from the prompt."""

    name: str
    type: Literal[
        "categorical", "date", "numeric", "geographic", "hierarchy",
        "slowly_changing", "conformed", "junk", "degenerate", "roleplay",
        "temporal", "time", "datetime", "dimension"
    ] = "categorical"
    values: list[str] = Field(default_factory=list)
    grain: Literal["daily", "week", "weekly", "month", "monthly", "quarter", "quarterly", "year", "yearly", "hour", "hourly", "minute", "second", "day", "none"] = "none"
    description: Optional[str] = None
    source_column: Optional[str] = None


class VisualSpec(BaseModel):
    """A visual recommendation for the dashboard."""

    type: Literal[
        "bar_chart", "column_chart", "line_chart", "area_chart", "combo_chart", "ribbon_chart", "waterfall",
        "pie_chart", "donut_chart", "treemap", "funnel",
        "scatter_chart", "scatter_plot", "bubble_chart", "dot_plot",
        "table", "matrix",
        "map", "filled_map", "shape_map", "azure_map", "arcgis_map",
        "card", "multi_row_card", "kpi", "gauge", "goals",
        "decomposition_tree", "key_influencers", "smart_narrative", "anomaly_detection",
        "slicer", "action_button", "page_navigator", "bookmark_navigator",
        "r_visual", "python_visual", "power_apps", "paginated_report", "qna",
        "kpi_card", "stacked_column_chart"
    ] = "bar_chart"
    metric: str | None = None
    dimension: str | None = None
    title: str | None = None
    description: str | None = None


class FilterSpec(BaseModel):
    """A filter condition extracted from the prompt."""

    field: str
    operator: str = "equals"
    value: str | int | float | bool | None = None
    description: str | None = None


class DataSourceSpec(BaseModel):
    """A high-level data source hint."""

    name: str
    description: str | None = None
    required_columns: list[str] = Field(default_factory=list)


class TableSpec(BaseModel):
    """Suggested table structure for dashboard generation."""

    name: str
    columns: list[str] = Field(default_factory=list)


class RelationshipSpec(BaseModel):
    """Suggested relationship between tables."""

    from_field: str
    to_field: str
    cardinality: Literal["one-to-one", "one-to-many", "many-to-one", "many-to-many"] = "many-to-one"
    description: str | None = None


class IntentExtractionPayload(BaseModel):
    """Canonical structured payload returned by the intent parser."""

    dashboard_title: str = "Untitled Dashboard"
    summary: str | None = None
    time_grain: Literal["daily", "weekly", "monthly", "quarterly", "yearly", "hourly", "minute", "second", "mixed", "unknown"] = "unknown"
    metrics: list[MetricSpec] = Field(default_factory=list)
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    visuals: list[VisualSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    suggested_tables: list[TableSpec] = Field(default_factory=list)
    suggested_relationships: list[RelationshipSpec] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class IntentResult(BaseModel):
    """Structured output extracted from a user prompt."""

    dashboard_title: str = "Untitled Dashboard"
    business_goal: str | None = None
    executive_summary: str | None = None
    time_grain: Literal["daily", "weekly", "monthly", "quarterly", "yearly", "hourly", "minute", "second", "mixed", "unknown"] = "unknown"
    metrics: list[MetricSpec] = Field(default_factory=list)
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    visuals: list[VisualSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    data_sources: list[DataSourceSpec | str] = Field(default_factory=list)
    suggested_tables: list[TableSpec] = Field(default_factory=list)
    suggested_relationships: list[RelationshipSpec] = Field(default_factory=list)
    target_audience: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    recommended_pages: list[str] = Field(default_factory=list)
    key_measures: list[str] = Field(default_factory=list)
    visual_recommendations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_response: str | None = None
    provider: str | None = None
    prompt_variant: str = "general"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


DimensionSpec.model_rebuild()
