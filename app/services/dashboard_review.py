"""Helpers for the human-in-the-loop review step between intent parsing and
PBIX generation: converting an IntentResult to/from editable table rows,
detecting metrics that can't be matched to real data, and translating raw
exceptions into plain-language messages for a non-technical user.

Kept separate from ui/gradio_app.py so this logic is unit-testable without
Gradio installed, and separate from app/services/pbix_builder.py so none of
the core intent-parsing/PBIX-building logic is touched by this UI-facing pass.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import ValidationError

from app.models.intent import DimensionSpec, IntentResult, MetricSpec, VisualSpec

METRICS_COLUMNS = [
    "Metric Name",
    "Type",
    "Source Column",
    "Numerator Column",
    "Denominator Column",
    "Description",
]
DIMENSIONS_COLUMNS = ["Dimension Name", "Type", "Grain", "Source Column"]
VISUALS_COLUMNS = ["Chart Type", "Metric", "Dimension", "Title"]

# Metrics with this type -- or with either ratio column populated, regardless
# of type -- are a ratio of two columns (Profit Margin % = Profit/Revenue,
# Average Order Value = Revenue/Orders) and use Numerator/Denominator
# Column instead of a single Source Column, which has no correct value for
# a metric that isn't any one column.
RATIO_METRIC_TYPES = {"percentage", "ratio"}

DEFAULT_METRIC_TYPE = "custom"
DEFAULT_DIMENSION_TYPE = "categorical"
DEFAULT_VISUAL_TYPE = "bar_chart"


def intent_to_tables(intent: IntentResult) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Convert the parsed intent into three editable tables for review."""

    metrics_df = pd.DataFrame(
        [
            {
                "Metric Name": metric.name,
                "Type": metric.type,
                "Source Column": metric.source_column or "",
                "Numerator Column": metric.numerator_column or "",
                "Denominator Column": metric.denominator_column or "",
                "Description": metric.description or "",
            }
            for metric in intent.metrics
        ],
        columns=METRICS_COLUMNS,
    )
    dimensions_df = pd.DataFrame(
        [
            {
                "Dimension Name": dimension.name,
                "Type": dimension.type,
                "Grain": dimension.grain,
                "Source Column": dimension.source_column or "",
            }
            for dimension in intent.dimensions
        ],
        columns=DIMENSIONS_COLUMNS,
    )
    visuals_df = pd.DataFrame(
        [
            {
                "Chart Type": visual.type,
                "Metric": visual.metric or "",
                "Dimension": visual.dimension or "",
                "Title": visual.title or "",
            }
            for visual in intent.visuals
        ],
        columns=VISUALS_COLUMNS,
    )
    return metrics_df, dimensions_df, visuals_df


def _rows(table: Any) -> list[dict[str, Any]]:
    """Normalize whatever a Gradio Dataframe component hands back into records."""

    if table is None:
        return []
    if isinstance(table, pd.DataFrame):
        return table.to_dict(orient="records")
    if isinstance(table, list):
        return table
    return []


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def tables_to_intent(
    base_intent: IntentResult,
    metrics_table: Any,
    dimensions_table: Any,
    visuals_table: Any,
) -> IntentResult:
    """Rebuild an IntentResult from the (possibly user-edited) review tables.

    Every other field (dashboard_title, time_grain, business_goal, etc.) is
    carried over from base_intent unchanged -- only metrics/dimensions/visuals
    reflect what's in the tables. Invalid type values the user might type
    fall back to a safe default rather than raising, so a typo doesn't block
    generation.
    """

    metrics: list[MetricSpec] = []
    for row in _rows(metrics_table):
        name = _clean_str(row.get("Metric Name"))
        if not name:
            continue
        kwargs = {
            "name": name,
            "type": _clean_str(row.get("Type")) or DEFAULT_METRIC_TYPE,
            "source_column": _clean_str(row.get("Source Column")) or None,
            "numerator_column": _clean_str(row.get("Numerator Column")) or None,
            "denominator_column": _clean_str(row.get("Denominator Column")) or None,
            "description": _clean_str(row.get("Description")) or None,
        }
        try:
            metrics.append(MetricSpec(**kwargs))
        except ValidationError:
            kwargs["type"] = DEFAULT_METRIC_TYPE
            metrics.append(MetricSpec(**kwargs))

    dimensions: list[DimensionSpec] = []
    for row in _rows(dimensions_table):
        name = _clean_str(row.get("Dimension Name"))
        if not name:
            continue
        kwargs = {
            "name": name,
            "type": _clean_str(row.get("Type")) or DEFAULT_DIMENSION_TYPE,
            "grain": _clean_str(row.get("Grain")) or "none",
            "source_column": _clean_str(row.get("Source Column")) or None,
        }
        try:
            dimensions.append(DimensionSpec(**kwargs))
        except ValidationError:
            kwargs["type"] = DEFAULT_DIMENSION_TYPE
            kwargs["grain"] = "none"
            dimensions.append(DimensionSpec(**kwargs))

    visuals: list[VisualSpec] = []
    for row in _rows(visuals_table):
        metric = _clean_str(row.get("Metric"))
        dimension = _clean_str(row.get("Dimension"))
        title = _clean_str(row.get("Title"))
        chart_type = _clean_str(row.get("Chart Type")) or DEFAULT_VISUAL_TYPE
        if not metric and not dimension and not title:
            continue
        kwargs = {
            "type": chart_type,
            "metric": metric or None,
            "dimension": dimension or None,
            "title": title or None,
        }
        try:
            visuals.append(VisualSpec(**kwargs))
        except ValidationError:
            kwargs["type"] = DEFAULT_VISUAL_TYPE
            visuals.append(VisualSpec(**kwargs))

    return base_intent.model_copy(update={"metrics": metrics, "dimensions": dimensions, "visuals": visuals})


