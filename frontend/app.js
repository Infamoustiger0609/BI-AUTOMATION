// Prompt2PBI standalone frontend -- talks to the existing FastAPI backend
// (/api/extract, /api/generate-with-plan, /api/job/{id}/status,
// /api/job/{id}/download, /api/templates) over plain fetch(). No build step,
// no framework runtime -- just this file and index.html.

const API_KEY_STORAGE_KEY = "prompt2pbi_api_key";

// Ratio-style metrics (Profit Margin %, Average Order Value, ...) aren't a
// value from any single column, so they use Numerator/Denominator Column
// instead of Source Column -- keep in sync with dashboard_review.py's
// METRICS_COLUMNS, which is what /api/extract's response records use.
const METRICS_COLUMNS = ["Metric Name", "Type", "Source Column", "Numerator Column", "Denominator Column", "Description"];
const DIMENSIONS_COLUMNS = ["Dimension Name", "Type", "Grain", "Source Column"];
const VISUALS_COLUMNS = ["Chart Type", "Metric", "Dimension", "Title"];

const EXAMPLE_PROMPTS = [
  {
    label: "Sales performance (5 KPIs + 2 charts)",
    text:
      "Create a sales performance dashboard with:\n" +
      "5 KPIs: Total Revenue, Total Profit, Profit Margin %, Number of Orders, Average Order Value\n" +
      "2 graphs:\n" +
      "  1. Monthly Revenue trend (line chart by Date)\n" +
      "  2. Revenue by Region (bar chart)\n" +
      "Dimensions: Region, Product Category\n" +
      "Time grain: monthly",
  },
  { label: "Financial dashboard", text: "Build a financial dashboard showing revenue, profit, budget variance, and monthly trends." },
  { label: "Operations dashboard", text: "Design an operations dashboard for backlog, SLA, and throughput tracking." },
];

const state = {
  baseIntent: null,
  uploadPath: null,
};

function apiKeyHeaders() {
  const key = localStorage.getItem(API_KEY_STORAGE_KEY);
  return key ? { "X-API-Key": key } : {};
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function setStepIndicator(step) {
  for (let i = 1; i <= 3; i++) {
    const el = document.getElementById(`step-indicator-${i}`);
    const badge = el.querySelector("span");
    const active = i <= step;
    el.classList.toggle("text-indigo-700", active);
    el.classList.toggle("text-slate-400", !active);
    badge.classList.toggle("bg-indigo-600", active);
    badge.classList.toggle("text-white", active);
    badge.classList.toggle("bg-slate-300", !active);
    badge.classList.toggle("text-slate-600", !active);
  }
}

function renderExampleButtons() {
  const container = document.getElementById("example-buttons");
  EXAMPLE_PROMPTS.forEach((ex) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = ex.label;
    btn.className =
      "rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-100";
    btn.addEventListener("click", () => {
      document.getElementById("prompt-input").value = ex.text;
    });
    container.appendChild(btn);
  });
}

async function loadTemplates() {
  try {
    const res = await fetch("/api/templates", { headers: apiKeyHeaders() });
    if (!res.ok) return;
    const templates = await res.json();
    const select = document.getElementById("template-select");
    select.innerHTML = "";
    templates.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.id;
      if (t.id === "general") opt.selected = true;
      select.appendChild(opt);
    });
  } catch (e) {
    console.warn("Failed to load templates, keeping default option", e);
  }
}

function buildTableHead(rowEl, columns) {
  rowEl.innerHTML = "";
  columns.forEach((col) => {
    const th = document.createElement("th");
    th.className = "px-3 py-2 font-semibold border-b border-slate-200";
    th.textContent = col;
    rowEl.appendChild(th);
  });
}

function buildTableBody(bodyEl, columns, rows) {
  bodyEl.innerHTML = "";
  rows.forEach((row, rowIndex) => {
    const tr = document.createElement("tr");
    tr.className = rowIndex % 2 === 0 ? "bg-white" : "bg-slate-50";
    columns.forEach((col) => {
      const td = document.createElement("td");
      td.className = "px-3 py-2 align-top";
      const input = document.createElement("input");
      input.type = "text";
      input.value = row[col] ?? "";
      input.dataset.col = col;
      td.appendChild(input);
      tr.appendChild(td);
    });
    bodyEl.appendChild(tr);
  });
}

function readTableBody(bodyEl, columns) {
  const rows = [];
  bodyEl.querySelectorAll("tr").forEach((tr) => {
    const row = {};
    let hasValue = false;
    tr.querySelectorAll("input").forEach((input) => {
      row[input.dataset.col] = input.value;
      if (input.value.trim() !== "") hasValue = true;
    });
    if (hasValue) rows.push(row);
  });
  return rows;
}

function renderReviewTables(metrics, dimensions, visuals) {
  buildTableHead(document.getElementById("metrics-head"), METRICS_COLUMNS);
  buildTableBody(document.getElementById("metrics-body"), METRICS_COLUMNS, metrics);

  buildTableHead(document.getElementById("dimensions-head"), DIMENSIONS_COLUMNS);
  buildTableBody(document.getElementById("dimensions-body"), DIMENSIONS_COLUMNS, dimensions);

  buildTableHead(document.getElementById("visuals-head"), VISUALS_COLUMNS);
  buildTableBody(document.getElementById("visuals-body"), VISUALS_COLUMNS, visuals);

  document.getElementById("review-tables").classList.remove("hidden");
  document.getElementById("review-hint").classList.add("hidden");
}

