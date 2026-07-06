"""Gradio web interface for the dashboard generator.

Single linear flow, top to bottom on one page:

  1. Describe your dashboard (prompt + optional data upload) -> click
     "1. Extract Dashboard Plan". Parses the prompt (profiling the uploaded
     file first, if any) and shows the resulting KPIs/dimensions/visuals as
     editable tables, plus any plain-language notices (fallback parser used,
     a KPI couldn't be matched to a real column, etc).
  2. Review/edit the extracted plan -> click "2. Generate Dashboard". Builds
     the .pbix from the (possibly edited) plan and shows live status/progress
     until the download is ready.

Async path: generation (step 2) reuses JobManager.submit_generation() as-is,
which already transparently routes to Celery+Redis when reachable and falls
back to a local thread pool otherwise -- no new async plumbing needed there.
Parsing (step 1) is a direct synchronous call instead of a second job type:
it's a single request/response (one LLM call or an instant heuristic pass),
so a job-polling UI step would add complexity without buying anything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings
from app.exceptions import FileValidationError
from app.models.intent import IntentResult
from app.services.dashboard_review import (
    DIMENSIONS_COLUMNS,
    METRICS_COLUMNS,
    VISUALS_COLUMNS,
    extraction_notices,
    friendly_error_message,
    intent_to_tables,
    tables_to_intent,
)
from app.services.job_manager import JobManager
from app.utils.helpers import ensure_directory

try:  # pragma: no cover - optional dependency in this environment
    import gradio as gr
except ImportError:  # pragma: no cover
    gr = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Gradio 6 moved theme/css from the Blocks() constructor to .launch() --
# passing them to Blocks() is silently ignored (only a console warning),
# which is why the intended styling wasn't rendering at all. The theme
# object itself is built in launch() below, where gr is guaranteed non-None.
BRAND_CSS = """
:root {
  --prompt2pbi-blue: #1f4ed8;
  --prompt2pbi-navy: #0f172a;
  --prompt2pbi-slate: #e2e8f0;
  --prompt2pbi-surface: #f8fafc;
}

.gradio-container {
  background: linear-gradient(180deg, #f8fafc 0%, #eef4ff 100%);
}

.gradio-container, .gradio-container p, .gradio-container li, .gradio-container span {
  color: var(--prompt2pbi-navy) !important;
}

.wrap {
  max-width: 1180px !important;
}

.prompt2pbi-card {
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 20px 50px rgba(15, 23, 42, 0.08);
  padding: 18px;
  margin-bottom: 16px;
}

.prompt2pbi-step-title {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--prompt2pbi-navy) !important;
  margin-bottom: 2px;
}

.prompt2pbi-step-desc {
  color: #475569 !important;
  font-size: 0.92rem;
  margin-bottom: 10px;
}

.prompt2pbi-hint {
  color: #64748b !important;
  font-style: italic;
  font-size: 0.9rem;
}

.prompt2pbi-error {
  color: #b91c1c !important;
  font-weight: 600;
}