def find_unmatched_metrics(intent: IntentResult, data_frame: pd.DataFrame | None, pbix_builder: Any) -> list[str]:
    """Return names of simple (non-ratio) metrics whose *suggested* source
    column isn't real.

    Ratio-style metrics (Profit Margin %, Average Order Value, ...) are
    excluded here -- they don't have a single source_column to validate at
    all, and are checked separately by find_unresolved_ratio_metrics(),
    which can report *which side* (numerator/denominator) is the problem.

    A metric with no suggested source_column at all isn't flagged -- falling
    back to a real numeric column in that case is expected behavior, not a
    guess gone wrong. Count/distinct-count metrics are excluded too: they
    degrade gracefully to COUNTROWS()/DISTINCTCOUNT() and don't silently
    aggregate the wrong column the way a mismatched sum/average would.
    """

    if data_frame is None:
        return []
    real_columns = {pbix_builder._sanitize_name(column).lower() for column in data_frame.columns}
    unmatched = []
    for metric in intent.metrics:
        if pbix_builder._is_ratio_metric(metric):
            continue
        if metric.type in {"count", "distinct_count"} or not metric.source_column:
            continue
        candidate = pbix_builder._sanitize_name(metric.source_column).lower()
        if candidate not in real_columns:
            unmatched.append(metric.name)
    return unmatched


def find_unresolved_ratio_metrics(intent: IntentResult, data_frame: pd.DataFrame | None, pbix_builder: Any) -> list[dict[str, str]]:
    """Return ratio-style metrics (Profit Margin %, Average Order Value, ...)
    whose numerator and/or denominator column can't be resolved against the
    real data.

    Each entry identifies exactly which side is the problem, e.g.
    {"name": "Average Order Value", "missing_side": "denominator",
    "resolved_numerator": "Revenue", "resolved_denominator": None} -- so the
    error message can say "couldn't find a column for the denominator of
    ..." instead of a generic "couldn't confidently match" that gives no clue
    which half of the ratio is wrong.
    """

    if data_frame is None:
        return []
    unresolved: list[dict[str, str]] = []
    for metric in intent.metrics:
        if not pbix_builder._is_ratio_metric(metric):
            continue
        numerator, denominator = pbix_builder._resolve_ratio_columns(metric, data_frame)
        numerator_missing = bool(metric.numerator_column) and numerator is None
        denominator_missing = bool(metric.denominator_column) and denominator is None
        # Also flag a side that was never even suggested by the parser --
        # a ratio metric needs both to be resolvable, not just validated
        # when a suggestion happens to be present.
        if not metric.numerator_column:
            numerator_missing = True
        if not metric.denominator_column:
            denominator_missing = True
        if numerator_missing or denominator_missing:
            if numerator_missing and denominator_missing:
                missing_side = "both"
            elif numerator_missing:
                missing_side = "numerator"
            else:
                missing_side = "denominator"
            unresolved.append(
                {
                    "name": metric.name,
                    "missing_side": missing_side,
                    "resolved_numerator": numerator or "",
                    "resolved_denominator": denominator or "",
                }
            )
    return unresolved


