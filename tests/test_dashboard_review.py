from __future__ import annotations

import pandas as pd
import pytest

from app.config import Settings
from app.models.intent import DimensionSpec, IntentResult, MetricSpec, VisualSpec
from app.services.dashboard_review import (
    extraction_notices,
    find_unmatched_metrics,
    friendly_error_message,
    intent_to_tables,
    tables_to_intent,
)
from app.services.pbix_builder import PBIXBuilder


def _builder(tmp_path):
    settings = Settings()
    settings.output_dir = tmp_path
    settings.upload_dir = tmp_path / "uploads"
    settings.sample_data_dir = tmp_path / "sample"
    return PBIXBuilder(settings=settings)


def _sample_intent() -> IntentResult:
    return IntentResult(
        dashboard_title="Sales Performance Dashboard",
        metrics=[
            MetricSpec(name="Total Revenue", type="sum", source_column="Revenue", description="Total revenue."),
            MetricSpec(name="Number of Orders", type="count", source_column="OrderID"),
        ],
        dimensions=[DimensionSpec(name="Region", type="categorical", source_column="Region")],
        visuals=[VisualSpec(type="line_chart", metric="Total Revenue", dimension="Date", title="Monthly Revenue Trend")],
    )


def test_intent_to_tables_round_trip_preserves_values():
    intent = _sample_intent()
    metrics_df, dimensions_df, visuals_df = intent_to_tables(intent)

    assert list(metrics_df["Metric Name"]) == ["Total Revenue", "Number of Orders"]
    assert list(metrics_df["Source Column"]) == ["Revenue", "OrderID"]
    assert list(dimensions_df["Dimension Name"]) == ["Region"]
    assert list(visuals_df["Chart Type"]) == ["line_chart"]

    rebuilt = tables_to_intent(intent, metrics_df, dimensions_df, visuals_df)
    assert [m.name for m in rebuilt.metrics] == ["Total Revenue", "Number of Orders"]
    assert rebuilt.dashboard_title == "Sales Performance Dashboard"  # carried over untouched


def test_tables_to_intent_reflects_user_edits():
    """The whole point of the review step: editing a wrong mapping before
    generation must actually change what gets built."""

    intent = _sample_intent()
    metrics_df, dimensions_df, visuals_df = intent_to_tables(intent)

    # User fixes the hallucinated "OrderID" source column to the real "Orders" column.
    metrics_df.loc[metrics_df["Metric Name"] == "Number of Orders", "Source Column"] = "Orders"

    edited = tables_to_intent(intent, metrics_df, dimensions_df, visuals_df)
    orders_metric = next(m for m in edited.metrics if m.name == "Number of Orders")
    assert orders_metric.source_column == "Orders"


def test_tables_to_intent_drops_blank_rows_and_survives_bad_type():
    intent = _sample_intent()
    metrics_df = pd.DataFrame(
        [
            {"Metric Name": "", "Type": "sum", "Source Column": "", "Description": ""},  # blank -- dropped
            {"Metric Name": "Total Profit", "Type": "not-a-real-type", "Source Column": "Profit", "Description": ""},
        ]
    )
    dimensions_df = pd.DataFrame(columns=["Dimension Name", "Type", "Grain", "Source Column"])
    visuals_df = pd.DataFrame(columns=["Chart Type", "Metric", "Dimension", "Title"])

    edited = tables_to_intent(intent, metrics_df, dimensions_df, visuals_df)
    assert len(edited.metrics) == 1
    assert edited.metrics[0].name == "Total Profit"
    assert edited.metrics[0].type == "custom"  # invalid type string falls back safely, doesn't crash


def test_find_unmatched_metrics_flags_hallucinated_column(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[
            MetricSpec(name="Total Revenue", type="sum", source_column="Revenue"),
            MetricSpec(name="Total Discount", type="sum", source_column="DiscountAmount"),  # doesn't exist
        ],
    )
    data = pd.DataFrame({"Region": ["East", "West"], "Revenue": [100, 200]})

    unmatched = find_unmatched_metrics(intent, data, builder)
    assert unmatched == ["Total Discount"]


def test_find_unmatched_metrics_excludes_count_type(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[MetricSpec(name="Number of Orders", type="count", source_column="OrderID")],
    )
    data = pd.DataFrame({"Region": ["East", "West"], "Orders": [1, 2]})

    # OrderID doesn't exist, but count-type falls back to COUNTROWS -- not "unmatched".
    assert find_unmatched_metrics(intent, data, builder) == []


def test_extraction_notices_surfaces_fallback_usage(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[MetricSpec(name="Total Revenue", type="sum", source_column="Revenue")],
        notes=["Fallback intent extraction used because: RuntimeError", "LLM integration will be used when provider credentials are available."],
    )
    data = pd.DataFrame({"Revenue": [1, 2]})

    notices = extraction_notices(intent, data, builder)
    assert any("simplified matching" in notice for notice in notices)


def test_extraction_notices_empty_when_everything_matches(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(metrics=[MetricSpec(name="Total Revenue", type="sum", source_column="Revenue")])
    data = pd.DataFrame({"Revenue": [1, 2]})

    assert extraction_notices(intent, data, builder) == []


@pytest.mark.parametrize(
    "raw,expected_snippet",
    [
        ("Column 'OrderID' in table 'FactData' cannot be found", "doesn't exist in your data"),
        ("Unsupported file type: .txt", "CSV or Excel"),
        ("Uploaded file exceeds the configured size limit.", "too large"),
        ("Uploaded file is empty.", "appears to be empty"),
        ("No columns to parse from file", "appears to be empty"),
        ("Prompt must be at least 3 characters long.", "more detailed prompt"),
        ("Relationship references missing table: DimFoo", "internal inconsistency"),
        ("Some totally unexpected internal error", "Something went wrong"),
    ],
)
def test_friendly_error_message_translates_common_failures(raw, expected_snippet):
    assert expected_snippet in friendly_error_message(raw)
