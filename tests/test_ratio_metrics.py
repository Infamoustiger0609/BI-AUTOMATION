"""Tests for ratio/derived-metric KPI support: Profit Margin %, Average Order
Value, and other metrics that are a ratio of two columns rather than a value
from any single column.

Deliberately covers more than the two examples that originally surfaced the
bug (Profit Margin %, Average Order Value) -- Return Rate, Cost per Order,
and Customer Retention Rate (the unresolvable case) are included so the fix
is proven to generalize, not special-cased to two hardcoded names.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.config import Settings
from app.models.intent import IntentResult, MetricSpec
from app.services.dashboard_review import (
    extraction_notices,
    find_unmatched_metrics,
    find_unresolved_ratio_metrics,
    intent_to_tables,
    tables_to_intent,
)
from app.services.intent_parser import IntentParser
from app.services.pbix_builder import PBIXBuilder

SAMPLE_COLUMNS = ["Date", "Region", "Product_Category", "Revenue", "Profit", "Orders"]


def _builder(tmp_path) -> PBIXBuilder:
    settings = Settings()
    settings.output_dir = tmp_path
    settings.upload_dir = tmp_path / "uploads"
    settings.sample_data_dir = tmp_path / "sample"
    settings.ensure_directories()
    return PBIXBuilder(settings=settings)


def _sample_data() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=6, freq="MS"),
            "Region": ["East", "West", "North", "South", "East", "West"],
            "Product_Category": ["A", "B", "A", "B", "A", "B"],
            "Revenue": [15000, 17000, 12000, 19500, 13500, 21000],
            "Profit": [3000, 3400, 1800, 4200, 2100, 4800],
            "Orders": [120, 135, 90, 150, 110, 160],
        }
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_metric_spec_accepts_numerator_and_denominator_columns():
    metric = MetricSpec(name="Profit Margin %", type="percentage", numerator_column="Profit", denominator_column="Revenue")
    assert metric.numerator_column == "Profit"
    assert metric.denominator_column == "Revenue"
    assert metric.source_column is None


def test_metric_spec_simple_path_unaffected():
    """Plain sum/count/average metrics must behave exactly as before --
    numerator/denominator just default to None and don't interfere."""
    metric = MetricSpec(name="Total Revenue", type="sum", source_column="Revenue")
    assert metric.source_column == "Revenue"
    assert metric.numerator_column is None
    assert metric.denominator_column is None


# ---------------------------------------------------------------------------
# pbix_builder ratio resolution + DAX
# ---------------------------------------------------------------------------


def test_is_ratio_metric_detects_type_and_populated_columns(tmp_path):
    builder = _builder(tmp_path)
    assert builder._is_ratio_metric(MetricSpec(name="X", type="percentage")) is True
    assert builder._is_ratio_metric(MetricSpec(name="X", type="ratio")) is True
    assert builder._is_ratio_metric(MetricSpec(name="Average Order Value", type="average", numerator_column="Revenue", denominator_column="Orders")) is True
    assert builder._is_ratio_metric(MetricSpec(name="Total Revenue", type="sum", source_column="Revenue")) is False
    assert builder._is_ratio_metric(MetricSpec(name="Average Discount", type="average", source_column="Discount")) is False


@pytest.mark.parametrize(
    "name,metric_type,numerator,denominator,expected_num_col,expected_den_col",
    [
        ("Profit Margin %", "percentage", "Profit", "Revenue", "Profit", "Revenue"),
        ("Average Order Value", "average", "Revenue", "Orders", "Revenue", "Orders"),
        ("Cost per Order", "ratio", "Profit", "Orders", "Profit", "Orders"),
    ],
)
def test_resolve_ratio_columns_matches_real_data(tmp_path, name, metric_type, numerator, denominator, expected_num_col, expected_den_col):
    builder = _builder(tmp_path)
    metric = MetricSpec(name=name, type=metric_type, numerator_column=numerator, denominator_column=denominator)
    resolved_num, resolved_den = builder._resolve_ratio_columns(metric, _sample_data())
    assert resolved_num == expected_num_col
    assert resolved_den == expected_den_col


def test_resolve_ratio_columns_returns_none_for_hallucinated_names(tmp_path):
    """A suggested column that doesn't exist must not silently fall back to
    an unrelated real column -- that would compute a misleading ratio."""
    builder = _builder(tmp_path)
    metric = MetricSpec(name="Customer Retention Rate", type="percentage", numerator_column="RetainedCustomers", denominator_column="TotalCustomers")
    resolved_num, resolved_den = builder._resolve_ratio_columns(metric, _sample_data())
    assert resolved_num is None
    assert resolved_den is None


def test_generate_ratio_dax_uses_divide_with_zero_fallback(tmp_path):
    builder = _builder(tmp_path)
    expression = builder.generate_ratio_dax(table="FactData", numerator_column="Profit", denominator_column="Revenue", base_measure="Profit Margin %")
    assert expression.startswith("DIVIDE(SUM(FactData[Profit]), SUM(FactData[Revenue]), 0)")
    assert "/" not in expression.split("\n")[0]  # DIVIDE, not raw division