def _ratio_side_hint(metric_name: str, side: str) -> str:
    """A short, plain-language hint at what kind of column that side needs."""

    lowered = metric_name.lower()
    if side == "numerator":
        if "margin" in lowered:
            return "expected something like Profit or Net Income"
        if "average" in lowered or "value" in lowered:
            return "expected something like Revenue, Sales, or Amount"
        if "rate" in lowered:
            return "expected a count of the qualifying events (e.g. Conversions, Returns)"
        return "expected a numeric column"
    if "average" in lowered or "value" in lowered:
        return "expected something like Orders, Transactions, or Order Count"
    if "rate" in lowered:
        return "expected a total count to divide into (e.g. Orders, Visits, Total)"
    if "margin" in lowered:
        return "expected something like Revenue or Sales"
    return "expected a numeric column"


def extraction_notices(intent: IntentResult, data_frame: pd.DataFrame | None, pbix_builder: Any) -> list[str]:
    """Plain-language notices to show alongside the review tables."""

    notices: list[str] = []
    if any("fallback" in note.lower() for note in intent.notes):
        notices.append(
            "Generated using simplified matching (the AI provider wasn't available) -- "
            "results may be less precise. Please review the extracted plan carefully below."
        )

    available = ", ".join(str(column) for column in data_frame.columns) if data_frame is not None else "none (no file uploaded)"

    unmatched = find_unmatched_metrics(intent, data_frame, pbix_builder)
    if unmatched:
        notices.append(
            f"Couldn't confidently match these KPIs to a column in your data: {', '.join(unmatched)}. "
            f"Available columns: {available}. Please fix the Source Column below before generating."
        )

    for entry in find_unresolved_ratio_metrics(intent, data_frame, pbix_builder):
        name = entry["name"]
        side = entry["missing_side"]
        if side == "both":
            notices.append(
                f"Couldn't find columns for '{name}' -- it's a ratio metric (numerator/denominator), "
                f"but neither side could be matched to your data. Available columns: {available}. "
                f"Please fill in both the Numerator Column and Denominator Column below before generating."
            )
        else:
            hint = _ratio_side_hint(name, side)
            resolved_key = "resolved_denominator" if side == "numerator" else "resolved_numerator"
            other_side = "denominator" if side == "numerator" else "numerator"
            other_value = entry[resolved_key]
            other_note = f" (the {other_side} matched '{other_value}')" if other_value else ""
            notices.append(
                f"Couldn't find a column for the {side} of '{name}'{other_note} -- {hint}. "
                f"Available columns: {available}. Please fill in the {side.title()} Column below before generating."
            )
    return notices


def friendly_error_message(raw_error: str) -> str:
    """Translate a raw exception message into plain language for a non-technical user."""

    lowered = raw_error.lower()
    if "column" in lowered and ("cannot be found" in lowered or "not found" in lowered):
        return (
            "The generated dashboard referenced a column that doesn't exist in your data. "
            "Try adjusting the metric/dimension mapping above and generate again."
        )
    if "unsupported file" in lowered:
        return "That file type isn't supported. Please upload a CSV or Excel (.xlsx/.xls) file."
    if "exceeds the configured size limit" in lowered:
        return "Your file is too large. Please upload a smaller file or a sample of your data."
    if "is empty" in lowered or ("empty" in lowered and "file" in lowered) or "no columns to parse" in lowered:
        return "The uploaded file appears to be empty or has no columns. Please check the file and try again."
    if "at least 3 characters" in lowered:
        return "Please enter a more detailed prompt describing the dashboard you want."
    if "references missing" in lowered or "must contain at least one" in lowered:
        return (
            "The dashboard plan has an internal inconsistency (a chart or measure refers to "
            "something that doesn't exist). Please review the extracted metrics/dimensions above and try again."
        )
    if "no llm provider is configured" in lowered:
        return "No AI provider is configured, so simplified matching was used automatically."
    return "Something went wrong while generating your dashboard. Please double-check your prompt and file, then try again."
