from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest

from app.config import Settings
from app.models.intent import DimensionSpec, IntentResult, MetricSpec, VisualSpec
from app.services.pbix_builder import PBIXBuilder


@dataclass
class FakeBackendBuilder:
    name: str
    tables: list[dict] = field(default_factory=list)
    measures: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    saved_path: Path | None = None

    def add_table(self, name, columns, rows=None, hidden=False, source_csv=None, source_db=None, mode="import"):
        self.tables.append(
            {
                "name": name,
                "columns": columns,
                "rows": rows or [],
                "hidden": hidden,
                "source_csv": source_csv,
                "source_db": source_db,
                "mode": mode,
            }
        )

    def add_measure(self, table, name, expression, description="", format_string=None):
        self.measures.append(
            {
                "table": table,
                "name": name,
                "expression": expression,
                "description": description,
                "format_string": format_string,
            }
        )

    def add_relationship(self, from_table, from_column, to_table, to_column):
        self.relationships.append(
            {
                "from_table": from_table,
                "from_column": from_column,
                "to_table": to_table,
                "to_column": to_column,
            }
        )

    def add_page(self, name, visuals=None):
        self.pages.append({"name": name, "visuals": visuals or []})

    def save(self, output_path):
        self.saved_path = Path(output_path)
        self.saved_path.write_bytes(b"FAKE PBIX ARTIFACT")


def build_builder(tmp_path: Path) -> tuple[PBIXBuilder, FakeBackendBuilder]:
    backend = FakeBackendBuilder(name="Test Dashboard")
    settings = Settings()
    settings.output_dir = tmp_path
    settings.upload_dir = tmp_path / "uploads"
    settings.sample_data_dir = tmp_path / "sample"
    builder = PBIXBuilder(
        settings=settings,
        backend_factory=lambda name: backend,
    )
    return builder, backend


def dynamic_intent(
    dashboard_title: str = "Operations Dashboard",
    metric_name: str = "Units Sold",
    dimension_name: str = "Warehouse",
    date_name: str | None = None,
    time_grain: str = "unknown",
    prompt_variant: str = "operations",
) -> IntentResult:
    dimensions = [DimensionSpec(name=dimension_name, type="categorical")]
    if date_name is not None:
        dimensions.append(DimensionSpec(name=date_name, type="date", grain=time_grain if time_grain != "unknown" else "monthly"))
    return IntentResult(
        dashboard_title=dashboard_title,
        prompt_variant=prompt_variant,
        time_grain=time_grain,
        metrics=[MetricSpec(name=metric_name, type="sum", source_column=metric_name)],
        dimensions=dimensions,
        visuals=[
            VisualSpec(type="bar_chart", metric=metric_name, dimension=dimension_name),
        ],
    )


def test_generate_simple_dashboard_with_one_table_and_one_chart(tmp_path):
    builder, backend = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Warehouse Snapshot",
        metric_name="Units Sold",
        dimension_name="Warehouse",
        prompt_variant="operations",
    )
    data = pd.DataFrame(
        {
            "Warehouse": ["North", "South", "North", "West"],
            "Units_Sold": [100, 120, 90, 160],
        }
    )

    output_path = builder.create_pbix(intent, data_frame=data, output_name="simple_dashboard")

    assert output_path.exists()
    assert output_path.suffix == ".pbix"
    assert len(backend.tables) >= 2
    assert len(backend.measures) >= 1
    assert len(backend.pages) == 3