.prompt2pbi-notice {
  color: #1f4ed8 !important;
}
"""

EXAMPLE_PROMPTS = [
    [
        "Create a sales performance dashboard with:\n"
        "5 KPIs: Total Revenue, Total Profit, Profit Margin %, Number of Orders, Average Order Value\n"
        "2 graphs:\n"
        "  1. Monthly Revenue trend (line chart by Date)\n"
        "  2. Revenue by Region (bar chart)\n"
        "Dimensions: Region, Product Category\n"
        "Time grain: monthly"
    ],
    ["Build a financial dashboard showing revenue, profit, budget variance, and monthly trends."],
    ["Design an operations dashboard for backlog, SLA, and throughput tracking."],
]

WAITING_FOR_EXTRACTION_TEXT = (
    "*This will fill in automatically once you click **1. Extract Dashboard Plan** above.*"
)
READY_TO_GENERATE_TEXT = "*Review the plan above -- edit anything that's wrong, then click **2. Generate Dashboard** below.*"
NOT_STARTED_STATUS_TEXT = "Not started yet -- click “2. Generate Dashboard” above once you're happy with the plan."


def _build_job_manager() -> JobManager:
    settings = get_settings()
    ensure_directory(settings.output_dir)
    ensure_directory(settings.upload_dir)
    return JobManager(settings=settings)


def _resolve_upload_path(uploaded_file: Any | None) -> Path | None:
    if uploaded_file is None:
        return None
    if isinstance(uploaded_file, (str, Path)):
        return Path(uploaded_file)
    return Path(getattr(uploaded_file, "name", uploaded_file))


def create_interface() -> Any:
    """Create the Gradio interface if Gradio is installed."""

    if gr is None:
        raise ImportError("gradio is not installed. Install dependencies to launch the UI.")

    job_manager = _build_job_manager()
    templates = job_manager.pbix_builder.get_template_names()

    with gr.Blocks(title="Prompt2PBI") as demo:
        gr.Markdown(
            """
            # Prompt2PBI
            Generate Power BI dashboards from plain-English prompts and your own data -- no coding required.
            """
        )

        parsed_intent_state = gr.State(None)
        upload_path_state = gr.State(None)

        # ---------------------------------------------------------------
        # Step 1: describe the dashboard
        # ---------------------------------------------------------------
        with gr.Column(elem_classes=["prompt2pbi-card"]):
            gr.Markdown("Step 1 -- Describe your dashboard", elem_classes=["prompt2pbi-step-title"])
            gr.Markdown(
                "Tell us what you want in plain English, and optionally upload your own spreadsheet. "
                "If you don't upload a file, we'll use generated sample data so you can preview the layout.",
                elem_classes=["prompt2pbi-step-desc"],
            )
            prompt = gr.TextArea(
                label="Dashboard Prompt",
                placeholder="Describe the dashboard you want to generate.",
                lines=8,
            )
            gr.Examples(examples=EXAMPLE_PROMPTS, inputs=[prompt], label="Quick Start Examples (click one to fill in the prompt above)")
            template = gr.Dropdown(
                label="Template",
                info="A starting style for the report layout -- pick the closest match, or leave as General.",
                choices=templates,
                value="general",
            )
            data_file = gr.File(
                label="Upload your data (CSV or Excel) -- optional, but recommended for real results",
                file_count="single",
                file_types=[".csv", ".xlsx", ".xls", ".json"],
                type="filepath",
            )
            extract_button = gr.Button("1. Extract Dashboard Plan", variant="secondary", size="lg")
            extraction_notice = gr.Markdown(value="", elem_classes=["prompt2pbi-notice"])
            extract_error_banner = gr.Markdown(value="", elem_classes=["prompt2pbi-error"])

        # ---------------------------------------------------------------
        # Step 2: review the extracted plan, then generate
        # ---------------------------------------------------------------
        with gr.Column(elem_classes=["prompt2pbi-card"]):
            gr.Markdown("Step 2 -- Review the plan, then generate", elem_classes=["prompt2pbi-step-title"])
            gr.Markdown(
                "After extraction, the KPIs, dimensions, and charts we found are listed below as editable tables. "
                "Fix anything that's wrong before generating -- for example, if a KPI is matched to the wrong column.",
                elem_classes=["prompt2pbi-step-desc"],
            )
            review_status = gr.Markdown(value=WAITING_FOR_EXTRACTION_TEXT, elem_classes=["prompt2pbi-hint"])

            gr.Markdown("**KPIs / Metrics** -- the numbers shown as cards on your dashboard (e.g. Total Revenue).")
            metrics_table = gr.Dataframe(
                headers=METRICS_COLUMNS,
                label=None,
                interactive=True,
                row_count=(0, "dynamic"),
            )
            gr.Markdown("**Dimensions** -- the categories used to slice/group your data (e.g. Region, Date).")
            dimensions_table = gr.Dataframe(
                headers=DIMENSIONS_COLUMNS,
                label=None,
                interactive=True,
                row_count=(0, "dynamic"),
            )
            gr.Markdown("**Charts** -- the visuals that will appear on the dashboard (chart type, KPI, and dimension).")
            visuals_table = gr.Dataframe(
                headers=VISUALS_COLUMNS,
                label=None,
                interactive=True,
                row_count=(0, "dynamic"),
            )

            generate_button = gr.Button("2. Generate Dashboard", variant="primary", size="lg", interactive=False)

        # ---------------------------------------------------------------
        # Step 3: generation status and download
        # ---------------------------------------------------------------
        with gr.Column(elem_classes=["prompt2pbi-card"]):
            gr.Markdown("Step 3 -- Generation status", elem_classes=["prompt2pbi-step-title"])
            gr.Markdown(
                "This section updates automatically once you click **2. Generate Dashboard** above.",
                elem_classes=["prompt2pbi-step-desc"],
            )
            status = gr.Textbox(label="Status", value=NOT_STARTED_STATUS_TEXT, interactive=False)
            progress = gr.Slider(label="Progress", minimum=0, maximum=100, value=0, step=1, interactive=False)
            output_file = gr.File(label="Download PBIX")
            generate_error_banner = gr.Markdown(value="", elem_classes=["prompt2pbi-error"])

        def _extract(prompt_text: str, template_value: str, uploaded_file: str | None):
            empty_tables = (
                pd.DataFrame(columns=METRICS_COLUMNS),
                pd.DataFrame(columns=DIMENSIONS_COLUMNS),
                pd.DataFrame(columns=VISUALS_COLUMNS),
            )
            try:
                upload_path = _resolve_upload_path(uploaded_file)
                data_frame = None
                if upload_path is not None:
                    if not upload_path.exists():
                        raise FileValidationError("We couldn't read the uploaded file. Please try uploading it again.")
                    data_frame = job_manager.data_handler.read_dataframe(upload_path)

                data_profile = job_manager._data_profile(data_frame)
                intent = asyncio.run(
                    job_manager.parser.parse_intent(prompt_text, data_profile=data_profile, prompt_variant=template_value)
                )

                notices = extraction_notices(intent, data_frame, job_manager.pbix_builder)
                notice_text = "\n\n".join(f"- {notice}" for notice in notices)

                metrics_df, dimensions_df, visuals_df = intent_to_tables(intent)
                return (
                    notice_text,
                    metrics_df,
                    dimensions_df,
                    visuals_df,
                    intent,
                    upload_path,
                    gr.update(interactive=True),
                    "",
                    READY_TO_GENERATE_TEXT,
                )
            except Exception as exc:
                logger.exception("Intent extraction failed")
                message = friendly_error_message(str(exc))
                return (
                    "",
                    *empty_tables,
                    None,
                    None,
                    gr.update(interactive=False),
                    message,
                    WAITING_FOR_EXTRACTION_TEXT,
                )

        extract_button.click(
            fn=_extract,
            inputs=[prompt, template, data_file],
            outputs=[
                extraction_notice,
                metrics_table,
                dimensions_table,
                visuals_table,
                parsed_intent_state,
                upload_path_state,
                generate_button,
                extract_error_banner,
                review_status,
            ],
        )

        def _generate(
            prompt_text: str,
            template_value: str,
            base_intent: IntentResult | None,
            upload_path: Path | None,
            metrics_df,
            dimensions_df,
            visuals_df,
        ):
            if base_intent is None:
                yield NOT_STARTED_STATUS_TEXT, 0, None, "Click **1. Extract Dashboard Plan** first."
                return

            try:
                edited_intent = tables_to_intent(base_intent, metrics_df, dimensions_df, visuals_df)
                job = job_manager.submit_generation(
                    prompt=prompt_text,
                    template=template_value,
                    uploaded_file=upload_path,
                    include_sample_data=upload_path is None,
                    preparsed_intent=edited_intent,
                )
            except Exception as exc:
                logger.exception("Failed to submit generation job")
                yield NOT_STARTED_STATUS_TEXT, 0, None, friendly_error_message(str(exc))
                return

            current_progress = 0
            current_status = "pending"
            while True:
                job_status = job_manager.get_status(job.job_id)
                current_progress = job_status.progress
                current_status = job_status.status
                if job_status.status in {"complete", "failed"}:
                    break
                yield f"Generating... ({job_status.status})", current_progress, None, ""
                time.sleep(0.5)

            if current_status == "failed":
                job_record = job_manager.get_job(job.job_id)
                yield (
                    "Generation failed.",
                    current_progress,
                    None,
                    friendly_error_message(job_record.error or ""),
                )
                return

            file_path = str(job_manager.get_download_path(job.job_id))
            yield "Done! Your dashboard is ready to download below.", current_progress, file_path, ""

        generate_button.click(
            fn=_generate,
            inputs=[prompt, template, parsed_intent_state, upload_path_state, metrics_table, dimensions_table, visuals_table],
            outputs=[status, progress, output_file, generate_error_banner],
        )

    return demo


def launch() -> None:
    """Launch the Gradio interface with the intended theme/styling applied.

    Gradio 6 moved theme/css from the Blocks() constructor to .launch() --
    passing them to Blocks() is silently ignored (a console warning, not an
    error), which is why the intended dark-navy-on-light styling wasn't
    rendering at all.
    """

    interface = create_interface()
    interface.launch(theme=gr.themes.Soft(primary_hue="blue", secondary_hue="emerald"), css=BRAND_CSS)


if __name__ == "__main__":
    launch()
