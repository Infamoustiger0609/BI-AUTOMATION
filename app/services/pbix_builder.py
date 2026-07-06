"""
PBIX file generation using the pbix-mcp library.
Builds complete Power BI dashboards programmatically.
"""

from __future__ import annotations

import logging
import json
import io
import math
import inspect
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import zipfile

import pandas as pd

from app.config import Settings, get_settings
from app.models.intent import (
    DimensionSpec,
    FilterSpec,
    IntentResult,
    MetricSpec,
    RelationshipSpec,
    TableSpec,
    VisualSpec,
)
from app.services.data_handler import DataHandler
from app.utils.helpers import ensure_directory, safe_filename

try:  # pragma: no cover - optional dependency in this environment
    from pbix_mcp.builder import PBIXBuilder as MCPPBIXBuilder
except ImportError:  # pragma: no cover - exercised when dependency is absent
    MCPPBIXBuilder = None  # type: ignore[assignment]


PBIXDataType = str


@dataclass(slots=True)
class ColumnDefinition:
    """A Power BI table column definition."""

    name: str
    data_type: PBIXDataType
    source_column: str | None = None
    is_calculated: bool = False
    expression: str | None = None


@dataclass(slots=True)
class TableDefinition:
    """A table definition passed to pbix-mcp."""

    name: str
    columns: list[ColumnDefinition]
    rows: list[dict[str, Any]] = field(default_factory=list)
    hidden: bool = False
    source_csv: str | None = None
    source_db: dict[str, Any] | None = None
    mode: str = "import"
    m_code: str | None = None
    table_role: str = "fact"
    skip_hierarchy: bool = False


@dataclass(slots=True)
class MeasureDefinition:
    """A DAX measure definition."""

    table: str
    name: str
    expression: str
    description: str = ""
    format_hint: str = "number"