def test_generate_complex_dashboard_with_multiple_tables_and_relationships(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Inventory Operations",
        metric_name="Inventory Levels",
        dimension_name="Location",
        date_name="ReportDate",
        time_grain="monthly",
        prompt_variant="operations",
    )
    data = pd.DataFrame(
        {
            "ReportDate": pd.date_range("2025-01-01", periods=6, freq="D"),
            "Location": ["North", "South", "North", "West", "East", "South"],
            "ProductCategory": ["A", "B", "C", "A", "B", "C"],
            "CustomerSegment": ["Retail", "Retail", "Wholesale", "Wholesale", "Retail", "Wholesale"],
            "Inventory_Levels": [100, 200, 150, 300, 180, 220],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    assert len(model.tables) >= 3
    assert any(table.name == "FactData" for table in model.tables)
    assert any(table.table_role == "dimension" for table in model.tables)
    assert model.relationships
    assert len(model.relationships) >= 1
    fact_table = next(t for t in model.tables if t.name == "FactData")
    assert any(col.name == "ReportDate" for col in fact_table.columns)



def test_generate_dashboard_from_csv_file(tmp_path):
    builder, _ = build_builder(tmp_path)
    csv_path = tmp_path / "inventory.csv"
    pd.DataFrame(
        {
            "ReportDate": pd.date_range("2025-01-01", periods=3, freq="D"),
            "Location": ["North", "South", "North"],
            "Inventory_Levels": [10, 20, 30],
        }
    ).to_csv(csv_path, index=False)

    model = builder.build_data_model(
        dynamic_intent(
            dashboard_title="Inventory CSV",
            metric_name="Inventory Levels",
            dimension_name="Location",
            date_name="ReportDate",
            time_grain="daily",
        ),
        source_path=csv_path,
    )

    assert model.source_kind == "upload"
    assert model.tables[0].source_csv == str(csv_path.resolve())
    assert model.data_profile["row_count"] == 3


def test_generate_dashboard_with_time_intelligence(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Financial Snapshot",
        metric_name="Profit Margin",
        dimension_name="Product Category",
        date_name="ReportDate",
        time_grain="monthly",
        prompt_variant="financial",
    )
    data = pd.DataFrame(
        {
            "ReportDate": pd.date_range("2025-01-01", periods=5, freq="MS"),
            "Product_Category": ["A", "B", "C", "A", "B"],
            "Profit_Margin": [0.10, 0.12, 0.09, 0.15, 0.18],
        }
    )

    measures = builder.build_dax_measures(intent, data_frame=data)
    expressions = {measure.name: measure.expression for measure in measures}

    assert "TOTALYTD" in expressions["YTD"]
    assert "TOTALQTD" in expressions["QTD"]
    assert "TOTALMTD" in expressions["MTD"]
    assert "SAMEPERIODLASTYEAR" in expressions["YoY % Change"]
    assert "DATEADD" in expressions["MoM % Change"]
    assert "ReportDate" in expressions["YTD"]


def test_generate_dashboard_with_multiple_pages(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="People Dashboard",
        metric_name="Employee Count",
        dimension_name="Department",
        date_name="ReportDate",
        time_grain="monthly",
        prompt_variant="hr",
    )
    data = pd.DataFrame(
        {
            "ReportDate": pd.date_range("2025-01-01", periods=8, freq="D"),
            "Department": ["HR", "Sales", "Ops", "Finance", "HR", "Sales", "Ops", "Finance"],
            "Employee_Count": [100, 120, 130, 110, 160, 180, 175, 190],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    assert len(model.pages) == 4
    assert model.pages[0].name == "Executive Summary"
    assert model.pages[1].visuals
    assert model.pages[2].visuals
    assert model.pages[3].visuals


def test_dax_generation_variants(tmp_path):
    builder, _ = build_builder(tmp_path)

    assert builder.generate_dax("sum", "FactData", "Units_Sold") == "SUM(FactData[Units_Sold])"
    assert builder.generate_dax("average", "FactData", "Units_Sold") == "AVERAGE(FactData[Units_Sold])"
    assert builder.generate_dax("distinct_count", "FactData", "CustomerID") == "DISTINCTCOUNT(FactData[CustomerID])"
    assert "TOTALYTD" in builder.generate_dax("ytd", "FactData", "Units_Sold", date_table="DimReportDate", date_column="ReportDate")
    assert "DIVIDE" in builder.generate_dax("target_vs_actual", "FactData", "Units_Sold", target="Budget")


def test_no_upload_fallback_builds_schema_from_intent(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Inventory Fallback",
        metric_name="Inventory Levels",
        dimension_name="Warehouse",
        time_grain="monthly",
    )

    model = builder.build_data_model(intent)

    fact_table = next(table for table in model.tables if table.name == "FactData")
    assert "Warehouse" in {column.name for column in fact_table.columns}
    assert "Inventory_Levels" in {column.name for column in fact_table.columns}
    assert any(row.get("Warehouse") for row in fact_table.rows)
    assert any(rel.to_table.startswith("Dim") for rel in model.relationships)


def test_date_dimension_detection_on_nonstandard_column_name(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Logistics Dashboard",
        metric_name="Shipments",
        dimension_name="Warehouse",
        date_name="transaction_day",
        time_grain="daily",
        prompt_variant="operations",
    )
    data = pd.DataFrame(
        {
            "transaction_day": pd.date_range("2025-01-01", periods=5, freq="D"),
            "Warehouse": ["A", "B", "A", "C", "B"],
            "Shipments": [12, 15, 14, 20, 18],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    fact_table = next(t for t in model.tables if t.name == "FactData")
    assert any(col.name == "transaction_day" for col in fact_table.columns)
    assert any(rel.to_column == "transaction_day" for rel in model.relationships)


def test_date_dimension_is_contiguous_even_when_source_dates_have_gaps(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Sparse Shipments",
        metric_name="Shipments",
        dimension_name="Warehouse",
        date_name="OrderDate",
        time_grain="daily",
        prompt_variant="operations",
    )
    # Sparse: only every third day, spanning parts of two months, to mimic
    # real uploaded data with missing days (e.g. no weekend records).
    sparse_dates = [pd.Timestamp("2025-01-05") + pd.Timedelta(days=3 * i) for i in range(10)]
    data = pd.DataFrame(
        {
            "OrderDate": sparse_dates,
            "Warehouse": ["A", "B"] * 5,
            "Shipments": list(range(10)),
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    date_dim = next(
        t for t in model.tables if t.table_role == "dimension" and any(c.name == "OrderDate" for c in t.columns)
    )
    dim_dates = pd.to_datetime(sorted(row["OrderDate"] for row in date_dim.rows))

    # The dimension must cover every day in the spanned months, not just the
    # sparse dates that happened to appear in the fact table -- DAX
    # time-intelligence functions (TOTALYTD, SAMEPERIODLASTYEAR, DATEADD)
    # require a gap-free date table to compute correctly.
    assert dim_dates[0] == pd.Timestamp("2025-01-01")
    assert dim_dates[-1] == pd.Timestamp("2025-02-28")
    assert (dim_dates.to_series().diff().dropna() == pd.Timedelta(days=1)).all()
    for fact_date in sparse_dates:
        assert fact_date.normalize() in dim_dates


def test_categorical_dimension_selection_from_low_cardinality_columns(tmp_path):
    builder, _ = build_builder(tmp_path)
    intent = IntentResult(
        dashboard_title="Support Dashboard",
        prompt_variant="operations",
        metrics=[MetricSpec(name="Ticket Count", type="count", source_column="Ticket Count")],
        dimensions=[],
    )
    data = pd.DataFrame(
        {
            "Team": ["A", "B", "A", "C", "B", "A"],
            "Ticket Count": [1, 2, 3, 4, 5, 6],
            "Priority": ["High", "High", "Low", "Low", "Medium", "Low"],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    assert any(table.name == "DimTeam" for table in model.tables)
    assert any(table.name == "DimPriority" for table in model.tables)


def test_date_dimension_values_match_fact_table_representation(tmp_path):
    """Regression test: DimDate previously stored its date key as a formatted
    string (e.g. "2025-01-01") while FactData embeds the same column as
    pandas.Timestamp objects (via DataFrame.to_dict()). The type mismatch
    meant pbix-mcp's FK/orphan check never matched a single fact row against
    the dimension, even though every fact date was logically covered."""

    builder, _ = build_builder(tmp_path)
    intent = dynamic_intent(
        dashboard_title="Margin Dashboard",
        metric_name="Margin",
        dimension_name="Category",
        date_name="Date",
        time_grain="monthly",
        prompt_variant="financial",
    )
    dates = pd.date_range("2025-01-01", periods=50, freq="MS")
    data = pd.DataFrame(
        {
            "Date": dates,
            "Category": ["A", "B"] * 25,
            "Margin": list(range(50)),
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    fact_table = next(t for t in model.tables if t.name == "FactData")
    date_dim = next(
        t for t in model.tables if t.table_role == "dimension" and any(c.name == "Date" for c in t.columns)
    )

    fact_dates = {row["Date"] for row in fact_table.rows}
    dim_dates = {row["Date"] for row in date_dim.rows}

    assert all(isinstance(value, pd.Timestamp) for value in fact_dates)
    assert all(isinstance(value, pd.Timestamp) for value in dim_dates)
    assert not (fact_dates - dim_dates)  # every fact date resolves against the dimension


def test_pbix_validation_rejects_bad_file_extension(tmp_path):
    builder, _ = build_builder(tmp_path)
    with pytest.raises(ValueError):
        builder.validate_generated_pbix(tmp_path / "bad.txt")


def _assert_no_overlaps_and_in_bounds(builder: PBIXBuilder, model) -> None:
    for page in model.pages:
        rects = [(v.x, v.y, v.width, v.height) for v in page.visuals]
        for x, y, width, height in rects:
            assert x >= 0 and y >= 0, f"page '{page.name}' visual off top/left edge: {(x, y, width, height)}"
            assert x + width <= builder.PAGE_WIDTH, f"page '{page.name}' visual runs off right edge: {(x, y, width, height)}"
            assert y + height <= builder.PAGE_HEIGHT, f"page '{page.name}' visual runs off bottom edge: {(x, y, width, height)}"
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert not builder._rects_overlap(rects[i], rects[j]), (
                    f"page '{page.name}' visuals overlap: {rects[i]} and {rects[j]}"
                )


def test_executive_summary_has_one_card_per_metric_not_duplicates(tmp_path):
    """Regression test for the reported bug: Executive Summary used to show
    exactly 3 KPI cards, all bound to the same (first) metric, even when the
    prompt requested 5 distinct KPIs."""

    builder, _ = build_builder(tmp_path)
    metric_names = ["Total Revenue", "Total Profit", "Profit Margin %", "Number of Orders", "Average Order Value"]
    intent = IntentResult(
        dashboard_title="Sales Performance Dashboard",
        prompt_variant="sales",
        time_grain="monthly",
        metrics=[MetricSpec(name=name, type="sum", source_column=name) for name in metric_names],
        dimensions=[
            DimensionSpec(name="Region", type="categorical"),
            DimensionSpec(name="Product Category", type="categorical"),
            DimensionSpec(name="Date", type="date", grain="monthly"),
        ],
        visuals=[
            VisualSpec(type="line_chart", metric="Total Revenue", dimension="Date", title="Monthly Revenue Trend"),
            VisualSpec(type="bar_chart", metric="Total Revenue", dimension="Region", title="Revenue by Region"),
        ],
    )
    data = pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=12, freq="MS"),
            "Region": ["East", "West", "North", "South"] * 3,
            "Product_Category": ["A", "B"] * 6,
            "Total_Revenue": [1000 + i * 50 for i in range(12)],
            "Total_Profit": [200 + i * 10 for i in range(12)],
            "Profit_Margin": [0.2 + (i % 5) / 100 for i in range(12)],
            "Number_of_Orders": [10 + i for i in range(12)],
            "Average_Order_Value": [100 + i for i in range(12)],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)
    exec_page = next(page for page in model.pages if page.name == "Executive Summary")
    cards = [visual for visual in exec_page.visuals if visual.visual_type == "card"]

    assert len(cards) == 5
    bound_metrics = {card.config["measure"] for card in cards}
    assert bound_metrics == set(metric_names)  # 5 distinct metrics, not 5 copies of the first
    # Executive Summary must also have a supporting chart, not just cards.
    assert any(visual.visual_type != "card" for visual in exec_page.visuals)


def test_intent_visuals_drive_trend_and_breakdown_pages_with_correct_bindings(tmp_path):
    """Verifies the exact scenario from the task: 5 KPIs, a line chart by
    Date, and a bar chart by Region, all correctly bound -- not just the
    generic single-metric template."""

    builder, _ = build_builder(tmp_path)
    metric_names = ["Total Revenue", "Total Profit", "Profit Margin %", "Number of Orders", "Average Order Value"]
    intent = IntentResult(
        dashboard_title="Sales Performance Dashboard",
        prompt_variant="sales",
        time_grain="monthly",
        metrics=[MetricSpec(name=name, type="sum", source_column=name) for name in metric_names],
        dimensions=[
            DimensionSpec(name="Region", type="categorical"),
            DimensionSpec(name="Product Category", type="categorical"),
            DimensionSpec(name="Date", type="date", grain="monthly"),
        ],
        visuals=[
            VisualSpec(type="line_chart", metric="Total Revenue", dimension="Date", title="Monthly Revenue Trend"),
            VisualSpec(type="bar_chart", metric="Total Revenue", dimension="Region", title="Revenue by Region"),
        ],
    )
    data = pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=12, freq="MS"),
            "Region": ["East", "West", "North", "South"] * 3,
            "Product_Category": ["A", "B"] * 6,
            "Total_Revenue": [1000 + i * 50 for i in range(12)],
            "Total_Profit": [200 + i * 10 for i in range(12)],
            "Profit_Margin": [0.2 + (i % 5) / 100 for i in range(12)],
            "Number_of_Orders": [10 + i for i in range(12)],
            "Average_Order_Value": [100 + i for i in range(12)],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    trend_page = next(page for page in model.pages if page.name == "Trend Analysis")
    line_visuals = [v for v in trend_page.visuals if v.visual_type == "lineChart"]
    assert any(
        v.config["measure"] == "Total Revenue" and v.config["category"]["column"] == "Date" for v in line_visuals
    )

    breakdown_page = next(page for page in model.pages if page.name == "Breakdown Analysis")
    bar_visuals = [v for v in breakdown_page.visuals if v.visual_type == "clusteredColumnChart"]
    assert any(
        v.config["measure"] == "Total Revenue" and v.config["category"]["column"] == "Region" for v in bar_visuals
    )

    # Every metric mentioned in the prompt ends up on some page.
    exec_page = next(page for page in model.pages if page.name == "Executive Summary")
    exec_metrics = {v.config["measure"] for v in exec_page.visuals if v.visual_type == "card"}
    assert exec_metrics == set(metric_names)

    # No visual references a missing table/column/measure.
    builder._validate_model(model)  # raises if anything is broken

    # No two visuals overlap, none run off the 1280x720 canvas, on any page.
    _assert_no_overlaps_and_in_bounds(builder, model)


def test_fallback_path_with_empty_intent_visuals_still_covers_multiple_metrics(tmp_path):
    """When intent.visuals is empty (sparse prompt / heuristic fallback),
    Trend and Breakdown pages must still cover more than just the first
    metric, not just Executive Summary."""

    builder, _ = build_builder(tmp_path)
    intent = IntentResult(
        dashboard_title="Ops Dashboard",
        prompt_variant="operations",
        time_grain="monthly",
        metrics=[
            MetricSpec(name="Shipments", type="sum", source_column="Shipments"),
            MetricSpec(name="Backlog", type="sum", source_column="Backlog"),
        ],
        dimensions=[
            DimensionSpec(name="Warehouse", type="categorical"),
            DimensionSpec(name="Date", type="date", grain="monthly"),
        ],
        visuals=[],  # empty -- must fall back to the generic auto-template
    )
    data = pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=6, freq="MS"),
            "Warehouse": ["A", "B"] * 3,
            "Shipments": [10, 20, 30, 40, 50, 60],
            "Backlog": [5, 6, 7, 8, 9, 10],
        }
    )

    model = builder.build_data_model(intent, data_frame=data)

    trend_page = next(page for page in model.pages if page.name == "Trend Analysis")
    trend_metrics = {v.config["measure"] for v in trend_page.visuals}
    assert trend_metrics == {"Shipments", "Backlog"}

    breakdown_page = next(page for page in model.pages if page.name == "Breakdown Analysis")
    breakdown_metrics = {v.config["measure"] for v in breakdown_page.visuals}
    assert breakdown_metrics == {"Shipments", "Backlog"}

    _assert_no_overlaps_and_in_bounds(builder, model)


def test_grid_layout_never_overlaps_or_overflows_across_various_counts(tmp_path):
    builder, _ = build_builder(tmp_path)
    for count in range(1, 9):
        positions = builder._grid_layout(count, top=20)
        assert len(positions) == count
        for x, y, width, height in positions:
            assert x >= 0 and y >= 0
            assert x + width <= builder.PAGE_WIDTH
            assert y + height <= builder.PAGE_HEIGHT
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                assert not builder._rects_overlap(positions[i], positions[j])


def test_visual_type_dict_key_matches_pbix_mcp_expected_shape(tmp_path):
    """Regression test for the critical bug: asdict(VisualDefinition) produces
    a "visual_type" key, but pbix_mcp.builder.PBIXBuilder._build_layout()
    reads the chart type from a "type" key -- so every chart was silently
    rendering as a plain card. _visual_to_backend_dict must produce "type"."""

    builder, _ = build_builder(tmp_path)
    visual = builder._bar_visual("Revenue_by_Region", "Total Revenue", "FactData", "Region", x=0, y=0, width=100, height=100)
    backend_dict = builder._visual_to_backend_dict(visual)
    assert backend_dict["type"] == "clusteredColumnChart"
    assert "visual_type" not in backend_dict


def test_visual_type_identifiers_match_power_bi_native_names(tmp_path):
    builder, _ = build_builder(tmp_path)
    assert builder._bar_visual("n", "m", "t", "c", 0, 0, 1, 1).visual_type == "clusteredColumnChart"
    assert builder._line_visual("n", "m", "t", "c", 0, 0, 1, 1).visual_type == "lineChart"
    assert builder._area_visual("n", "m", "t", "c", 0, 0, 1, 1).visual_type == "areaChart"
    assert builder._donut_visual("n", "m", "t", "c", 0, 0, 1, 1).visual_type == "donutChart"
    assert builder._waterfall_visual("n", "m", "t", "c", 0, 0, 1, 1).visual_type == "waterfallChart"
    assert builder._card_visual("n", "m", 0, 0, 1, 1).visual_type == "card"


def test_validation_fails_closed_on_unrecognized_visual_type(tmp_path):
    """The catch-all used to `return True` for any unrecognized visual_type,
    which is exactly how the original broken 3-cards-one-metric bug slipped
    past validation. It must now fail closed."""

    from app.services.pbix_builder import VisualDefinition

    builder, _ = build_builder(tmp_path)
    bogus_visual = VisualDefinition(name="Bogus", visual_type="totallyMadeUpType", config={}, x=0, y=0, width=100, height=100)
    assert builder._visual_references_existing_fields(bogus_visual, {"FactData": {"Region"}}, {"Total Revenue"}) is False


def test_format_hint_classification_by_metric_type_and_name(tmp_path):
    builder, _ = build_builder(tmp_path)
    assert builder._infer_format_hint(MetricSpec(name="Total Revenue", type="sum")) == "currency"
    assert builder._infer_format_hint(MetricSpec(name="Average Order Value", type="average")) == "currency"
    assert builder._infer_format_hint(MetricSpec(name="Profit Margin %", type="percentage")) == "percentage"
    assert builder._infer_format_hint(MetricSpec(name="Conversion Rate", type="ratio")) == "percentage"
    assert builder._infer_format_hint(MetricSpec(name="Number of Orders", type="count")) == "whole_number"
    assert builder._infer_format_hint(MetricSpec(name="Widget Score", type="sum")) == "number"


def test_count_metric_with_hallucinated_source_column_falls_back_to_real_data(tmp_path):
    """Regression test for the OrderID bug: a "Number of Orders" KPI came back
    from the parser with source_column="OrderID", but the real uploaded data
    only has an "Orders" column (no OrderID at all). Building the measure
    verbatim against "OrderID" produced DAX referencing a column that doesn't
    exist, which Power BI rejects at open time ("Column 'OrderID' in table
    'FactData' cannot be found"). The resolved column must come from the real
    data, never the hallucinated suggestion, and count-type metrics must fall
    back to COUNTROWS (no column needed at all) when nothing real matches."""

    builder, _ = build_builder(tmp_path)
    intent = IntentResult(
        dashboard_title="Sales Dashboard",
        prompt_variant="sales",
        metrics=[MetricSpec(name="Number of Orders", type="count", source_column="OrderID")],
        dimensions=[DimensionSpec(name="Region", type="categorical")],
    )
    data = pd.DataFrame(
        {
            "Region": ["East", "West", "North"],
            "Orders": [10, 20, 30],  # no OrderID column anywhere in the real data
        }
    )

    measures = builder.build_dax_measures(intent, data_frame=data)
    expression = measures[0].expression

    assert "OrderID" not in expression
    assert "COUNTROWS(FactData)" in expression or "FactData[Orders]" in expression

    # Also confirm the full model builds and validates cleanly end to end --
    # this is exactly the check that would have caught the broken reference
    # before it reached Power BI.
    model = builder.build_data_model(intent, data_frame=data)
    for measure in model.measures:
        assert "OrderID" not in measure.expression
    builder._validate_model(model)


def test_metric_source_column_prefers_real_match_over_hallucinated_suggestion(tmp_path):
    builder, _ = build_builder(tmp_path)
    data = pd.DataFrame({"Region": ["East", "West"], "Revenue": [100, 200]})

    # Suggested column matches a real one (case/format differences aside) -- use it.
    metric_matching = MetricSpec(name="Total Revenue", type="sum", source_column="revenue")
    assert builder._metric_source_column(metric_matching, data) == "Revenue"

    # Suggested column doesn't exist in the real data -- fall back to a real numeric column.
    metric_hallucinated = MetricSpec(name="Total Sales", type="sum", source_column="SalesAmount")
    assert builder._metric_source_column(metric_hallucinated, data) == "Revenue"


def test_measures_carry_format_hint_through_to_backend_when_supported(tmp_path):
    builder, backend = build_builder(tmp_path)
    intent = dynamic_intent(metric_name="Total Revenue", dimension_name="Region")
    data = pd.DataFrame({"Region": ["East", "West"], "Total_Revenue": [100, 200]})

    builder.create_pbix(intent, data_frame=data, output_name="format_hint_test")

    measure = next(m for m in backend.measures if m["name"] == "Total Revenue")
    assert measure["format_string"] == '"$"#,##0.00'