def test_build_dax_measures_generates_divide_for_multiple_ratio_metrics(tmp_path):
    """Covers 4 distinct ratio metrics against the same dataset -- not just
    the original two -- proving the fix generalizes."""
    builder = _builder(tmp_path)
    data = _sample_data()
    intent = IntentResult(
        metrics=[
            MetricSpec(name="Profit Margin %", type="percentage", numerator_column="Profit", denominator_column="Revenue"),
            MetricSpec(name="Average Order Value", type="average", numerator_column="Revenue", denominator_column="Orders"),
            MetricSpec(name="Cost per Order", type="ratio", numerator_column="Profit", denominator_column="Orders"),
            MetricSpec(name="Total Revenue", type="sum", source_column="Revenue"),  # simple metric, unaffected
        ]
    )
    measures = builder.build_dax_measures(intent, data_frame=data)
    by_name = {m.name: m for m in measures}

    assert "DIVIDE(SUM(FactData[Profit]), SUM(FactData[Revenue]), 0)" in by_name["Profit Margin %"].expression
    assert by_name["Profit Margin %"].format_hint == "percentage"

    assert "DIVIDE(SUM(FactData[Revenue]), SUM(FactData[Orders]), 0)" in by_name["Average Order Value"].expression
    assert by_name["Average Order Value"].format_hint == "currency"

    assert "DIVIDE(SUM(FactData[Profit]), SUM(FactData[Orders]), 0)" in by_name["Cost per Order"].expression

    # The plain sum metric must be completely unaffected by the ratio path.
    assert by_name["Total Revenue"].expression == "SUM(FactData[Revenue])\n// base_measure: Total Revenue"


# ---------------------------------------------------------------------------
# dashboard_review: table schema, unmatched detection, notices
# ---------------------------------------------------------------------------


def test_metrics_table_has_numerator_denominator_columns():
    intent = IntentResult(
        metrics=[MetricSpec(name="Profit Margin %", type="percentage", numerator_column="Profit", denominator_column="Revenue")]
    )
    metrics_df, _, _ = intent_to_tables(intent)
    assert "Numerator Column" in metrics_df.columns
    assert "Denominator Column" in metrics_df.columns
    assert metrics_df.iloc[0]["Numerator Column"] == "Profit"
    assert metrics_df.iloc[0]["Denominator Column"] == "Revenue"
    # A ratio metric has no meaningful single Source Column.
    assert metrics_df.iloc[0]["Source Column"] == ""


def test_tables_to_intent_round_trips_ratio_metric_edits():
    base_intent = IntentResult(
        metrics=[MetricSpec(name="Average Order Value", type="average", numerator_column="Revenue", denominator_column="")]
    )
    metrics_df, dimensions_df, visuals_df = intent_to_tables(base_intent)
    # User fills in the denominator the extractor couldn't resolve.
    metrics_df.loc[0, "Denominator Column"] = "Orders"

    edited = tables_to_intent(base_intent, metrics_df, dimensions_df, visuals_df)
    assert edited.metrics[0].numerator_column == "Revenue"
    assert edited.metrics[0].denominator_column == "Orders"


def test_find_unmatched_metrics_excludes_ratio_metrics(tmp_path):
    """Ratio metrics must never be flagged by the single-source_column
    checker -- there is no correct single Source Column for them."""
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[MetricSpec(name="Profit Margin %", type="percentage", numerator_column="Profit", denominator_column="Revenue")]
    )
    assert find_unmatched_metrics(intent, _sample_data(), builder) == []


@pytest.mark.parametrize(
    "name,metric_type,numerator,denominator",
    [
        ("Profit Margin %", "percentage", "Profit", "Revenue"),
        ("Average Order Value", "average", "Revenue", "Orders"),
        ("Return Rate", "percentage", "Profit", "Orders"),  # arbitrary but real columns
    ],
)
def test_find_unresolved_ratio_metrics_empty_when_both_sides_resolve(tmp_path, name, metric_type, numerator, denominator):
    builder = _builder(tmp_path)
    intent = IntentResult(metrics=[MetricSpec(name=name, type=metric_type, numerator_column=numerator, denominator_column=denominator)])
    assert find_unresolved_ratio_metrics(intent, _sample_data(), builder) == []


def test_find_unresolved_ratio_metrics_identifies_missing_denominator(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[MetricSpec(name="Average Order Value", type="average", numerator_column="Revenue", denominator_column="OrderCount")]
    )
    unresolved = find_unresolved_ratio_metrics(intent, _sample_data(), builder)
    assert len(unresolved) == 1
    assert unresolved[0]["name"] == "Average Order Value"
    assert unresolved[0]["missing_side"] == "denominator"
    assert unresolved[0]["resolved_numerator"] == "Revenue"


