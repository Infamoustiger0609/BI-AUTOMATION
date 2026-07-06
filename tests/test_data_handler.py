from __future__ import annotations

import pandas as pd

from app.models.intent import DimensionSpec, IntentResult, MetricSpec
from app.services.data_handler import DataHandler


def test_data_handler_reads_csv(tmp_path):
    file_path = tmp_path / "sample.csv"
    pd.DataFrame({"A": [1, 2], "B": ["x", "y"]}).to_csv(file_path, index=False)

    handler = DataHandler()
    frame = handler.read_dataframe(file_path)

    assert list(frame.columns) == ["A", "B"]
    assert len(frame) == 2


def test_generate_sample_data_from_intent_with_dynamic_metric_and_dimension():
    handler = DataHandler()
    intent = IntentResult(
        dashboard_title="Warehouse Dashboard",
        prompt_variant="operations",
        metrics=[MetricSpec(name="Units Sold", type="sum", source_column="Units Sold")],
        dimensions=[DimensionSpec(name="Warehouse", type="categorical")],
    )

    frame = handler.generate_sample_data_from_intent(intent, rows=12)

    assert len(frame) == 12
    assert "Warehouse" in frame.columns
    assert "Units_Sold" in frame.columns
    assert pd.api.types.is_numeric_dtype(frame["Units_Sold"])
    assert frame["Warehouse"].nunique() >= 2


def test_generate_sample_data_from_intent_supports_date_grain():
    handler = DataHandler()
    intent = IntentResult(
        dashboard_title="Margin Dashboard",
        prompt_variant="financial",
        time_grain="monthly",
        metrics=[MetricSpec(name="Profit Margin", type="percentage", source_column="Profit Margin")],
        dimensions=[DimensionSpec(name="Product Category", type="categorical")],
    )

    frame = handler.generate_sample_data_from_intent(intent, rows=10)

    assert "Product_Category" in frame.columns
    assert "Date" in frame.columns
    assert pd.api.types.is_datetime64_any_dtype(frame["Date"])
    assert "Profit_Margin" in frame.columns


def test_generate_sample_data_from_intent_uses_date_dimension_name():
    handler = DataHandler()
    intent = IntentResult(
        dashboard_title="Inventory Dashboard",
        metrics=[MetricSpec(name="Inventory Levels", type="sum", source_column="Inventory Levels")],
        dimensions=[DimensionSpec(name="Warehouse Location", type="date", grain="daily")],
    )

    frame = handler.generate_sample_data_from_intent(intent, rows=8)

    assert "Warehouse_Location" in frame.columns
    assert pd.api.types.is_datetime64_any_dtype(frame["Warehouse_Location"])
    assert "Inventory_Levels" in frame.columns


def test_generate_sample_data_from_intent_fallback_schema():
    handler = DataHandler()
    intent = IntentResult(dashboard_title="Fallback Dashboard")

    frame = handler.generate_sample_data_from_intent(intent, rows=6)

    assert list(frame.columns) == ["Date", "Category", "Value"]
    assert pd.api.types.is_datetime64_any_dtype(frame["Date"])
    assert pd.api.types.is_numeric_dtype(frame["Value"])