@dataclass(slots=True)
class RelationshipDefinition:
    """A relationship definition between two tables."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "many-to-one"


@dataclass(slots=True)
class DateContext:
    """Resolved date table and column context for dynamic time intelligence."""

    table_name: str
    column_name: str


@dataclass(slots=True)
class VisualDefinition:
    """A report visual definition."""

    name: str
    visual_type: str
    config: dict[str, Any]
    x: int = 20
    y: int = 20
    width: int = 300
    height: int = 200


@dataclass(slots=True)
class PageDefinition:
    """A report page definition."""

    name: str
    visuals: list[VisualDefinition] = field(default_factory=list)
    width: int = 1280
    height: int = 720


@dataclass(slots=True)
class ModelBuildResult:
    """Intermediate result from model generation."""

    tables: list[TableDefinition]
    relationships: list[RelationshipDefinition]
    measures: list[MeasureDefinition]
    pages: list[PageDefinition]
    data_profile: dict[str, Any]
    source_kind: str
    template_name: str


@dataclass(slots=True)
class LocalPBIXBackend:
    """Fallback PBIX backend used when pbix-mcp is unavailable."""

    name: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)

    def add_table(
        self,
        name: str,
        columns: list[dict[str, Any]],
        rows: list[dict[str, Any]] | None = None,
        hidden: bool = False,
        source_csv: str | None = None,
        source_db: dict[str, Any] | None = None,
        mode: str = "import",
        skip_hierarchy: bool = False,
    ) -> "LocalPBIXBackend":
        self.tables.append(
            {
                "name": name,
                "columns": columns,
                "rows": rows or [],
                "hidden": hidden,
                "source_csv": source_csv,
                "source_db": source_db,
                "mode": mode,
                "skip_hierarchy": skip_hierarchy,
            }
        )
        return self

    def add_measure(
        self,
        table: str,
        name: str,
        expression: str,
        description: str = "",
        format_string: str | None = None,
    ) -> "LocalPBIXBackend":
        self.measures.append(
            {
                "table": table,
                "name": name,
                "expression": expression,
                "description": description,
                "format_string": format_string,
            }
        )
        return self

    def add_relationship(
        self,
        from_table: str,
        from_column: str,
        to_table: str,
        to_column: str,
    ) -> "LocalPBIXBackend":
        self.relationships.append(
            {
                "from_table": from_table,
                "from_column": from_column,
                "to_table": to_table,
                "to_column": to_column,
            }
        )
        return self

    def add_page(self, name: str = "Page 1", visuals: list[dict[str, Any]] | None = None) -> "LocalPBIXBackend":
        self.pages.append({"name": name, "visuals": visuals or []})
        return self

    def save(self, output_path: str) -> None:
        payload = {
            "name": self.name,
            "tables": self.tables,
            "measures": self.measures,
            "relationships": self.relationships,
            "pages": self.pages,
            "generated_by": "Prompt2PBI scaffold fallback backend",
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(payload, indent=2, default=str))
            archive.writestr("readme.txt", "This is a scaffold PBIX artifact produced without pbix-mcp.")


class PBIXBuilder:
    """Build Power BI .pbix files from intent and data."""

    TEMPLATE_LIBRARY: dict[str, dict[str, Any]] = {
        "sales": {
            "display_name": "Sales Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
        "financial": {
            "display_name": "Financial Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
        "marketing": {
            "display_name": "Marketing Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
        "hr": {
            "display_name": "HR Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
        "operations": {
            "display_name": "Operations Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
        "general": {
            "display_name": "Prompt2PBI Dashboard",
            "pages": ["Executive Summary", "Trend Analysis", "Breakdown Analysis", "Detail View"],
        },
    }

    MAX_EMBEDDED_ROWS = 5000

    # Real Power BI report-layout visualType identifiers for every chart shape
    # this builder emits (confirmed against pbix-mcp's builder.py). These are
    # the only strings that get a category+measure binding built for them.
    CATEGORY_MEASURE_VISUAL_TYPES = frozenset(
        {"clusteredColumnChart", "lineChart", "areaChart", "donutChart", "waterfallChart"}
    )

    PAGE_WIDTH = 1280
    PAGE_HEIGHT = 720
    PAGE_MARGIN = 20

    EXEC_SUMMARY_MAX_CARDS = 6
    TREND_MAX_METRICS = 3
    BREAKDOWN_MAX_METRICS = 3

    # Auto-generated time-intelligence aggregates (see build_dax_measures) --
    # useful measures to have in the model, but they shouldn't compete with
    # the user's actually-requested KPIs for Executive Summary card slots or
    # Trend/Breakdown chart slots.
    TIME_INTELLIGENCE_MEASURE_NAMES = frozenset({"MTD", "QTD", "YTD", "YoY % Change", "MoM % Change"})

    # Best-effort mapping from the LLM/heuristic-facing VisualSpec.type vocabulary
    # (app/models/intent.py) to the visual types this builder can actually render.
    VISUAL_SPEC_TYPE_MAP: dict[str, str] = {
        "bar_chart": "clusteredColumnChart",
        "column_chart": "clusteredColumnChart",
        "stacked_column_chart": "clusteredColumnChart",
        "combo_chart": "clusteredColumnChart",
        "ribbon_chart": "clusteredColumnChart",
        "line_chart": "lineChart",
        "area_chart": "areaChart",
        "waterfall": "waterfallChart",
        "pie_chart": "donutChart",
        "donut_chart": "donutChart",
        "treemap": "clusteredColumnChart",
        "funnel": "clusteredColumnChart",
        "scatter_chart": "clusteredColumnChart",
        "scatter_plot": "clusteredColumnChart",
        "bubble_chart": "clusteredColumnChart",
        "dot_plot": "clusteredColumnChart",
        "table": "table",
        "matrix": "matrix",
    }

    def __init__(
        self,
        settings: Settings | None = None,
        data_handler: DataHandler | None = None,
        backend_factory: Callable[[str], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.data_handler = data_handler or DataHandler(settings=self.settings)
        self.backend_factory = backend_factory or self._default_backend_factory
        self.logger = logger or logging.getLogger(__name__)
        ensure_directory(self.settings.output_dir)

    def create_pbix(
        self,
        intent: IntentResult,
        data_frame: pd.DataFrame | None = None,
        output_name: str | None = None,
        source_path: str | Path | None = None,
        template_name: str | None = None,
        retries: int = 2,
    ) -> Path:
        """Create a PBIX file from intent and optional data.

        The method prepares a normalized data model, generates DAX measures,
        page layouts, and then delegates the final binary build to pbix-mcp.
        """

        model = self.build_data_model(
            intent=intent,
            data_frame=data_frame,
            source_path=source_path,
            template_name=template_name,
        )
        output_path = self.build_output_path(output_name or intent.dashboard_title)
        backend = self._instantiate_backend(intent.dashboard_title)

        try:
            self._apply_model_to_backend(backend, model)
            self._save_backend(backend, output_path, model, retries=retries)
            validated = self.validate_generated_pbix(output_path)
            self.logger.info(
                "PBIX file created",
                extra={
                    "output_path": str(validated),
                    "table_count": len(model.tables),
                    "measure_count": len(model.measures),
                    "page_count": len(model.pages),
                },
            )
            return validated
        except Exception:
            self.logger.exception(
                "PBIX generation failed",
                extra={"output_path": str(output_path), "template": model.template_name},
            )
            raise

    def build_data_model(
        self,
        intent: IntentResult,
        data_frame: pd.DataFrame | None = None,
        source_path: str | Path | None = None,
        template_name: str | None = None,
    ) -> ModelBuildResult:
        """Generate a star-schema style model from the supplied intent and data."""

        template_key = self._resolve_template_name(intent, template_name)
        source_kind = "upload" if data_frame is not None or source_path else "sample"
        frame = self._resolve_dataframe(data_frame=data_frame, source_path=source_path, intent=intent)
        frame = self._normalize_dataframe(frame)
        profile = self._profile_dataframe(frame)
        date_context = self._resolve_date_context(frame, intent)

        fact_table = self._build_fact_table(frame, intent, source_path=source_path)
        dimension_tables, relationships = self._build_dimension_tables(frame, fact_table, intent)
        date_dimension = self._build_date_dimension(frame, fact_table, intent, date_context=date_context)
        if date_dimension is not None:
            dimension_tables.append(date_dimension)
            relationships.extend(self._date_relationships(fact_table, date_dimension, frame, intent, date_context))

        measures = self.build_dax_measures(intent, fact_table.name, frame, date_context=date_context)
        pages = self.build_page_structure(
            intent,
            template_key,
            fact_table,
            dimension_tables,
            measures,
            date_context=date_context,
        )

        tables = [fact_table, *dimension_tables]
        self._attach_calculated_columns(tables, measures)

        return ModelBuildResult(
            tables=tables,
            relationships=self._dedupe_relationships(relationships),
            measures=measures,
            pages=pages,
            data_profile=profile,
            source_kind=source_kind,
            template_name=template_key,
        )

    def build_dax_measures(
        self,
        intent: IntentResult,
        fact_table_name: str = "FactData",
        data_frame: pd.DataFrame | None = None,
        date_context: DateContext | None = None,
    ) -> list[MeasureDefinition]:
        """Generate DAX measures from the extracted intent data."""

        measures: list[MeasureDefinition] = []
        resolved_date_context = date_context or self._resolve_date_context(data_frame, intent)
        for metric in intent.metrics:
            if self._is_ratio_metric(metric):
                numerator_col, denominator_col = self._resolve_ratio_columns(metric, data_frame)
                expression = self.generate_ratio_dax(
                    table=fact_table_name,
                    numerator_column=numerator_col,
                    denominator_column=denominator_col,
                    filters=intent.filters,
                    base_measure=metric.name,
                )
            else:
                expression = self.generate_dax(
                    metric_type=metric.type,
                    table=fact_table_name,
                    column=self._metric_source_column(metric, data_frame),
                    filters=intent.filters,
                    time_grain=intent.time_grain,
                    base_measure=metric.name,
                    date_table=resolved_date_context.table_name if resolved_date_context else None,
                    date_column=resolved_date_context.column_name if resolved_date_context else None,
                )
            format_hint = self._infer_format_hint(metric)
            base_description = metric.description or f"Auto-generated {metric.type} measure."
            measures.append(
                MeasureDefinition(
                    table=fact_table_name,
                    name=metric.name,
                    expression=expression,
                    description=f"{base_description} (Format: {format_hint.replace('_', ' ').title()})",
                    format_hint=format_hint,
                )
            )

        if not measures:
            measures.append(
                MeasureDefinition(
                    table=fact_table_name,
                    name="Total Count",
                    expression=self.generate_dax("count", fact_table_name, self._fallback_value_column(data_frame)),
                    description="Default count measure generated from the input data.",
                )
            )

        if any(
            dim.type == "date" or dim.grain != "none"
            for dim in intent.dimensions
        ) or intent.time_grain != "unknown":
            base_column = self._fallback_value_column(data_frame)
            measures.extend(
                [
                    MeasureDefinition(
                        table=fact_table_name,
                        name="MTD",
                        expression=self.generate_dax(
                            "mtd",
                            fact_table_name,
                            base_column,
                            date_table=resolved_date_context.table_name if resolved_date_context else None,
                            date_column=resolved_date_context.column_name if resolved_date_context else None,
                        ),
                        description="Month-to-date aggregation.",
                    ),
                    MeasureDefinition(
                        table=fact_table_name,
                        name="QTD",
                        expression=self.generate_dax(
                            "qtd",
                            fact_table_name,
                            base_column,
                            date_table=resolved_date_context.table_name if resolved_date_context else None,
                            date_column=resolved_date_context.column_name if resolved_date_context else None,
                        ),
                        description="Quarter-to-date aggregation.",
                    ),
                    MeasureDefinition(
                        table=fact_table_name,
                        name="YTD",
                        expression=self.generate_dax(
                            "ytd",
                            fact_table_name,
                            base_column,
                            date_table=resolved_date_context.table_name if resolved_date_context else None,
                            date_column=resolved_date_context.column_name if resolved_date_context else None,
                        ),
                        description="Year-to-date aggregation.",
                    ),
                    MeasureDefinition(
                        table=fact_table_name,
                        name="YoY % Change",
                        expression=self.generate_dax(
                            "yoy",
                            fact_table_name,
                            base_column,
                            time_grain=intent.time_grain,
                            date_table=resolved_date_context.table_name if resolved_date_context else None,
                            date_column=resolved_date_context.column_name if resolved_date_context else None,
                        ),
                        description="Year-over-year percent change.",
                    ),
                    MeasureDefinition(
                        table=fact_table_name,
                        name="MoM % Change",
                        expression=self.generate_dax(
                            "mom",
                            fact_table_name,
                            base_column,
                            time_grain=intent.time_grain,
                            date_table=resolved_date_context.table_name if resolved_date_context else None,
                            date_column=resolved_date_context.column_name if resolved_date_context else None,
                        ),
                        description="Month-over-month percent change.",
                    ),
                ]
            )

        return self._dedupe_measures(measures)

    def generate_dax(
        self,
        metric_type: str,
        table: str,
        column: str | None,
        filters: list[FilterSpec] | None = None,
        time_grain: str | None = None,
        base_measure: str | None = None,
        target: str | None = None,
        date_table: str | None = None,
        date_column: str | None = None,
    ) -> str:
        """Generate a DAX expression for a common metric pattern."""

        metric = metric_type.lower()
        column_ref = f"{table}[{column}]" if column else f"{table}[Value]"
        resolved_date_table = date_table or "DateDim"
        resolved_date_column = date_column or "Date"
        date_ref = f"'{resolved_date_table}'[{resolved_date_column}]"
        base_expression = {
            "sum": f"SUM({column_ref})",
            "average": f"AVERAGE({column_ref})",
            # COUNTROWS needs no column reference at all, so it's the safe
            # choice whenever no real source column could be resolved --
            # COUNT(table[Value]) would reference a column that doesn't exist.
            "count": f"COUNTROWS({table})" if column is None else f"COUNT({column_ref})",
            "distinct_count": f"DISTINCTCOUNT({column_ref})",
            "percentage": f"DIVIDE(SUM({column_ref}), COUNTROWS({table}))",
            "ratio": f"DIVIDE(SUM({column_ref}), COUNTROWS({table}))",
        }.get(metric)

        if metric == "ytd":
            base_expression = f"TOTALYTD(SUM({column_ref}), {date_ref})"
        elif metric == "qtd":
            base_expression = f"TOTALQTD(SUM({column_ref}), {date_ref})"
        elif metric == "mtd":
            base_expression = f"TOTALMTD(SUM({column_ref}), {date_ref})"
        elif metric == "yoy":
            base_expression = (
                "VAR CurrentPeriod = SUM({column_ref})\n"
                "VAR PreviousYear = CALCULATE(SUM({column_ref}), SAMEPERIODLASTYEAR({date_ref}))\n"
                "RETURN DIVIDE(CurrentPeriod - PreviousYear, PreviousYear)"
            ).format(column_ref=column_ref, date_ref=date_ref)
        elif metric == "mom":
            base_expression = (
                "VAR CurrentPeriod = SUM({column_ref})\n"
                "VAR PreviousPeriod = CALCULATE(SUM({column_ref}), DATEADD({date_ref}, -1, MONTH))\n"
                "RETURN DIVIDE(CurrentPeriod - PreviousPeriod, PreviousPeriod)"
            ).format(column_ref=column_ref, date_ref=date_ref)
        elif metric == "% of total":
            base_expression = (
                f"DIVIDE(SUM({column_ref}), CALCULATE(SUM({column_ref}), ALL({table})))"
            )
        elif metric == "percent_change":
            base_expression = (
                f"VAR CurrentValue = SUM({column_ref})\n"
                f"VAR PreviousValue = CALCULATE(SUM({column_ref}), DATEADD({date_ref}, -1, MONTH))\n"
                "RETURN DIVIDE(CurrentValue - PreviousValue, PreviousValue)"
            )
        elif metric == "target_vs_actual":
            target_ref = self._sanitize_name(target or "Target")
            base_expression = (
                f"DIVIDE(SUM({column_ref}) - SUM({table}[{target_ref}]), SUM({table}[{target_ref}]))"
            )

        if base_expression is None:
            base_expression = f"SUM({column_ref})"

        if filters:
            filter_clauses = [
                self._filter_to_dax_clause(filter_spec, table) for filter_spec in filters
            ]
            filter_clauses = [clause for clause in filter_clauses if clause]
            if filter_clauses:
                inner = base_expression
                base_expression = (
                    "CALCULATE(\n"
                    f"    {inner},\n"
                    + ",\n".join(f"    {clause}" for clause in filter_clauses)
                    + "\n)"
                )

        if metric in {"yoy", "mom"} and time_grain:
            base_expression += f"\n// time_grain: {time_grain}"
        if base_measure:
            base_expression += f"\n// base_measure: {base_measure}"
        return base_expression

    def build_page_structure(
        self,
        intent: IntentResult,
        template_name: str,
        fact_table: TableDefinition,
        dimension_tables: list[TableDefinition],
        measures: list[MeasureDefinition],
        date_context: DateContext | None = None,
    ) -> list[PageDefinition]:
        """Auto-generate a professional multi-page report structure.

        If the parser extracted explicit VisualSpec requests (intent.visuals),
        those drive the Trend/Breakdown pages directly -- correct chart type,
        correct metric, correct dimension. Only falls back to the generic
        per-metric auto-template when intent.visuals is empty (e.g. a very
        sparse prompt or the heuristic fallback parser).
        """

        metric_names = [
            measure.name for measure in measures if measure.name not in self.TIME_INTELLIGENCE_MEASURE_NAMES
        ] or [measure.name for measure in measures]
        measure_format = {measure.name: measure.format_hint for measure in measures}
        category_context = self._preferred_category_context(intent, dimension_tables, fact_table)
        date_context = date_context or self._resolve_date_context(None, intent, fact_table, dimension_tables)
        template = self.TEMPLATE_LIBRARY.get(template_name, self.TEMPLATE_LIBRARY["general"])
        pages: list[PageDefinition] = []

        pages.append(
            PageDefinition(
                name=template["pages"][0],
                visuals=self._build_executive_summary_visuals(
                    metric_names,
                    measure_format,
                    category_context,
                    date_context,
                ),
            )
        )

        spec_pages = self._build_visuals_from_intent_specs(
            intent, metric_names, dimension_tables, date_context, category_context
        )
        if spec_pages is not None:
            trend_visuals, breakdown_visuals = spec_pages
        else:
            trend_visuals = self._build_trend_analysis_visuals(metric_names, date_context)
            breakdown_visuals = self._build_breakdown_visuals(metric_names, category_context)

        if trend_visuals:
            pages.append(
                PageDefinition(
                    name=template["pages"][1],
                    visuals=trend_visuals,
                )
            )
        pages.append(
            PageDefinition(
                name=template["pages"][2],
                visuals=breakdown_visuals,
            )
        )
        pages.append(
            PageDefinition(
                name=template["pages"][3],
                visuals=self._build_detail_view_visuals(metric_names, fact_table),
            )
        )
        return pages

    def build_report_layout(self, intent: IntentResult, model: ModelBuildResult) -> list[dict[str, Any]]:
        """Return a pbix-mcp compatible page layout."""

        layout: list[dict[str, Any]] = []
        for page in model.pages:
            visuals = [self._visual_to_backend_dict(visual) for visual in page.visuals]
            layout.append(
                {
                    "name": page.name,
                    "visuals": visuals,
                    "width": page.width,
                    "height": page.height,
                }
            )
        return layout

    def validate_generated_pbix(
        self,
        file_path: str | Path,
        model: ModelBuildResult | None = None,
    ) -> Path:
        """Validate the PBIX output before returning it to the caller."""

        path = Path(file_path)
        if path.suffix.lower() != ".pbix":
            raise ValueError("Generated file must have a .pbix extension.")
        if not path.exists():
            raise FileNotFoundError(f"Generated PBIX file does not exist: {path}")
        if path.stat().st_size <= 0:
            raise ValueError("Generated PBIX file is empty.")
        if model is not None:
            self._validate_model(model)
        return path

    def generate_power_query_m(
        self,
        table_name: str,
        source_path: str | Path | None = None,
        source_kind: str = "csv",
        sample_data: pd.DataFrame | None = None,
    ) -> str:
        """Generate a parameter-friendly Power Query M script."""

        if source_path:
            path = str(Path(source_path)).replace("\\", "\\\\")
            if source_kind.lower() == "excel":
                return (
                    "let\n"
                    f"    // Auto-generated connection for {table_name}\n"
                    f"    Source = Excel.Workbook(File.Contents(\"{path}\"), null, true),\n"
                    "    Sheets = Source{0}[Data],\n"
                    "    PromotedHeaders = Table.PromoteHeaders(Sheets, [PromoteAllScalars=true])\n"
                    "in\n"
                    "    PromotedHeaders"
                )
            return (
                "let\n"
                f"    // Auto-generated connection for {table_name}\n"
                f"    Source = Csv.Document(File.Contents(\"{path}\"), [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
                "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n"
                "in\n"
                "    PromotedHeaders"
            )

        frame = sample_data if sample_data is not None else self.data_handler.generate_sample_data(10)
        preview = frame.head(5).to_dict(orient="records")
        return (
            "let\n"
            f"    // Placeholder sample data for {table_name}\n"
            f"    Rows = {preview!r},\n"
            f"    Source = Table.FromRecords(Rows)\n"
            "in\n"
            "    Source"
        )

    def build_output_path(self, output_name: str | None = None) -> Path:
        """Build a safe output path for a PBIX artifact."""

        name = safe_filename(output_name or "prompt2pbi_dashboard")
        return self.settings.output_dir / f"{name}.pbix"

    def get_template_names(self) -> list[str]:
        """Return the available dashboard template keys."""

        return sorted(self.TEMPLATE_LIBRARY.keys())

    def _default_backend_factory(self, name: str) -> Any:
        if MCPPBIXBuilder is None:
            self.logger.warning(
                "pbix-mcp is not installed; using local fallback PBIX backend.",
                extra={"dashboard_name": name},
            )
            return LocalPBIXBackend(name)
        return MCPPBIXBuilder(name)

    def _resolve_template_name(self, intent: IntentResult, template_name: str | None) -> str:
        candidate = (template_name or intent.prompt_variant or "general").lower()
        if candidate in self.TEMPLATE_LIBRARY:
            return candidate
        lowered = intent.dashboard_title.lower()
        if any(token in lowered for token in ["sales", "revenue", "pipeline"]):
            return "sales"
        if any(token in lowered for token in ["finance", "profit", "budget"]):
            return "financial"
        if any(token in lowered for token in ["marketing", "campaign", "lead"]):
            return "marketing"
        if any(token in lowered for token in ["hr", "people", "employee"]):
            return "hr"
        if any(token in lowered for token in ["operations", "sla", "backlog"]):
            return "operations"
        return "general"

    def _resolve_dataframe(
        self,
        data_frame: pd.DataFrame | None,
        source_path: str | Path | None,
        intent: IntentResult,
    ) -> pd.DataFrame:
        if data_frame is not None:
            return data_frame.copy()
        if source_path is not None:
            return self.data_handler.read_dataframe(source_path)
        return self.data_handler.generate_sample_data_from_intent(intent, rows=50)

    def _normalize_dataframe(self, data_frame: pd.DataFrame) -> pd.DataFrame:
        frame = data_frame.copy()
        frame.columns = [self._sanitize_name(column) for column in frame.columns]
        return frame.head(self.MAX_EMBEDDED_ROWS)

    def _profile_dataframe(self, data_frame: pd.DataFrame) -> dict[str, Any]:
        schema = self.data_handler.infer_schema(data_frame)
        return {
            "row_count": int(len(data_frame)),
            "column_count": int(len(data_frame.columns)),
            "columns": schema,
        }

    def _build_fact_table(
        self,
        data_frame: pd.DataFrame,
        intent: IntentResult,
        source_path: str | Path | None = None,
    ) -> TableDefinition:
        frame = data_frame.copy()
        for col in frame.columns:
            if self._is_date_series(frame[col]):
                frame[col] = pd.to_datetime(frame[col])
        columns = [
            ColumnDefinition(name=column, data_type=self._map_dtype(frame[column]), source_column=column)
            for column in frame.columns
        ]
        m_code = self.generate_power_query_m(
            "FactData",
            source_path=source_path,
            source_kind=Path(source_path).suffix.lstrip(".") if source_path else "csv",
            sample_data=data_frame,
        )
        source_csv = str(Path(source_path).resolve()) if source_path else None
        return TableDefinition(
            name="FactData",
            columns=columns,
            rows=self._rows_for_embedding(data_frame),
            source_csv=source_csv,
            mode="import",
            m_code=m_code,
            table_role="fact",
        )

    def _build_dimension_tables(
        self,
        data_frame: pd.DataFrame,
        fact_table: TableDefinition,
        intent: IntentResult,
    ) -> tuple[list[TableDefinition], list[RelationshipDefinition]]:
        dimensions: list[TableDefinition] = []
        relationships: list[RelationshipDefinition] = []
        categorical_columns = self._categorical_columns(data_frame, intent)
        for column in categorical_columns:

            if self._is_date_series(data_frame[column]):
                continue

            dim_name = self._dimension_table_name(column)
            key_name = f"{column}Key"
            rows, key_map = self._dimension_rows(data_frame, column, key_name)
            columns = [
                ColumnDefinition(name=key_name, data_type="Int64", source_column=key_name),
                ColumnDefinition(name=column, data_type="String", source_column=column),
            ]
            dimensions.append(
                TableDefinition(
                    name=dim_name,
                    columns=columns,
                    rows=rows,
                    table_role="dimension",
                )
            )
            if all(existing_column.name != key_name for existing_column in fact_table.columns):
                fact_table.columns.append(
                    ColumnDefinition(name=key_name, data_type="Int64", source_column=key_name)
                )
            for row in fact_table.rows:
                raw_value = str(row.get(column, ""))
                row[key_name] = key_map.get(raw_value)
            relationships.append(
                RelationshipDefinition(
                    from_table=fact_table.name,
                    from_column=key_name,
                    to_table=dim_name,
                    to_column=key_name,
                )
            )
        return dimensions, relationships

    def _build_date_dimension(
        self,
        data_frame: pd.DataFrame,
        fact_table: TableDefinition,
        intent: IntentResult,
        date_context: DateContext | None = None,
    ) -> TableDefinition | None:
        resolved = date_context or self._resolve_date_context(data_frame, intent, fact_table)
        if resolved is None:
            return None
        fact_date_column = self._preferred_fact_date_column(data_frame, resolved.column_name)
        if fact_date_column is None or fact_date_column not in data_frame.columns:
            return None
        parsed = pd.to_datetime(data_frame[fact_date_column], errors="coerce")
        valid_dates = parsed.dropna().dt.normalize()
        if valid_dates.empty:
            return None

        # Build a contiguous calendar spanning whole months, not just the exact
        # dates present in the fact data. DAX time-intelligence functions this
        # builder generates (TOTALYTD, SAMEPERIODLASTYEAR, DATEADD) require a
        # gap-free date table to compute correctly -- a table built only from
        # observed fact dates would have holes wherever the source data does
        # (e.g. missing weekends), silently breaking those measures.
        range_start = valid_dates.min().replace(day=1)
        range_end = valid_dates.max().replace(day=1) + pd.offsets.MonthEnd(1)
        calendar_dates = pd.date_range(start=range_start, end=range_end, freq="D")

        rows: list[dict[str, Any]] = []
        for date in calendar_dates:
            rows.append(
                {
                    # Store the raw Timestamp, not a formatted string: fact rows
                    # embed this column via DataFrame.to_dict(), which preserves
                    # datetime64 values as pandas.Timestamp objects. A string here
                    # would never equal those Timestamps, so pbix-mcp's FK/orphan
                    # check would flag every single fact row as an orphan.
                    resolved.column_name: date,
                    "Year": int(date.year),
                    "Quarter": f"Q{((date.month - 1) // 3) + 1}",
                    "Month": int(date.month),
                    "MonthName": date.strftime("%B"),
                    "Day": int(date.day),
                }
            )
        columns = [
            ColumnDefinition(name=resolved.column_name, data_type="DateTime", source_column=fact_date_column),
            ColumnDefinition(name="Year", data_type="Int64"),
            ColumnDefinition(name="Quarter", data_type="String"),
            ColumnDefinition(name="Month", data_type="Int64"),
            ColumnDefinition(name="MonthName", data_type="String"),
            ColumnDefinition(name="Day", data_type="Int64"),
        ]
        return TableDefinition(name=resolved.table_name, columns=columns, rows=rows, table_role="dimension")

    def _date_relationships(
        self,
        fact_table: TableDefinition,
        date_dimension: TableDefinition,
        data_frame: pd.DataFrame,
        intent: IntentResult,
        date_context: DateContext | None = None,
    ) -> list[RelationshipDefinition]:
        resolved = date_context or self._resolve_date_context(data_frame, intent, fact_table)
        fact_date_column = self._preferred_fact_date_column(data_frame, resolved.column_name if resolved else None)
        if fact_date_column is None or resolved is None:
            return []
        if resolved.column_name not in {column.name for column in date_dimension.columns}:
            return []
        return [
            RelationshipDefinition(
                from_table=fact_table.name,
                from_column=fact_date_column,
                to_table=date_dimension.name,
                to_column=resolved.column_name,
            )
        ]

    def _build_executive_summary_visuals(
        self,
        metric_names: list[str],
        measure_format: dict[str, str],
        category_context: tuple[str, str] | None,
        date_context: DateContext | None = None,
    ) -> list[VisualDefinition]:
        """One KPI card per metric (not three cards all bound to the same one),
        plus a single supporting chart -- category breakdown if available,
        otherwise a trend line -- so the page isn't just a row of numbers."""

        capped_metrics = (metric_names or ["Total Count"])[: self.EXEC_SUMMARY_MAX_CARDS]
        card_height = 120
        card_positions = self._row_layout(len(capped_metrics), top=20, height=card_height)
        visuals = [
            self._card_visual(
                f"KPI_Card_{index + 1}",
                metric,
                x=x,
                y=y,
                width=width,
                height=height,
                title=metric,
                format_hint=measure_format.get(metric),
            )
            for index, (metric, (x, y, width, height)) in enumerate(zip(capped_metrics, card_positions))
        ]

        supporting_top = card_height + self.PAGE_MARGIN * 2
        primary_metric = capped_metrics[0]
        chart_slot = self._grid_layout(1, top=supporting_top, columns=1)
        if chart_slot:
            x, y, width, height = chart_slot[0]
            if category_context:
                visuals.append(
                    self._bar_visual(
                        "Category_Split",
                        primary_metric,
                        category_context[0],
                        category_context[1],
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        title=f"{primary_metric} by {category_context[1]}",
                    )
                )
            elif date_context:
                visuals.append(
                    self._line_visual(
                        "Executive_Trend",
                        primary_metric,
                        date_context.table_name,
                        date_context.column_name,
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        title=f"{primary_metric} Trend",
                    )
                )
        return visuals

    def _build_trend_analysis_visuals(
        self,
        metric_names: list[str],
        date_context: DateContext | None,
    ) -> list[VisualDefinition]:
        """Build a trend line per metric (up to a cap), if date context exists."""
        if not date_context or not metric_names:
            self.logger.debug("No date context - skipping trend visuals")
            return []

        capped_metrics = metric_names[: self.TREND_MAX_METRICS]
        positions = self._grid_layout(len(capped_metrics), top=20)
        return [
            self._line_visual(
                name=f"Trend_{metric}",
                metric_name=metric,
                date_table=date_context.table_name,
                date_field=date_context.column_name,
                x=x,
                y=y,
                width=width,
                height=height,
                title=f"{metric} Trend",
            )
            for metric, (x, y, width, height) in zip(capped_metrics, positions)
        ]

    def _build_breakdown_visuals(
        self,
        metric_names: list[str],
        category_context: tuple[str, str] | None,
    ) -> list[VisualDefinition]:
        """Build a category breakdown bar per metric (up to a cap), plus a
        donut chart for the primary metric for visual variety."""

        category_table, category_field = category_context if category_context else ("FactData", "Category")
        capped_metrics = (metric_names or ["Total Count"])[: self.BREAKDOWN_MAX_METRICS]
        items: list[tuple[str, str, Callable[..., VisualDefinition]]] = [
            (metric, f"Breakdown_{metric}", self._bar_visual) for metric in capped_metrics
        ]
        items.append((capped_metrics[0], f"Share_{capped_metrics[0]}", self._donut_visual))

        positions = self._grid_layout(len(items), top=20)
        visuals = []
        for (metric, name, builder), (x, y, width, height) in zip(items, positions):
            visuals.append(builder(name, metric, category_table, category_field, x=x, y=y, width=width, height=height))
        return visuals

    def _build_visuals_from_intent_specs(
        self,
        intent: IntentResult,
        metric_names: list[str],
        dimension_tables: list[TableDefinition],
        date_context: DateContext | None,
        category_context: tuple[str, str] | None,
    ) -> tuple[list[VisualDefinition], list[VisualDefinition]] | None:
        """Build Trend/Breakdown visuals directly from intent.visuals (VisualSpec).

        Returns None when intent.visuals is empty, signaling the caller to
        fall back to the generic auto-template behavior instead. Every
        VisualSpec whose metric/dimension can be resolved becomes a real
        chart of the requested type; any metric left uncovered afterward
        (not just the first one) still gets a plain breakdown visual so it
        ends up on some page.
        """
        if not intent.visuals:
            return None

        measure_lookup: dict[str, str] = {}
        for name in metric_names:
            measure_lookup[name.strip().lower()] = name
            measure_lookup[self._sanitize_name(name).lower()] = name

        category_lookup: dict[str, tuple[str, str]] = {}
        for table in dimension_tables:
            for column in table.columns:
                if column.name.endswith("Key"):
                    continue
                category_lookup[column.name.lower()] = (table.name, column.name)
                category_lookup[self._sanitize_name(column.name).lower()] = (table.name, column.name)

        trend_visuals: list[VisualDefinition] = []
        breakdown_visuals: list[VisualDefinition] = []
        covered_metrics: set[str] = set()

        for spec in intent.visuals:
            resolved_metric = self._resolve_visual_metric(spec.metric, measure_lookup)
            if resolved_metric is None:
                self.logger.warning(
                    "Skipping requested visual with unresolvable metric",
                    extra={"visual_type": spec.type, "requested_metric": spec.metric},
                )
                continue

            rendered_type = self.VISUAL_SPEC_TYPE_MAP.get(spec.type)
            if rendered_type is None or rendered_type not in self.CATEGORY_MEASURE_VISUAL_TYPES:
                # Cards and tables are handled by Executive Summary / Detail
                # View; anything pbix-mcp can't bind is skipped rather than
                # emitted with a made-up type that validation can't check.
                continue

            is_trend = rendered_type in {"lineChart", "areaChart"}
            dimension_match = self._resolve_visual_dimension(
                spec.dimension, category_lookup, date_context, prefer_date=is_trend
            )
            if dimension_match is None:
                self.logger.warning(
                    "Skipping requested visual with unresolvable dimension",
                    extra={"visual_type": spec.type, "requested_dimension": spec.dimension},
                )
                continue
            dim_table, dim_column = dimension_match

            title = spec.title or (f"{resolved_metric} Trend" if is_trend else f"{resolved_metric} by {dim_column}")
            name = f"{'Trend' if is_trend else 'Breakdown'}_{resolved_metric}_{dim_column}"

            if rendered_type == "lineChart":
                trend_visuals.append(
                    self._line_visual(name, resolved_metric, dim_table, dim_column, x=0, y=0, width=0, height=0, title=title)
                )
            elif rendered_type == "areaChart":
                trend_visuals.append(
                    self._area_visual(name, resolved_metric, dim_table, dim_column, x=0, y=0, width=0, height=0, title=title)
                )
            elif rendered_type == "donutChart":
                breakdown_visuals.append(
                    self._donut_visual(name, resolved_metric, dim_table, dim_column, x=0, y=0, width=0, height=0, title=title)
                )
            elif rendered_type == "waterfallChart":
                breakdown_visuals.append(
                    self._waterfall_visual(name, resolved_metric, dim_table, dim_column, x=0, y=0, width=0, height=0, title=title)
                )
            else:  # clusteredColumnChart and any other category+measure fallback
                breakdown_visuals.append(
                    self._bar_visual(name, resolved_metric, dim_table, dim_column, x=0, y=0, width=0, height=0, title=title)
                )
            covered_metrics.add(resolved_metric)

        # Every metric mentioned in the prompt should end up on some page --
        # give any metric not covered by an explicit visual a plain breakdown
        # bar, capped so the page doesn't get overcrowded.
        if category_context:
            for metric in metric_names:
                if metric in covered_metrics or len(breakdown_visuals) >= self.BREAKDOWN_MAX_METRICS + 2:
                    continue
                breakdown_visuals.append(
                    self._bar_visual(
                        f"Breakdown_{metric}",
                        metric,
                        category_context[0],
                        category_context[1],
                        x=0,
                        y=0,
                        width=0,
                        height=0,
                        title=f"{metric} by {category_context[1]}",
                    )
                )
                covered_metrics.add(metric)

        # A date dimension exists but no trend-shaped visual was explicitly
        # requested (e.g. the prompt only asked for a category breakdown) --
        # still surface a default trend for the primary metric rather than
        # silently dropping the Trend Analysis page. Explicit requests above
        # always win; this only fills a gap when none were made.
        if not trend_visuals and date_context is not None and metric_names:
            primary_metric = metric_names[0]
            trend_visuals.append(
                self._line_visual(
                    f"Trend_{primary_metric}",
                    primary_metric,
                    date_context.table_name,
                    date_context.column_name,
                    x=0,
                    y=0,
                    width=0,
                    height=0,
                    title=f"{primary_metric} Trend",
                )
            )

        if not trend_visuals and not breakdown_visuals:
            return None

        trend_visuals = trend_visuals[: self.TREND_MAX_METRICS + 1]
        breakdown_visuals = breakdown_visuals[: self.BREAKDOWN_MAX_METRICS + 2]
        for visual, (x, y, width, height) in zip(trend_visuals, self._grid_layout(len(trend_visuals), top=20)):
            visual.x, visual.y, visual.width, visual.height = x, y, width, height
        for visual, (x, y, width, height) in zip(breakdown_visuals, self._grid_layout(len(breakdown_visuals), top=20)):
            visual.x, visual.y, visual.width, visual.height = x, y, width, height

        return trend_visuals, breakdown_visuals

    def _resolve_visual_metric(self, requested: str | None, measure_lookup: dict[str, str]) -> str | None:
        if not requested:
            return None
        candidate = requested.strip().lower()
        if candidate in measure_lookup:
            return measure_lookup[candidate]
        sanitized_candidate = self._sanitize_name(requested).lower()
        if sanitized_candidate in measure_lookup:
            return measure_lookup[sanitized_candidate]
        for key, name in measure_lookup.items():
            if candidate and len(candidate) > 2 and (candidate in key or key in candidate):
                return name
        return None

    def _resolve_visual_dimension(
        self,
        requested: str | None,
        category_lookup: dict[str, tuple[str, str]],
        date_context: DateContext | None,
        prefer_date: bool,
    ) -> tuple[str, str] | None:
        candidate = (requested or "").strip().lower()
        date_like_tokens = {"date", "time", "month", "period", "day", "year", "quarter", "week"}
        looks_date_like = not candidate or candidate in date_like_tokens
        if date_context is not None:
            looks_date_like = looks_date_like or candidate in {
                date_context.column_name.lower(),
                self._sanitize_name(date_context.column_name).lower(),
            }

        if date_context is not None and (prefer_date or looks_date_like) and (looks_date_like or prefer_date):
            return date_context.table_name, date_context.column_name

        if candidate in category_lookup:
            return category_lookup[candidate]
        sanitized_candidate = self._sanitize_name(requested or "").lower()
        if sanitized_candidate in category_lookup:
            return category_lookup[sanitized_candidate]
        for key, value in category_lookup.items():
            if candidate and len(candidate) > 2 and (candidate in key or key in candidate):
                return value

        if prefer_date and date_context is not None:
            return date_context.table_name, date_context.column_name
        return None

    def _build_detail_view_visuals(self, metric_names: list[str], fact_table: TableDefinition) -> list[VisualDefinition]:
        columns = [column.name for column in fact_table.columns][:8]
        matrix_columns = [
            {"table": fact_table.name, "column": column} for column in columns
        ]
        return [
            VisualDefinition(
                name="Detail Matrix",
                visual_type="matrix",
                config={"columns": matrix_columns},
                x=20,
                y=20,
                width=900,
                height=420,
            ),
            VisualDefinition(
                name="Supporting Table",
                visual_type="table",
                config={"columns": matrix_columns},
                x=940,
                y=20,
                width=300,
                height=420,
            ),
        ]

    def _card_visual(
        self,
        name: str,
        metric_name: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
        format_hint: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        config: dict[str, Any] = {"measure": metric_name, "title": title or metric_name}
        if format_hint:
            config["format"] = format_hint
        return VisualDefinition(
            name=safe_name,
            visual_type="card",
            config=config,
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _bar_visual(
        self,
        name: str,
        metric_name: str,
        category_table: str,
        category_field: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        return VisualDefinition(
            name=safe_name,
            visual_type="clusteredColumnChart",
            config={
                "category": {"table": category_table, "column": category_field},
                "measure": metric_name,
                "title": title or f"{metric_name} by {category_field}",
            },
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _line_visual(
        self,
        name: str,
        metric_name: str,
        date_table: str,
        date_field: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        return VisualDefinition(
            name=safe_name,
            visual_type="lineChart",
            config={
                "category": {"table": date_table, "column": date_field},
                "measure": metric_name,
                "title": title or f"{metric_name} Trend",
            },
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _area_visual(
        self,
        name: str,
        metric_name: str,
        date_table: str,
        date_field: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        return VisualDefinition(
            name=safe_name,
            visual_type="areaChart",
            config={
                "category": {"table": date_table, "column": date_field},
                "measure": metric_name,
                "title": title or f"{metric_name} Trend (Area)",
            },
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _donut_visual(
        self,
        name: str,
        metric_name: str,
        category_table: str,
        category_field: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        return VisualDefinition(
            name=safe_name,
            visual_type="donutChart",
            config={
                "category": {"table": category_table, "column": category_field},
                "measure": metric_name,
                "title": title or f"{metric_name} Share by {category_field}",
            },
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _waterfall_visual(
        self,
        name: str,
        metric_name: str,
        category_table: str,
        category_field: str,
        x: int,
        y: int,
        width: int,
        height: int,
        title: str | None = None,
    ) -> VisualDefinition:
        safe_name = self._sanitize_name(name)
        return VisualDefinition(
            name=safe_name,
            visual_type="waterfallChart",
            config={
                "category": {"table": category_table, "column": category_field},
                "measure": metric_name,
                "title": title or f"{metric_name} Variance by {category_field}",
            },
            x=x,
            y=y,
            width=width,
            height=height,
        )

    def _preferred_category_context(
        self,
        intent: IntentResult,
        dimension_tables: list[TableDefinition],
        fact_table: TableDefinition,
    ) -> tuple[str, str] | None:
        # Only trust a dimension the parser suggested if a real dimension
        # table was actually built for it (_categorical_columns already
        # validates suggested names against the real data) -- otherwise this
        # would point visuals at a table/column that doesn't exist, the same
        # class of bug as the OrderID metric fix.
        built_dimension_table_names = {table.name for table in dimension_tables}
        for dimension in intent.dimensions:
            if dimension.type == "date":
                continue
            column_name = self._sanitize_name(dimension.name)
            candidate_table_name = self._dimension_table_name(column_name)
            if candidate_table_name in built_dimension_table_names:
                return candidate_table_name, column_name
        for table in dimension_tables:
            for column in table.columns:
                if column.name.endswith("Key"):
                    continue
                if column.data_type.lower() in {"date", "datetime"} or "date" in column.name.lower():
                    continue
                return table.name, column.name
        for column in fact_table.columns:
            if self._is_categorical_series_name(column.name):
                return fact_table.name, column.name
        return None

    def _preferred_category_field(
        self,
        intent: IntentResult,
        dimension_tables: list[TableDefinition],
        fact_table: TableDefinition,
    ) -> str | None:
        context = self._preferred_category_context(intent, dimension_tables, fact_table)
        return context[1] if context else None

    def _preferred_date_field(
        self,
        intent: IntentResult,
        fact_table: TableDefinition,
        dimension_tables: list[TableDefinition],
    ) -> str | None:
        """Return a date field from intent, fact table, or dimension."""
        for dimension in intent.dimensions:
            if dimension.type in ["date", "temporal", "time"]:
                return dimension.source_column or dimension.name
        for column in fact_table.columns:
            if "date" in column.name.lower() or column.data_type == "DateTime":
                return column.name
        return None

    def _preferred_fact_date_column(self, data_frame: pd.DataFrame, preferred_column: str | None = None) -> str | None:
        if preferred_column and preferred_column in data_frame.columns:
            return preferred_column
        for column in data_frame.columns:
            if self._is_date_series(data_frame[column]):
                return column
        for column in data_frame.columns:
            if "date" in column.lower():
                return column
        return None

    def _build_model_from_intent(
        self,
        intent: IntentResult,
        data_frame: pd.DataFrame,
    ) -> ModelBuildResult:
        raise NotImplementedError

    def _categorical_columns(self, data_frame: pd.DataFrame, intent: IntentResult) -> list[str]:
        # Same class of bug as the metric/OrderID fix: a dimension name
        # suggested by the parser (e.g. "Product" when the real column is
        # "Product Category") isn't guaranteed to exist in the real data.
        # Trusting it verbatim crashes _build_dimension_tables with a
        # pandas KeyError; only real columns can go into `explicit`.
        real_columns = {self._sanitize_name(column).lower(): column for column in data_frame.columns}
        explicit: list[str] = []
        for dimension in intent.dimensions:
            if dimension.type == "date":
                continue
            candidate = self._sanitize_name(dimension.name).lower()
            if candidate in real_columns:
                explicit.append(real_columns[candidate])
        candidates: list[str] = []
        for column in data_frame.columns:
            series = data_frame[column]
            if self._is_date_series(series):
                continue
            if self._is_numeric_series(series) or self._is_bool_series(series):
                continue
            if column in explicit:
                candidates.append(column)
                continue
            if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series) or pd.api.types.is_categorical_dtype(series):
                if series.nunique(dropna=True) <= max(30, int(max(1, len(data_frame)) * 0.4)):
                    candidates.append(column)
        return self._dedupe_strings(explicit + candidates)

    def _dimension_rows(
        self, data_frame: pd.DataFrame, column: str, key_name: str
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        unique_values = (
            data_frame[column]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .sort_values(ignore_index=True)
        )
        rows: list[dict[str, Any]] = []
        key_map: dict[str, int] = {}
        for idx, value in enumerate(unique_values, start=1):
            rows.append({key_name: idx, column: value})
            key_map[value] = idx
        return rows, key_map

    def _rows_for_embedding(self, data_frame: pd.DataFrame) -> list[dict[str, Any]]:
        frame = data_frame.copy()
        for col in frame.select_dtypes(include=['datetime64']).columns:
            frame[col] = pd.to_datetime(frame[col])
        frame = frame.where(pd.notnull(frame), None)
        return frame.to_dict(orient="records")

    def _attach_calculated_columns(self, tables: list[TableDefinition], measures: list[MeasureDefinition]) -> None:
        if not measures:
            return
        for table in tables:
            if table.table_role != "fact":
                continue
            if any("variance" in measure.name.lower() for measure in measures):
                table.columns.append(
                    ColumnDefinition(
                        name="Variance %",
                        data_type="Double",
                        is_calculated=True,
                        expression="DIVIDE([Actual] - [Target], [Target])",
                    )
                )

    def _metric_source_column(self, metric: MetricSpec, data_frame: pd.DataFrame | None) -> str | None:
        """Resolve a metric to a column that actually exists in the real data.

        The parser (LLM or heuristic) may suggest a source_column that looks
        plausible for the metric name (e.g. "OrderID" for a "Number of
        Orders" KPI) but doesn't exist in the uploaded/generated data. Using
        that suggestion verbatim produces DAX referencing a nonexistent
        column, which fails in Power BI ("Column 'OrderID' ... cannot be
        found"). Real data always wins: the suggested name is only used if it
        actually matches a real column; otherwise fall back to a real numeric
        column, or None if there isn't one.
        """
        if data_frame is not None:
            real_columns = {self._sanitize_name(column).lower(): column for column in data_frame.columns}
            if metric.source_column:
                candidate = self._sanitize_name(metric.source_column).lower()
                if candidate in real_columns:
                    return real_columns[candidate]
            for column in data_frame.columns:
                if self._is_numeric_series(data_frame[column]):
                    return column
            return None
        # No real data available at all (e.g. a DAX preview without any data)
        # -- the suggested name is the best information we have.
        if metric.source_column:
            return self._sanitize_name(metric.source_column)
        return None

    def _is_ratio_metric(self, metric: MetricSpec) -> bool:
        """A metric is a two-column ratio if its type says so (percentage/
        ratio), or if the parser populated numerator/denominator columns for
        it regardless of the literal type -- e.g. "Average Order Value" is
        type="average" but is Revenue/Orders, not AVERAGE(one column)."""

        return metric.type in {"percentage", "ratio"} or bool(metric.numerator_column or metric.denominator_column)

    def _resolve_exact_or_none(self, candidate: str | None, data_frame: pd.DataFrame | None) -> str | None:
        """Resolve a candidate column name against real columns, with no
        fallback to an unrelated column.

        Used for ratio numerator/denominator resolution, where falling back
        to "any numeric column" (as _metric_source_column does for a plain
        sum/average) could silently compute a misleading ratio -- e.g. if
        "Profit" isn't found, falling back to "Revenue" for the numerator
        would silently compute Revenue/Revenue = 1. An unresolved side
        should surface as unresolved, not as a guess.
        """
        if not candidate or data_frame is None:
            return None
        real_columns = {self._sanitize_name(column).lower(): column for column in data_frame.columns}
        return real_columns.get(self._sanitize_name(candidate).lower())

    def _resolve_ratio_columns(
        self, metric: MetricSpec, data_frame: pd.DataFrame | None
    ) -> tuple[str | None, str | None]:
        """Resolve numerator/denominator columns for a ratio-style metric
        against the real data, validating the parser's suggestions the same
        way _metric_source_column validates a plain source_column."""

        numerator = self._resolve_exact_or_none(metric.numerator_column, data_frame)
        denominator = self._resolve_exact_or_none(metric.denominator_column, data_frame)
        return numerator, denominator

    def generate_ratio_dax(
        self,
        table: str,
        numerator_column: str | None,
        denominator_column: str | None,
        filters: list[FilterSpec] | None = None,
        base_measure: str | None = None,
    ) -> str:
        """Generate a DIVIDE-based DAX expression for a two-column ratio
        metric (e.g. Profit Margin % = Profit/Revenue, Average Order Value =
        Revenue/Orders).

        Uses DIVIDE's third argument (0) so a blank/zero denominator returns
        0 instead of a divide-by-zero error visual in Power BI. A side that
        couldn't be resolved to a real column falls back to COUNTROWS(table)
        rather than referencing a nonexistent column -- callers should
        already have surfaced an unresolved ratio metric to the user during
        review (see dashboard_review.find_unresolved_ratio_metrics) before
        generation ever reaches this point.
        """

        numerator_expr = f"SUM({table}[{numerator_column}])" if numerator_column else f"COUNTROWS({table})"
        denominator_expr = f"SUM({table}[{denominator_column}])" if denominator_column else f"COUNTROWS({table})"
        base_expression = f"DIVIDE({numerator_expr}, {denominator_expr}, 0)"

        if filters:
            filter_clauses = [self._filter_to_dax_clause(filter_spec, table) for filter_spec in filters]
            filter_clauses = [clause for clause in filter_clauses if clause]
            if filter_clauses:
                inner = base_expression
                base_expression = (
                    "CALCULATE(\n"
                    f"    {inner},\n"
                    + ",\n".join(f"    {clause}" for clause in filter_clauses)
                    + "\n)"
                )

        if base_measure:
            base_expression += f"\n// base_measure: {base_measure}"
        return base_expression

    def _fallback_value_column(self, data_frame: pd.DataFrame | None) -> str:
        if data_frame is None:
            return "Value"
        for column in data_frame.columns:
            if self._is_numeric_series(data_frame[column]):
                return column
        return data_frame.columns[0] if len(data_frame.columns) else "Value"

    def _map_dtype(self, series: pd.Series) -> str:
        if self._is_date_series(series):
            return "DateTime"
        if self._is_bool_series(series):
            return "Boolean"
        if self._is_integer_series(series):
            return "Int64"
        if self._is_float_series(series):
            return "Double"
        return "String"

    def _is_numeric_series(self, series: pd.Series) -> bool:
        return pd.api.types.is_numeric_dtype(series)

    def _is_integer_series(self, series: pd.Series) -> bool:
        return pd.api.types.is_integer_dtype(series)

    def _is_float_series(self, series: pd.Series) -> bool:
        return pd.api.types.is_float_dtype(series)

    def _is_bool_series(self, series: pd.Series) -> bool:
        return pd.api.types.is_bool_dtype(series)

    def _is_date_series(self, series: pd.Series) -> bool:
        if pd.api.types.is_datetime64_any_dtype(series):
            return True
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            return False
        parsed = pd.to_datetime(series.astype(str), errors="coerce", format="mixed")
        return parsed.notna().sum() >= max(3, math.ceil(len(series) * 0.6))

    def _is_categorical_series_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(
            keyword in lowered
            for keyword in ["name", "type", "group", "category", "class", "segment", "label"]
        )

    def _dimension_table_name(self, column_name: str) -> str:
        return f"Dim{self._sanitize_name(column_name)}"

    def _resolve_date_context(
        self,
        data_frame: pd.DataFrame | None,
        intent: IntentResult,
        fact_table: TableDefinition | None = None,
        dimension_tables: list[TableDefinition] | None = None,
    ) -> DateContext | None:
        explicit_date_names = [self._sanitize_name(dimension.name) for dimension in intent.dimensions if dimension.type == "date"]
        if data_frame is not None:
            for column in explicit_date_names:
                if column in data_frame.columns and self._is_date_series(data_frame[column]):
                    return DateContext(table_name=self._dimension_table_name(column), column_name=column)
            for column in data_frame.columns:
                if self._is_date_series(data_frame[column]):
                    return DateContext(table_name=self._dimension_table_name(column), column_name=column)
            for column in explicit_date_names:
                if column in data_frame.columns:
                    return DateContext(table_name=self._dimension_table_name(column), column_name=column)

        if fact_table is not None:
            for column in fact_table.columns:
                if "date" in column.name.lower():
                    return DateContext(table_name=self._dimension_table_name(column.name), column_name=column.name)

        if dimension_tables is not None:
            for table in dimension_tables:
                for column in table.columns:
                    if column.data_type.lower() in {"date", "datetime"}:
                        return DateContext(table_name=table.name, column_name=column.name)

        return None

    def _sanitize_name(self, value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_")
        return value or "Column"

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def _row_layout(self, count: int, top: int, height: int, columns: int | None = None) -> list[tuple[int, int, int, int]]:
        """Lay out `count` equal-width items in a single fixed-height row.

        Used for KPI cards, which should stay a compact fixed height
        regardless of how much vertical space is left on the page.
        """
        if count <= 0:
            return []
        columns = max(1, min(columns or count, count))
        available_width = self.PAGE_WIDTH - self.PAGE_MARGIN * (columns + 1)
        item_width = max(1, available_width // columns)
        return [
            (self.PAGE_MARGIN + index * (item_width + self.PAGE_MARGIN), top, item_width, height)
            for index in range(count)
        ]

    def _grid_layout(
        self,
        count: int,
        top: int = 20,
        columns: int | None = None,
        bottom_margin: int | None = None,
    ) -> list[tuple[int, int, int, int]]:
        """Lay out `count` items in a grid that fills from `top` to the bottom
        of the 1280x720 canvas, guaranteeing no overlap and no overflow.

        Returns (x, y, width, height) tuples. Used everywhere chart visuals
        are placed on a page so item sizing/spacing math lives in exactly one
        place instead of being hand-computed (and easy to get wrong) in every
        _build_*_visuals method.
        """
        if count <= 0:
            return []
        bottom_margin = self.PAGE_MARGIN if bottom_margin is None else bottom_margin
        if columns is None:
            columns = min(count, 3) if count <= 3 else min(4, math.ceil(math.sqrt(count)))
        columns = max(1, min(columns, count))
        rows = math.ceil(count / columns)

        available_width = self.PAGE_WIDTH - self.PAGE_MARGIN * (columns + 1)
        item_width = max(1, available_width // columns)

        available_height = max(1, self.PAGE_HEIGHT - top - bottom_margin - self.PAGE_MARGIN * (rows - 1))
        item_height = max(1, available_height // rows)

        positions: list[tuple[int, int, int, int]] = []
        for index in range(count):
            row, col = divmod(index, columns)
            x = self.PAGE_MARGIN + col * (item_width + self.PAGE_MARGIN)
            y = top + row * (item_height + self.PAGE_MARGIN)
            positions.append((x, y, item_width, item_height))
        return positions

    @staticmethod
    def _rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    def _infer_format_hint(self, metric: MetricSpec) -> str:
        """Classify a metric's display format: currency, percentage, whole_number, or number.

        Power BI's own FormatString metadata isn't settable through pbix-mcp's
        current add_measure() API (verified against the installed library --
        no format parameter exists), so this can't be applied to the built
        .pbix file directly. It's still computed and surfaced in the measure
        description and card config so the intended format is visible and
        testable, and so it's a one-line change to wire through if a future
        pbix-mcp version exposes a format hook.
        """
        haystack = f"{metric.name} {metric.description or ''}".lower()
        if metric.type in {"percentage", "ratio"} or any(
            token in haystack for token in ["%", "percent", "margin", "rate", "pct", "ratio"]
        ):
            return "percentage"
        if any(
            token in haystack
            for token in ["revenue", "profit", "cost", "price", "budget", "sales", "income", "expense", "value", "aov", "spend"]
        ):
            return "currency"
        if metric.type in {"count", "distinct_count"} or any(
            token in haystack for token in ["count", "orders", "units", "quantity", "number of", "volume"]
        ):
            return "whole_number"
        return "number"

    def _format_hint_to_dax_format(self, format_hint: str) -> str:
        """Map a format_hint to a Power BI/DAX FormatString.

        Passed through to the backend only when its add_measure() actually
        accepts a format_string kwarg (see _apply_model_to_backend) -- the
        currently pinned pbix-mcp version does not expose one.
        """
        return {
            "currency": '"$"#,##0.00',
            "percentage": "0.0%",
            "whole_number": "#,##0",
            "number": "#,##0.00",
        }.get(format_hint, "#,##0.00")

    def _validate_model(self, model: ModelBuildResult) -> None:
        if not model.tables:
            raise ValueError("PBIX model must contain at least one table.")

        table_names = {table.name for table in model.tables}
        column_lookup = {
            table.name: {column.name for column in table.columns}
            for table in model.tables
        }

        for table in model.tables:
            if not table.columns:
                raise ValueError(f"Table '{table.name}' must contain at least one column.")

        for relationship in model.relationships:
            if relationship.from_table not in table_names:
                raise ValueError(f"Relationship references missing table: {relationship.from_table}")
            if relationship.to_table not in table_names:
                raise ValueError(f"Relationship references missing table: {relationship.to_table}")
            if relationship.from_column not in column_lookup[relationship.from_table]:
                raise ValueError(
                    f"Relationship references missing column: {relationship.from_table}.{relationship.from_column}"
                )
            if relationship.to_column not in column_lookup[relationship.to_table]:
                raise ValueError(
                    f"Relationship references missing column: {relationship.to_table}.{relationship.to_column}"
                )

        for measure in model.measures:
            if not self._validate_dax_expression(measure.expression):
                raise ValueError(f"Invalid DAX expression for measure '{measure.name}'.")

        for page in model.pages:
            for visual in page.visuals:
                if not visual.name or not visual.visual_type:
                    raise ValueError("Every visual must have a name and visual type.")
                if not self._visual_references_existing_fields(visual, column_lookup, {measure.name for measure in model.measures}):
                    raise ValueError(f"Visual '{visual.name}' references missing fields.")

    def _validate_dax_expression(self, expression: str) -> bool:
        stripped = expression.strip()
        if not stripped:
            return False
        if stripped.count("(") != stripped.count(")"):
            return False
        if stripped.count("[") != stripped.count("]"):
            return False
        return True

    def _visual_references_existing_fields(
        self,
        visual: VisualDefinition,
        column_lookup: dict[str, set[str]],
        measure_names: set[str],
    ) -> bool:
        config = visual.config
        if visual.visual_type == "card":
            return config.get("measure") in measure_names
        if visual.visual_type in self.CATEGORY_MEASURE_VISUAL_TYPES:
            category = config.get("category", {})
            metric = config.get("measure")
            table = category.get("table")
            column = category.get("column")
            return (
                metric in measure_names
                and table in column_lookup
                and column in column_lookup[table]
            )
        if visual.visual_type in {"table", "matrix"}:
            columns = config.get("columns", [])
            for item in columns:
                table = item.get("table")
                column = item.get("column")
                if table not in column_lookup or column not in column_lookup[table]:
                    return False
            return True
        # Fail closed: an unrecognized visual_type means we don't know how to
        # verify its bindings, so treat it as invalid rather than silently
        # passing it through -- this is exactly how the original KPI-card bug
        # (three cards bound to one metric) slipped past validation unnoticed.
        return False

    def _filter_to_dax_clause(self, filter_spec: FilterSpec, table: str) -> str | None:
        field_ref = f"{table}[{filter_spec.field}]"
        operator = filter_spec.operator.lower()
        if operator in {"equals", "=", "eq"}:
            return f'{field_ref} = "{filter_spec.value}"'
        if operator in {"not_equals", "!=", "ne"}:
            return f'{field_ref} <> "{filter_spec.value}"'
        if operator in {"contains"}:
            return f'CONTAINSSTRING({field_ref}, "{filter_spec.value}")'
        if operator in {"greater", "gt"}:
            return f"{field_ref} > {filter_spec.value}"
        if operator in {"less", "lt"}:
            return f"{field_ref} < {filter_spec.value}"
        if operator in {"relative"}:
            return f'-- relative filter: {filter_spec.value}'
        return None

    def _dedupe_relationships(self, relationships: list[RelationshipDefinition]) -> list[RelationshipDefinition]:
        seen: set[tuple[str, str, str, str]] = set()
        deduped: list[RelationshipDefinition] = []
        for relationship in relationships:
            key = (
                relationship.from_table,
                relationship.from_column,
                relationship.to_table,
                relationship.to_column,
            )
            if key not in seen:
                seen.add(key)
                deduped.append(relationship)
        return deduped

    def _dedupe_measures(self, measures: list[MeasureDefinition]) -> list[MeasureDefinition]:
        seen: set[str] = set()
        deduped: list[MeasureDefinition] = []
        for measure in measures:
            if measure.name not in seen:
                seen.add(measure.name)
                deduped.append(measure)
        return deduped

    def _save_backend(self, backend: Any, output_path: Path, model: ModelBuildResult, retries: int = 2) -> None:
        """Save using the pbix-mcp backend with basic transient retry handling.

        When the backend exposes build() (the real pbix-mcp PBIXBuilder does;
        our LocalPBIXBackend fallback and the test double don't), the raw
        bytes are post-processed -- per-visual titles, measure FormatString,
        and a report theme -- before being written to disk. See
        _postprocess_pbix_bytes for why this has to happen at the byte level
        rather than through add_measure()/add_page(): none of these three are
        settable through pbix-mcp's high-level API in the installed version.
        Falls back to backend.save() untouched when build() isn't available.
        """

        build = getattr(backend, "build", None)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if callable(build):
                    data = build()
                    data = self._postprocess_pbix_bytes(data, model)
                    validate = getattr(backend, "validate", None)
                    if callable(validate):
                        issues = validate(data)
                        if issues:
                            self.logger.warning(
                                "PBIX validation issues after post-processing",
                                extra={"issues": issues},
                            )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(data)
                    return
                save = getattr(backend, "save", None)
                if callable(save):
                    save(str(output_path))
                    return
                raise AttributeError("pbix-mcp builder does not expose save/build.")
            except Exception as exc:
                last_error = exc
                if attempt < retries and self._is_transient_backend_error(exc):
                    time.sleep(min(2**attempt, 4))
                    continue
                raise
        if last_error is not None:
            raise last_error

    # ------------------------------------------------------------------
    # Post-processing: title objects, measure FormatString, report theme.
    #
    # None of these three are settable through pbix-mcp 0.9.2's high-level
    # PBIXBuilder API (add_table/add_measure/add_page/build). Verified by
    # reading the installed library's source directly, not by assumption:
    #
    #   - add_measure(table, name, expression, description="") has no
    #     format_string parameter, AND the SQL that inserts a row into the
    #     Measure table hardcodes FormatString to the literal NULL
    #     (pbix_mcp/builder.py, _modify_metadata_and_encode, the
    #     "INSERT INTO [Measure]" statement) -- so even if a parameter were
    #     added, nothing downstream would read it.
    #   - _build_layout() only ever writes {"visualType", "projections",
    #     "prototypeQuery"} into a visual's singleVisual config -- it never
    #     reads a title/objects key from the dicts we pass to add_page(), so
    #     there is no argument we could pass that would reach the layout.
    #   - PBIXBuilder.build() calls build_pbix_clean(datamodel_bytes,
    #     layout_bytes) with no theme_json, even though that function accepts
    #     one -- build() is a single monolithic method with no earlier exit
    #     point, so using the theme_json parameter would mean reimplementing
    #     build()'s entire body (metadata SQLite creation, VertiPaq encoding,
    #     ABF construction, layout building) in our own code just to reach
    #     the final packaging call under our control instead of theirs. That
    #     is a much larger, more fragile change than patching the complete,
    #     already-built output.
    #
    # What IS available: pbix-mcp ships (and uses internally, e.g. in its own
    # validate() method and its MCP-server tools pbix_set_theme/pbix_format_visual)
    # a documented round-trip toolkit for editing an already-built PBIX:
    #   - formats.datamodel_roundtrip.{decompress_datamodel, compress_datamodel}
    #   - formats.abf_rebuild.rebuild_abf_with_modified_sqlite(abf, modifier_fn) --
    #     purpose-built for "open the embedded metadata.sqlitedb, run arbitrary
    #     SQL against it, rebuild the ABF container" (see its own docstring).
    # The exact Report/Layout JSON shape needed for titles (singleVisual.
    # vcObjects.title) and for a theme (top-level resourcePackages entry +
    # config.themeCollection) was taken directly from pbix-mcp's own
    # server.py (_build_format_objects and pbix_set_theme), not guessed.
    #
    # This means all three are fixed via post-processing the complete build()
    # output using pbix-mcp's own documented helpers -- not by reimplementing
    # its internals, and not by forking build()'s orchestration.
    # ------------------------------------------------------------------

    _DEFAULT_THEME_FILENAME = "CY24SU11.json"
    _DEFAULT_THEME: dict[str, Any] = {
        "name": "Prompt2PBI",
        "dataColors": [
            "#4F46E5", "#7C3AED", "#0EA5E9", "#10B981",
            "#F59E0B", "#EF4444", "#6366F1", "#14B8A6",
        ],
        "background": "#FFFFFF",
        "foreground": "#0F172A",
        "tableAccent": "#4F46E5",
    }

    def _postprocess_pbix_bytes(self, pbix_bytes: bytes, model: ModelBuildResult) -> bytes:
        """Apply title/format-string/theme patches to a fully-built PBIX.

        Silently returns the input unchanged (logging a warning) if a patch
        step fails or the expected internal structure (DataModel, Report/
        Layout) isn't present -- e.g. a future pbix-mcp version that changed
        its internal format -- so a patching failure never blocks basic PBIX
        generation.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(pbix_bytes)) as zf:
                entries = {name: zf.read(name) for name in zf.namelist()}
        except zipfile.BadZipFile:
            self.logger.warning("Built PBIX is not a valid ZIP; skipping title/format/theme patches.")
            return pbix_bytes

        changed = False

        if "DataModel" in entries:
            try:
                entries["DataModel"] = self._patch_measure_format_strings(entries["DataModel"], model.measures)
                changed = True
            except Exception:
                self.logger.warning(
                    "Could not patch measure FormatString; leaving DataModel untouched.", exc_info=True
                )

        if "Report/Layout" in entries:
            try:
                entries["Report/Layout"], theme_file = self._patch_report_layout(entries["Report/Layout"], model)
                entries[theme_file[0]] = theme_file[1]
                changed = True
            except Exception:
                self.logger.warning(
                    "Could not patch report layout titles/theme; leaving Report/Layout untouched.", exc_info=True
                )

        if not changed:
            return pbix_bytes

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out_zip:
            for name, content in entries.items():
                out_zip.writestr(name, content)
        return buffer.getvalue()

    def _patch_measure_format_strings(self, datamodel_bytes: bytes, measures: list[MeasureDefinition]) -> bytes:
        """Set each measure's FormatString directly in the embedded metadata
        SQLite, via pbix-mcp's own rebuild_abf_with_modified_sqlite() helper."""

        from pbix_mcp.formats.abf_rebuild import rebuild_abf_with_modified_sqlite
        from pbix_mcp.formats.datamodel_roundtrip import compress_datamodel, decompress_datamodel

        abf = decompress_datamodel(datamodel_bytes)
        format_by_name = {measure.name: self._format_hint_to_dax_format(measure.format_hint) for measure in measures}

        def modifier(conn: Any) -> None:
            for name, format_string in format_by_name.items():
                conn.execute(
                    "UPDATE [Measure] SET [FormatString] = ? WHERE [Name] = ?",
                    (format_string, name),
                )

        new_abf = rebuild_abf_with_modified_sqlite(abf, modifier)
        return compress_datamodel(new_abf)

    def _patch_report_layout(self, layout_bytes: bytes, model: ModelBuildResult) -> tuple[bytes, tuple[str, bytes]]:
        """Inject per-visual title objects and register a report theme.

        Returns the patched Layout bytes plus the (path, content) of the new
        theme resource file that must also be added to the PBIX ZIP.
        """

        layout = json.loads(layout_bytes.decode("utf-16-le"))

        title_by_visual_name = {
            visual.name: visual.config.get("title")
            for page in model.pages
            for visual in page.visuals
            if visual.config.get("title")
        }
        for section in layout.get("sections", []):
            for container in section.get("visualContainers", []):
                try:
                    config = json.loads(container["config"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
                title_text = title_by_visual_name.get(config.get("name"))
                single_visual = config.get("singleVisual")
                if title_text and isinstance(single_visual, dict):
                    vc_objects = single_visual.setdefault("vcObjects", {})
                    vc_objects["title"] = [
                        {
                            "properties": {
                                "show": {"expr": {"Literal": {"Value": "true"}}},
                                "text": {"expr": {"Literal": {"Value": self._pbi_string_literal(title_text)}}},
                            }
                        }
                    ]
                    container["config"] = json.dumps(config, ensure_ascii=False)

        theme = self._DEFAULT_THEME
        theme_filename = self._DEFAULT_THEME_FILENAME
        resource_packages = layout.get("resourcePackages", [])
        shared_pkg = None
        for pkg in resource_packages:
            inner = pkg.get("resourcePackage", pkg)
            if inner.get("name") == "SharedResources":
                shared_pkg = inner
                break
        if shared_pkg is None:
            shared_pkg = {"name": "SharedResources", "type": 2, "items": [], "disabled": False}
            resource_packages.append({"resourcePackage": shared_pkg})
        items = shared_pkg.setdefault("items", [])
        if not any(item.get("type") == 202 for item in items):
            items.append({"type": 202, "path": f"BaseThemes/{theme_filename}", "name": theme["name"]})
        layout["resourcePackages"] = resource_packages

        config_str = layout.get("config", "{}")
        try:
            report_config = json.loads(config_str) if isinstance(config_str, str) else dict(config_str)
        except json.JSONDecodeError:
            report_config = {}
        report_config["themeCollection"] = {
            "baseTheme": {
                "name": theme["name"],
                "version": {"visual": "1.0.0", "report": "1.0.0", "page": "1.0.0"},
                "type": 2,
            }
        }
        layout["config"] = json.dumps(report_config, ensure_ascii=False)

        patched_layout = json.dumps(layout, ensure_ascii=False).encode("utf-16-le")
        theme_path = f"Report/StaticResources/SharedResources/BaseThemes/{theme_filename}"
        theme_bytes = json.dumps(theme, indent=2, ensure_ascii=False).encode("utf-8")
        return patched_layout, (theme_path, theme_bytes)

    def _pbi_string_literal(self, text: str) -> str:
        """Escape a string for use as a DAX/PBI Literal expression Value (e.g. 'Text')."""

        return "'" + text.replace("'", "''") + "'"

    def _is_transient_backend_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(token in message for token in ["timeout", "temporarily", "transient", "locked"])

    def _instantiate_backend(self, name: str) -> Any:
        backend = self.backend_factory(name)
        if backend is None:
            raise RuntimeError("PBIX backend factory returned no builder instance.")
        return backend

    def _apply_model_to_backend(self, backend: Any, model: ModelBuildResult) -> None:
        self._validate_model(model)
        add_table_signature = None
        try:
            add_table_signature = inspect.signature(backend.add_table)
        except (TypeError, ValueError, AttributeError):
            add_table_signature = None
        for table in model.tables:
            table_kwargs = {
                "rows": table.rows,
                "hidden": table.hidden,
                "source_csv": table.source_csv,
                "source_db": table.source_db,
                "mode": table.mode,
            }
            if "skip_hierarchy" in table.__dataclass_fields__ and (
                add_table_signature is None or "skip_hierarchy" in add_table_signature.parameters
            ):
                table_kwargs["skip_hierarchy"] = table.skip_hierarchy
            backend.add_table(
                table.name,
                [{"name": column.name, "data_type": column.data_type} for column in table.columns],
                **table_kwargs,
            )

        add_measure_signature = None
        try:
            add_measure_signature = inspect.signature(backend.add_measure)
        except (TypeError, ValueError, AttributeError):
            add_measure_signature = None
        for measure in model.measures:
            measure_kwargs: dict[str, Any] = {"description": measure.description}
            if add_measure_signature is None or "format_string" in add_measure_signature.parameters:
                measure_kwargs["format_string"] = self._format_hint_to_dax_format(measure.format_hint)
            backend.add_measure(
                measure.table,
                measure.name,
                measure.expression,
                **measure_kwargs,
            )

        for relationship in model.relationships:
            backend.add_relationship(
                relationship.from_table,
                relationship.from_column,
                relationship.to_table,
                relationship.to_column,
            )

        for page in model.pages:
            backend.add_page(
                page.name,
                visuals=[self._visual_to_backend_dict(visual) for visual in page.visuals],
            )

    def _visual_to_backend_dict(self, visual: VisualDefinition) -> dict[str, Any]:
        """Convert a VisualDefinition into the dict shape pbix-mcp's builder expects.

        pbix_mcp.builder.PBIXBuilder._build_layout() reads the visual's chart
        type from a "type" key (`vis.get("type", "card")`) -- NOT "visual_type".
        asdict(VisualDefinition) produces "visual_type", so passing that dict
        straight through silently defaulted every chart (bar/line/area/donut/
        waterfall) to a plain "card", discarding its category binding. Confirmed
        by calling the real installed pbix-mcp builder directly with both shapes.
        """
        return {
            "name": visual.name,
            "type": visual.visual_type,
            "config": visual.config,
            "x": visual.x,
            "y": visual.y,
            "width": visual.width,
            "height": visual.height,
        }