def test_find_unresolved_ratio_metrics_identifies_both_sides_missing(tmp_path):
    """Customer Retention Rate against a dataset with no customer columns at
    all -- the genuinely-unresolvable case."""
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[
            MetricSpec(
                name="Customer Retention Rate",
                type="percentage",
                numerator_column="RetainedCustomers",
                denominator_column="TotalCustomers",
            )
        ]
    )
    unresolved = find_unresolved_ratio_metrics(intent, _sample_data(), builder)
    assert len(unresolved) == 1
    assert unresolved[0]["missing_side"] == "both"


def test_extraction_notice_names_the_specific_missing_side(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[MetricSpec(name="Average Order Value", type="average", numerator_column="Revenue", denominator_column="OrderCount")]
    )
    notices = extraction_notices(intent, _sample_data(), builder)
    assert any("denominator" in notice.lower() and "average order value" in notice.lower() for notice in notices)
    # Must not just say the old generic "couldn't confidently match" message.
    assert not any("please fix the source column" in notice.lower() for notice in notices)


def test_extraction_notice_for_fully_unresolvable_ratio_metric(tmp_path):
    builder = _builder(tmp_path)
    intent = IntentResult(
        metrics=[
            MetricSpec(
                name="Customer Retention Rate",
                type="percentage",
                numerator_column="RetainedCustomers",
                denominator_column="TotalCustomers",
            )
        ]
    )
    notices = extraction_notices(intent, _sample_data(), builder)
    assert any("customer retention rate" in notice.lower() and "neither side" in notice.lower() for notice in notices)


# ---------------------------------------------------------------------------
# intent_parser fallback heuristic: multiple ratio metric names, generalized
# ---------------------------------------------------------------------------


def _profile() -> dict:
    return {"columns": SAMPLE_COLUMNS, "row_count": 6, "sample_columns": SAMPLE_COLUMNS}


@pytest.mark.parametrize(
    "prompt_text,expected_name",
    [
        ("show me profit margin % by region", "Profit Margin %"),
        ("what is the average order value", "Average Order Value"),
        ("track the return rate for orders", "Return Rate"),
        ("report cost per order", "Cost per Order"),
    ],
)
def test_fallback_heuristic_recognizes_multiple_ratio_metric_names(prompt_text, expected_name):
    """Four distinct ratio metric phrasings against the same 6-column sample
    dataset -- proves the fix isn't special-cased to just the original two
    (Profit Margin %, Average Order Value)."""
    parser = IntentParser(provider="gemini")
    metrics = parser._infer_ratio_metrics(prompt_text, _profile())
    matches = [m for m in metrics if m.name == expected_name]
    assert matches, f"Expected a {expected_name!r} metric to be inferred from: {prompt_text!r}"


def test_fallback_heuristic_fully_resolves_profit_margin_and_aov_on_the_sample_dataset():
    """The two metrics that originally surfaced the bug: both sides resolve
    cleanly against the real 6-column dataset."""
    parser = IntentParser(provider="gemini")

    margin = next(
        m for m in parser._infer_ratio_metrics("profit margin % and average order value", _profile()) if m.name == "Profit Margin %"
    )
    assert margin.numerator_column == "profit"
    assert margin.denominator_column == "revenue"

    aov = next(
        m for m in parser._infer_ratio_metrics("profit margin % and average order value", _profile()) if m.name == "Average Order Value"
    )
    assert aov.numerator_column == "revenue"
    assert aov.denominator_column == "orders"


def test_fallback_heuristic_partially_resolves_when_only_one_side_has_a_real_column():
    """Return Rate / Cost per Order against this dataset: Orders exists (so
    the denominator resolves) but there's no Return/Cost column, so the
    numerator is correctly left unresolved rather than guessed."""
    parser = IntentParser(provider="gemini")

    return_rate = next(m for m in parser._infer_ratio_metrics("what is our return rate", _profile()) if m.name == "Return Rate")
    assert return_rate.numerator_column is None  # no "return"/"refund" column in this dataset
    assert return_rate.denominator_column == "orders"

    cost_per_order = next(m for m in parser._infer_ratio_metrics("report cost per order", _profile()) if m.name == "Cost per Order")
    assert cost_per_order.numerator_column is None  # no "cost"/"expense" column in this dataset
    assert cost_per_order.denominator_column == "orders"


def test_fallback_heuristic_leaves_unresolvable_sides_none_not_guessed():
    """Customer Retention Rate against a dataset with no customer-related
    columns -- the heuristic must not guess an unrelated column."""
    parser = IntentParser(provider="gemini")
    metrics = parser._infer_ratio_metrics("what is our customer retention rate", _profile())
    matches = [m for m in metrics if m.name == "Customer Retention Rate"]
    assert matches
    assert matches[0].numerator_column is None
    assert matches[0].denominator_column is None