function setGenerateEnabled(enabled) {
  const btn = document.getElementById("generate-button");
  btn.disabled = !enabled;
  btn.className = enabled
    ? "mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-bold text-white hover:bg-indigo-700 transition-colors"
    : "mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-300 px-4 py-2.5 text-sm font-bold text-slate-600 cursor-not-allowed transition-colors";
}

function showNotices(notices) {
  const el = document.getElementById("extract-notices");
  if (!notices || notices.length === 0) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.innerHTML = "<ul class='list-disc list-inside space-y-1'>" + notices.map((n) => `<li>${escapeHtml(n)}</li>`).join("") + "</ul>";
  el.classList.remove("hidden");
}

function showError(elId, message) {
  const el = document.getElementById(elId);
  if (!message) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.textContent = message;
  el.classList.remove("hidden");
}

function setStatus(text, progress) {
  document.getElementById("status-text").textContent = text;
  document.getElementById("progress-bar").style.width = `${progress}%`;
  document.getElementById("progress-label").textContent = `${progress}%`;
}

async function extractPlan() {
  const button = document.getElementById("extract-button");
  const prompt = document.getElementById("prompt-input").value.trim();
  const template = document.getElementById("template-select").value;
  const fileInput = document.getElementById("file-input");

  showError("extract-error", "");
  showNotices([]);

  if (prompt.length < 3) {
    showError("extract-error", "Please enter a more detailed prompt describing the dashboard you want.");
    return;
  }

  button.disabled = true;
  const originalHtml = button.innerHTML;
  button.innerHTML = '<span class="spinner" style="border-top-color:#4338ca;border-color:rgba(67,56,202,0.3);"></span><span>Extracting...</span>';

  try {
    const formData = new FormData();
    formData.append("prompt", prompt);
    formData.append("template", template);
    if (fileInput.files.length > 0) {
      formData.append("file", fileInput.files[0]);
    }

    const res = await fetch("/api/extract", { method: "POST", body: formData, headers: apiKeyHeaders() });
    const data = await res.json();

    if (!res.ok) {
      showError("extract-error", data.error || "Something went wrong while extracting the plan.");
      setGenerateEnabled(false);
      return;
    }

    state.baseIntent = data.base_intent;
    state.uploadPath = data.upload_path;

    renderReviewTables(data.metrics, data.dimensions, data.visuals);
    showNotices(data.notices);
    setGenerateEnabled(true);
    setStepIndicator(2);
  } catch (e) {
    showError("extract-error", "Couldn't reach the server. Please check your connection and try again.");
    setGenerateEnabled(false);
  } finally {
    button.disabled = false;
    button.innerHTML = originalHtml;
  }
}

async function pollJobStatus(jobId) {
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const res = await fetch(`/api/job/${jobId}/status`, { headers: apiKeyHeaders() });
    const status = await res.json();
    if (!res.ok) {
      showError("generate-error", status.message || "Lost track of the generation job.");
      return;
    }
    setStatus(`Generating... (${status.status})`, status.progress ?? 0);

    if (status.status === "complete") {
      setStatus("Done! Your dashboard is ready to download below.", 100);
      const downloadArea = document.getElementById("download-area");
      document.getElementById("download-link").href = `/api/job/${jobId}/download`;
      downloadArea.classList.remove("hidden");
      return;
    }
    if (status.status === "failed") {
      setStatus("Generation failed.", status.progress ?? 100);
      showError("generate-error", status.error || "Dashboard generation failed. Please review the plan and try again.");
      return;
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

async function generateDashboard() {
  const button = document.getElementById("generate-button");
  if (!state.baseIntent) {
    showError("generate-error", 'Click "1. Extract Dashboard Plan" first.');
    return;
  }

  showError("generate-error", "");
  const metrics = readTableBody(document.getElementById("metrics-body"), METRICS_COLUMNS);
  const dimensions = readTableBody(document.getElementById("dimensions-body"), DIMENSIONS_COLUMNS);
  const visuals = readTableBody(document.getElementById("visuals-body"), VISUALS_COLUMNS);

  const wasEnabled = !button.disabled;
  button.disabled = true;
  const originalHtml = button.innerHTML;
  button.innerHTML = '<span class="spinner" style="border-top-color:#ffffff;"></span><span>Submitting...</span>';

  document.getElementById("download-area").classList.add("hidden");
  setStatus("Submitting your dashboard for generation...", 0);

  try {
    const res = await fetch("/api/generate-with-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...apiKeyHeaders() },
      body: JSON.stringify({
        prompt: document.getElementById("prompt-input").value.trim(),
        template: document.getElementById("template-select").value,
        upload_path: state.uploadPath,
        base_intent: state.baseIntent,
        metrics,
        dimensions,
        visuals,
      }),
    });
    const data = await res.json();

    if (!res.ok) {
      showError("generate-error", data.error || "Something went wrong while starting generation.");
      setStatus('Not started yet — click "2. Generate Dashboard" above once you\'re happy with the plan.', 0);
      return;
    }

    setStepIndicator(3);
    await pollJobStatus(data.job_id);
  } catch (e) {
    showError("generate-error", "Couldn't reach the server. Please check your connection and try again.");
  } finally {
    button.disabled = !wasEnabled ? true : false;
    button.innerHTML = originalHtml;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  renderExampleButtons();
  loadTemplates();
  document.getElementById("extract-button").addEventListener("click", extractPlan);
  document.getElementById("generate-button").addEventListener("click", generateDashboard);
});
