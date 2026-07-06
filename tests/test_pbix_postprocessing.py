"""Tests for the title/FormatString/theme post-processing fixes.

Require the real pbix-mcp library (not the LocalPBIXBackend fallback or the
FakeBackendBuilder test double), since these fixes patch the actual DataModel
and Report/Layout structures pbix-mcp produces. Skipped entirely if pbix-mcp
isn't installed, matching the existing pattern used elsewhere in this repo
for pbix-mcp-dependent tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("pbix_mcp")

from app.config import Settings
from app.models.intent import DimensionSpec, IntentResult, MetricSpec, VisualSpec
from app.services.pbix_builder import PBIXBuilder


def _real_builder(tmp_path: Path) -> PBIXBuilder:
    settings = Settings()
    settings.output_dir = tmp_path
    settings.upload_dir = tmp_path / "uploads"
    settings.sample_data_dir = tmp_path / "sample"
    settings.ensure_directories()
    # No backend_factory override -- exercises the real pbix_mcp.builder.PBIXBuilder,
    # which is what create_pbix()/_save_backend() actually build() + post-process.
    return PBIXBuilder(settings=settings)


def _sample_intent_and_data() -> tuple[IntentResult, pd.DataFrame]:
    intent = IntentResult(
        dashboard_title="Sales Performance Dashboard",
        metrics=[
            MetricSpec(name="Total Revenue", type="sum", source_column="Revenue"),
            MetricSpec(name="Number of Orders", type="count", source_column="Orders"),
            MetricSpec(name="Profit Margin", type="percentage", source_column="Profit"),
        ],
        dimensions=[
            DimensionSpec(name="Region", type="categorical"),
            DimensionSpec(name="Date", type="date", grain="monthly"),
        ],
        visuals=[
            VisualSpec(type="line_chart", metric="Total Revenue", dimension="Date", title="Monthly Revenue Trend"),
            VisualSpec(type="bar_chart", metric="Total Revenue", dimension="Region", title="Revenue by Region"),
        ],
    )
    data = pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=6, freq="MS"),
            "Region": ["East", "West", "North", "South", "East", "West"],
            "Revenue": [15000, 17000, 12000, 19500, 13500, 21000],
            "Profit": [3000, 3400, 1800, 4200, 2100, 4800],
            "Orders": [120, 135, 90, 150, 110, 160],
        }
    )
    return intent, data


def _read_measure_format_strings(pbix_path: Path) -> dict[str, str | None]:
    from pbix_mcp.formats.abf_rebuild import read_metadata_sqlite
    from pbix_mcp.formats.datamodel_roundtrip import decompress_datamodel

    with zipfile.ZipFile(pbix_path) as zf:
        datamodel_bytes = zf.read("DataModel")
    abf = decompress_datamodel(datamodel_bytes)
    sqlite_bytes = read_metadata_sqlite(abf)

    fd, tmp_db_path = tempfile.mkstemp(suffix=".sqlitedb")
    try:
        os.write(fd, sqlite_bytes)
        os.close(fd)
        conn = sqlite3.connect(tmp_db_path)
        try:
            rows = conn.execute("SELECT Name, FormatString FROM [Measure]").fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp_db_path)
    return dict(rows)


def _read_layout(pbix_path: Path) -> dict:
    with zipfile.ZipFile(pbix_path) as zf:
        layout_bytes = zf.read("Report/Layout")
    return json.loads(layout_bytes.decode("utf-16-le"))


def test_measure_format_strings_are_persisted_in_the_real_datamodel(tmp_path):
    builder = _real_builder(tmp_path)
    intent, data = _sample_intent_and_data()

    output_path = builder.create_pbix(intent, data_frame=data, output_name="format_string_test")

    format_strings = _read_measure_format_strings(output_path)
    assert format_strings["Total Revenue"] == '"$"#,##0.00'
    assert format_strings["Number of Orders"] == "#,##0"
    assert format_strings["Profit Margin"] == "0.0%"
    # No measure should be left with the library's hardcoded NULL.
    assert all(value is not None for value in format_strings.values())


def test_visual_titles_are_persisted_in_the_real_report_layout(tmp_path):
    builder = _real_builder(tmp_path)
    intent, data = _sample_intent_and_data()

    output_path = builder.create_pbix(intent, data_frame=data, output_name="title_test")

    layout = _read_layout(output_path)
    titles = {}
    for section in layout.get("sections", []):
        for container in section.get("visualContainers", []):
            config = json.loads(container["config"])
            vc_title = config.get("singleVisual", {}).get("vcObjects", {}).get("title")
            if vc_title:
                titles[config["name"]] = vc_title[0]["properties"]["text"]["expr"]["Literal"]["Value"]

    assert "'Monthly Revenue Trend'" in titles.values()
    assert "'Revenue by Region'" in titles.values()
    # Auto-derived titles (not explicitly requested in intent.visuals) also make it through.
    assert any("Total Revenue" in v for v in titles.values())


def test_report_theme_is_registered_and_embedded(tmp_path):
    builder = _real_builder(tmp_path)
    intent, data = _sample_intent_and_data()

    output_path = builder.create_pbix(intent, data_frame=data, output_name="theme_test")

    layout = _read_layout(output_path)
    resource_packages = layout.get("resourcePackages", [])
    theme_item = None
    for pkg in resource_packages:
        inner = pkg.get("resourcePackage", pkg)
        if inner.get("name") == "SharedResources":
            for item in inner.get("items", []):
                if item.get("type") == 202:
                    theme_item = item

    assert theme_item is not None, "theme not registered in resourcePackages"
    assert theme_item["name"] == "Prompt2PBI"

    report_config = json.loads(layout.get("config", "{}"))
    assert report_config["themeCollection"]["baseTheme"]["name"] == "Prompt2PBI"

    with zipfile.ZipFile(output_path) as zf:
        names = zf.namelist()
        assert "Report/StaticResources/SharedResources/BaseThemes/CY24SU11.json" in names
        theme_json = json.loads(zf.read("Report/StaticResources/SharedResources/BaseThemes/CY24SU11.json"))
    assert theme_json["dataColors"][0] == "#4F46E5"


def test_patched_pbix_still_passes_pbix_mcp_own_validation(tmp_path):
    """The post-processing must not corrupt the structural integrity pbix-mcp
    itself checks for (ZIP structure, DataModel decompression, SQLite
    consistency, AttributeHierarchy, etc.)."""

    from pbix_mcp.builder import PBIXBuilder as MCPPBIXBuilder

    builder = _real_builder(tmp_path)
    intent, data = _sample_intent_and_data()
    output_path = builder.create_pbix(intent, data_frame=data, output_name="validate_test")

    issues = MCPPBIXBuilder("Validate").validate(output_path.read_bytes())
    assert issues == []


def test_postprocess_gracefully_no_ops_when_structure_is_unrecognized(tmp_path):
    """A backend whose output doesn't have DataModel/Report/Layout (e.g. the
    LocalPBIXBackend fallback's manifest-only ZIP) must not raise -- patching
    is best-effort and silently skips when there's nothing to patch."""

    builder = _real_builder(tmp_path)
    fake_zip = io_bytes_zip({"manifest.json": b"{}"})
    result = builder._postprocess_pbix_bytes(fake_zip, model=_dummy_model())
    assert result == fake_zip


def io_bytes_zip(entries: dict[str, bytes]) -> bytes:
    import io as _io

    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _dummy_model():
    from app.services.pbix_builder import ModelBuildResult

    return ModelBuildResult(
        tables=[], relationships=[], measures=[], pages=[], data_profile={}, source_kind="sample", template_name="general"
    )
