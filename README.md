# Prompt2PBI

Prompt2PBI is a browser-based application that turns plain-English dashboard requests into Power BI `.pbix` files.

## What Is Included

- FastAPI API layer with generation, status, download, templates, and health endpoints
- Gradio web interface with prompt entry, template selection, file upload, and download flow
- Async job management with a Celery-compatible task path and local fallback execution
- PBIX builder integration with `pbix-mcp` plus a local fallback backend for development
- CSV/Excel upload handling, validation, and sample-data generation
- Structured logging, request IDs, CORS, API key checks, and rate limiting

## Running Locally

1. Create a virtual environment and install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and configure any optional keys.

3. Start the API:

```bash
uvicorn app.main:app --reload
```

4. Launch the Gradio UI:

```bash
python -m ui.gradio_app
```

## Deployment

- Development environment: `.env.dev`
- Staging environment: `.env.staging`
- Production environment: `.env.prod`

See [DEPLOYMENT.md](DEPLOYMENT.md) for Docker Compose, single-server, and Kubernetes instructions.

## API Endpoints

- `POST /api/generate`
- `POST /api/generate-with-data`
- `GET /api/job/{job_id}/status`
- `GET /api/job/{job_id}/download`
- `GET /api/templates`
- `GET /api/health`

## Notes

- If `APP_API_KEY` is set, all `/api/*` job endpoints (generate, status, download) and the `/ws/status` websocket (via `?api_key=`) require it.
- Job state is stored in Redis when `REDIS_URL` is reachable, so multiple web replicas and Celery workers can share the same jobs; it falls back to in-process memory (single instance only) when Redis isn't reachable.
- If `pbix-mcp` is installed, the builder uses it automatically.
- If `pbix-mcp` is not installed, the project falls back to a local scaffold artifact writer so the app remains testable in development.

## PBIX rendering: visual titles, measure number formats, and report theme

`pbix-mcp` 0.9.2's high-level API (`add_table`/`add_measure`/`add_page`/`build`) does not expose a way to set a visual's on-canvas title, a measure's number-format string, or a report theme. All three were previously logged as "not fixable through this integration." They are fixed as of this pass -- **read this before re-investigating or re-accepting the limitation.**

**What was actually checked** (not assumed from the high-level function signatures):
- `add_measure()` has no `format_string` parameter, *and* the `INSERT INTO [Measure]` statement in `pbix_mcp/builder.py` (`_modify_metadata_and_encode`) hardcodes `FormatString` to the SQL literal `NULL` -- so even a hypothetical added parameter would never reach the built file.
- `_build_layout()` only ever writes `{"visualType", "projections", "prototypeQuery"}` into a visual's `singleVisual` config -- it never reads a title/objects key from what we pass to `add_page()`, so no argument combination reaches the layout.
- `PBIXBuilder.build()` calls `build_pbix_clean(datamodel_bytes, layout_bytes)` with no `theme_json`, even though that function accepts one. Swapping in `builder_v2.build_pbix_clean` directly was evaluated and rejected: `build()` is one monolithic method with no earlier exit point, so using that parameter would mean reimplementing all of `build()`'s orchestration (metadata SQLite creation, VertiPaq encoding, ABF construction, layout building) in our own code -- a much larger, more fragile change than patching the complete output.

**How it's actually fixed** (`app/services/pbix_builder.py`, `_postprocess_pbix_bytes` and its helpers, called from `_save_backend`): pbix-mcp ships (and uses internally, e.g. in its own `validate()` and its `pbix_set_theme`/`pbix_format_visual` MCP tools) a documented round-trip toolkit for editing an already-built PBIX -- `formats.datamodel_roundtrip.{decompress_datamodel, compress_datamodel}` and `formats.abf_rebuild.rebuild_abf_with_modified_sqlite()` (purpose-built to run arbitrary SQL against the embedded metadata database and rebuild the container). The exact JSON shapes for a visual title (`singleVisual.vcObjects.title`) and a report theme (`resourcePackages` + `config.themeCollection`) were taken directly from pbix-mcp's own `server.py`, not guessed. After `backend.build()` returns the complete PBIX bytes, this project:
1. Decompresses `DataModel`, runs `UPDATE [Measure] SET [FormatString] = ? WHERE [Name] = ?` for every measure via `rebuild_abf_with_modified_sqlite`, recompresses.
2. Rewrites `Report/Layout` to inject a `vcObjects.title` per visual (from `VisualDefinition.config["title"]`) and to register a fixed brand theme (indigo accent, see `PBIXBuilder._DEFAULT_THEME`) via `resourcePackages`/`config.themeCollection`, and adds the theme JSON file to the ZIP at `Report/StaticResources/SharedResources/BaseThemes/CY24SU11.json`.
3. Re-zips, keeping every other entry untouched.

If a patch step fails or pbix-mcp's internal format ever changes shape, `_postprocess_pbix_bytes` logs a warning and returns the *unpatched* bytes rather than failing generation -- basic PBIX output is never blocked by this.

**Verified by**: `tests/test_pbix_postprocessing.py` builds a real file with the real `pbix_mcp.builder.PBIXBuilder` (not the local fallback), then directly inspects the embedded SQLite (`FormatString` column) and the decoded `Report/Layout` JSON to confirm the values are actually present, and re-runs pbix-mcp's own `validate()` against the patched bytes to confirm nothing was corrupted.

**Not verified, and not verifiable in this environment**: actual visual rendering in Power BI Desktop (not installed here -- no GUI Windows app, can't screenshot it). The JSON/SQLite structures are confirmed correct and match pbix-mcp's own construction code for the same features; whether Power BI Desktop's renderer accepts them exactly as expected still wants a human opening the file at least once.

Separately, `requirements.txt` previously pinned `pbix-mcp==0.1.0`, a version that does not exist on PyPI (real versions start at 0.2.0) -- a fresh `pip install -r requirements.txt` would have failed outright. Now pinned to `0.9.2`, the version actually installed and tested against in this pass.
