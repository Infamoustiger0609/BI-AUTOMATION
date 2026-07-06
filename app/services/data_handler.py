"""
Data processing for uploaded files.
Handles CSV, Excel, and other data formats.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.config import Settings, get_settings
from app.models.intent import DimensionSpec, IntentResult, MetricSpec
from app.utils.helpers import ensure_directory
from app.utils.validators import is_allowed_extension, validate_file_exists


class DataHandler:
    """Utility class for validating and reading uploaded data."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        ensure_directory(self.settings.upload_dir)

    def validate_file_format(self, file_path: str | Path) -> Path:
        """Validate that the uploaded file has a supported extension."""

        path = validate_file_exists(file_path)
        if not is_allowed_extension(path, self.settings.allowed_extensions):
            allowed = ", ".join(self.settings.allowed_extensions)
            raise ValueError(f"Unsupported file extension. Allowed: {allowed}")
        return path

    def read_dataframe(self, file_path: str | Path) -> pd.DataFrame:
        """Read a supported file format into a pandas DataFrame."""

        path = self.validate_file_format(file_path)
        suffix = path.suffix.lower()

        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
        if suffix == ".json":
            return pd.read_json(path)
        raise ValueError(f"Unsupported file type: {suffix}")

    def infer_schema(self, dataframe: pd.DataFrame) -> list[dict[str, Any]]:
        """Infer a lightweight schema suggestion for a DataFrame."""

        suggestions: list[dict[str, Any]] = []
        for column in dataframe.columns:
            series = dataframe[column]
            suggestions.append(
                {
                    "name": column,
                    "dtype": str(series.dtype),
                    "nullable": bool(series.isna().any()),
                    "sample_values": series.dropna().astype(str).head(3).tolist(),
                }
            )
        return suggestions

    def generate_sample_data_from_intent(
        self,
        intent: IntentResult,
        rows: int = 50,
    ) -> pd.DataFrame:
        """Generate schema-aware sample data from the extracted intent."""

        row_count = max(1, rows)
        if not intent.metrics and not intent.dimensions:
            return self._generate_generic_sample_data(row_count)

        data: dict[str, list[Any]] = {}
        date_offset = 0
        has_date_dimension = False

        for dimension in intent.dimensions:
            column_name = self._clean_column_name(dimension.name)
            if dimension.type == "date":
                has_date_dimension = True
                data[column_name] = self._generate_date_series(dimension, row_count, offset=date_offset)
                date_offset += 1
            else:
                data[column_name] = self._generate_categorical_series(dimension, row_count)

        if not has_date_dimension and intent.time_grain != "unknown":
            grain_map = {
                "daily": "daily",
                "weekly": "weekly",
                "monthly": "monthly",
                "quarterly": "quarterly",
                "yearly": "yearly",
                "mixed": "monthly",  # Fallback for mixed
                "unknown": "monthly"  # Fallback for unknown
            }
            grain_value = grain_map.get(intent.time_grain, "monthly")
            generic_date = DimensionSpec(name="Date", type="date", grain=grain_value)
            data[self._clean_column_name(generic_date.name)] = self._generate_date_series(generic_date, row_count)

        for metric in intent.metrics:
            column_name = self._clean_column_name(metric.source_column or metric.name)
            data[column_name] = self._generate_metric_series(metric, row_count, data)

        if not data:
            return self._generate_generic_sample_data(row_count)

        return pd.DataFrame(data)

    def generate_sample_data(self, rows: int = 20) -> pd.DataFrame:
        """Backward-compatible generic sample data generator."""

        return self._generate_generic_sample_data(max(1, rows))

    def _generate_generic_sample_data(self, rows: int) -> pd.DataFrame:
        """Generate a simple fallback data set when no intent schema exists."""

        return pd.DataFrame(
            {
                "Date": pd.date_range("2025-01-01", periods=rows, freq="D"),
                "Category": [f"Category_{(i % 5) + 1}" for i in range(rows)],
                "Value": [100 + (i * 10) for i in range(rows)],
            }
        )

    def _generate_categorical_series(self, dimension: DimensionSpec, rows: int) -> list[str]:
        values = [str(value).strip() for value in dimension.values if str(value).strip()]
        if not values:
            base = self._dimension_base_name(dimension.name)
            values = [f"{base}_{index}" for index in range(1, min(6, rows) + 1)]
        return [values[index % len(values)] for index in range(rows)]

    def _generate_date_series(
        self,
        dimension: DimensionSpec,
        rows: int,
        offset: int = 0,
    ) -> list[pd.Timestamp]:
        grain = (dimension.grain or "daily").lower()
        freq_map = {
            "hourly": "H",
            "daily": "D",
            "weekly": "W-MON",
            "monthly": "MS",
            "quarterly": "QS",
            "yearly": "YS",
        }
        freq = freq_map.get(grain, "D")
        start = pd.Timestamp("2025-01-01") + pd.Timedelta(days=offset * max(1, rows // 10))
        return list(pd.date_range(start=start, periods=rows, freq=freq))

    def _generate_metric_series(
        self,
        metric: MetricSpec,
        rows: int,
        current_data: dict[str, list[Any]],
    ) -> list[Any]:
        metric_type = metric.type.lower()
        metric_name = metric.source_column or metric.name
        base = self._seed_from_name(metric_name)
        scale = max(1, len(current_data) or 1)
        values: list[Any] = []

        for index in range(rows):
            trend = base + ((index + 1) * scale * 3)
            wobble = (index % 7) * (scale + 1)
            value = trend + wobble
            if metric_type in {"percentage", "ratio"} or self._is_fraction_like(metric_name):
                values.append(round(((value % 100) / 100) + 0.05, 2))
            elif metric_type in {"average", "custom"}:
                values.append(round(value / 10.0, 2))
            else:
                values.append(int(value))
        return values

    def _seed_from_name(self, value: str) -> int:
        return sum(ord(char) for char in value) % 97

    def _dimension_base_name(self, value: str) -> str:
        cleaned = self._clean_column_name(value)
        return cleaned or "Category"

    def _is_fraction_like(self, value: str) -> bool:
        lowered = value.lower()
        return any(token in lowered for token in ["margin", "rate", "pct", "percent", "ratio", "growth"])

    def _clean_column_name(self, value: str) -> str:
        cleaned = "_".join(part for part in str(value).strip().replace("-", " ").split())
        return cleaned or "Value"
