"""
Intent parsing using LLM API.
Extracts structured dashboard requirements from natural language.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.models.intent import (
    DataSourceSpec,
    DimensionSpec,
    FilterSpec,
    IntentExtractionPayload,
    IntentResult,
    MetricSpec,
    RelationshipSpec,
    TableSpec,
    VisualSpec,
)
from app.utils.validators import validate_prompt


@dataclass(slots=True)
class CacheEntry:
    """In-memory cache entry with a TTL."""

    value: IntentResult
    expires_at: float


@dataclass(slots=True)
class CircuitBreakerState:
    """Simple circuit breaker state for provider reliability."""

    failure_count: int = 0
    opened_until: float = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.opened_until

    def record_success(self) -> None:
        self.failure_count = 0
        self.opened_until = 0.0

    def record_failure(self, threshold: int, cooldown_seconds: int) -> bool:
        self.failure_count += 1
        if self.failure_count >= threshold:
            self.opened_until = time.monotonic() + cooldown_seconds
            return True
        return False


class IntentParser:

    MAX_CACHE_ENTRIES = 500

    def _clean_column_name(self, name: str) -> str:
        """Convert a display name to a valid column name."""
        import re
        # Lowercase, replace spaces with underscores, remove special chars
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', '', name)  # Remove special chars
        cleaned = cleaned.lower().strip().replace(' ', '_')
        return cleaned or 'column'

    PROMPT_VARIANTS: dict[str, str] = {
        "general": """
Focus on broad business dashboards with balanced recommendations across metrics,
dimensions, visuals, filters, and relationships.
""".strip(),
        "financial": """
Prioritize financial planning and performance analysis. Emphasize revenue,
margin, cost, profit, variance, budget, forecast, and month-over-month analysis.
""".strip(),
        "sales": """
Prioritize sales performance analysis. Emphasize pipeline, revenue, conversion,
region, product, customer segment, and trend analysis.
""".strip(),
        "operational": """
Prioritize operations monitoring. Emphasize throughput, SLA, backlog, cycle time,
service levels, queues, and process bottlenecks.
""".strip(),
        "executive": """
Prioritize executive-level dashboards. Emphasize high-level KPIs, summary visuals,
exception highlighting, and drill-down paths.
""".strip(),
    }

    FEW_SHOT_EXAMPLES: dict[str, list[tuple[str, str]]] = {
        "general": [
            (
                "Create a dashboard to track store performance by region and product.",
                json.dumps(
                    {
                        "dashboard_title": "Store Performance",
                        "time_grain": "monthly",
                        "metrics": [
                            {
                                "name": "Total Sales",
                                "type": "sum",
                                "description": "Total sales amount.",
                                "source_column": "Sales",
                            }
                        ],
                        "dimensions": [
                            {"name": "Region", "type": "categorical", "grain": "none"},
                            {"name": "Product", "type": "categorical", "grain": "none"},
                        ],
                        "visuals": [
                            {"type": "bar_chart", "metric": "Total Sales", "dimension": "Region"},
                            {"type": "table", "metric": "Total Sales", "dimension": "Product"},
                        ],
                        "filters": [],
                        "data_sources": ["sales"],
                        "suggested_tables": [{"name": "Sales", "columns": ["Date", "Region", "Product", "Sales"]}],
                        "suggested_relationships": [],
                    },
                    indent=2,
                ),
            )
        ],
        "sales": [
            (
                "Show monthly revenue trends and regional breakdowns.",
                json.dumps(
                    {
                        "dashboard_title": "Sales Dashboard",
                        "time_grain": "monthly",
                        "metrics": [{"name": "Total Revenue", "type": "sum", "description": "Revenue across the selected period."}],
                        "dimensions": [{"name": "Region", "type": "categorical", "grain": "none"}],
                        "visuals": [{"type": "line_chart", "metric": "Total Revenue", "dimension": "Date"}],
                        "filters": [],
                        "data_sources": ["sales", "product"],
                        "suggested_tables": [{"name": "Sales", "columns": ["Date", "Region", "Revenue"]}],
                        "suggested_relationships": [],
                    },
                    indent=2,
                ),
            )
        ],
        "financial": [
            (
                "Analyze revenue, profit, and budget variance by month.",
                json.dumps(
                    {
                        "dashboard_title": "Finance Dashboard",
                        "time_grain": "monthly",
                        "metrics": [
                            {"name": "Revenue", "type": "sum", "description": "Total revenue."},
                            {"name": "Profit", "type": "sum", "description": "Total profit."},
                            {"name": "Budget Variance", "type": "ratio", "description": "Actual vs budget variance."},
                        ],
                        "dimensions": [{"name": "Date", "type": "date", "grain": "monthly"}],
                        "visuals": [{"type": "line_chart", "metric": "Revenue", "dimension": "Date"}],
                        "filters": [],
                        "data_sources": ["finance", "budget"],
                        "suggested_tables": [{"name": "Finance", "columns": ["Date", "Revenue", "Profit", "Budget"]}],
                        "suggested_relationships": [],
                    },
                    indent=2,
                ),
            )
        ],
        "operational": [
            (
                "Track SLA compliance and backlog trends for support operations.",
                json.dumps(
                    {
                        "dashboard_title": "Operations Dashboard",
                        "time_grain": "daily",
                        "metrics": [
                            {"name": "SLA Compliance", "type": "percentage", "description": "Percent of items meeting SLA."},
                            {"name": "Backlog", "type": "count", "description": "Open backlog items."},
                        ],
                        "dimensions": [{"name": "Date", "type": "date", "grain": "daily"}],
                        "visuals": [{"type": "line_chart", "metric": "Backlog", "dimension": "Date"}],
                        "filters": [],
                        "data_sources": ["operations", "support"],
                        "suggested_tables": [{"name": "Operations", "columns": ["Date", "Backlog", "SLA"]}],
                        "suggested_relationships": [],
                    },
                    indent=2,
                ),
            )
        ],
        "executive": [
            (
                "Provide an executive overview of KPIs and trends.",
                json.dumps(
                    {
                        "dashboard_title": "Executive Overview",
                        "time_grain": "monthly",
                        "metrics": [
                            {"name": "Total KPI", "type": "sum", "description": "Primary strategic KPI."}
                        ],
                        "dimensions": [{"name": "Date", "type": "date", "grain": "monthly"}],
                        "visuals": [{"type": "card", "metric": "Total KPI", "dimension": None}],
                        "filters": [],
                        "data_sources": ["executive"],
                        "suggested_tables": [{"name": "ExecutiveKPIs", "columns": ["Date", "KPI"]}],
                        "suggested_relationships": [],
                    },
                    indent=2,
                ),
            )
        ],
    }

    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        settings: Settings | None = None,
        max_retries: int | None = None,
        timeout_seconds: int | None = None,
        cache_ttl_seconds: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = (provider or self.settings.llm_provider).lower()
        self.model_name = model_name or self.settings.llm_model
        self.max_retries = max_retries or self.settings.llm_max_retries
        self.timeout_seconds = timeout_seconds or self.settings.llm_timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds or self.settings.llm_cache_ttl_seconds
        self.logger = logger or logging.getLogger(__name__)
        self.cache: dict[str, CacheEntry] = {}
        self.circuit_breaker = CircuitBreakerState()
        self.metrics: Counter[str] = Counter()
        self._lock = threading.RLock()

        self.logger.info(
            "IntentParser initialized",
            extra={
                "provider": self.provider,
                "model_name": self.model_name,
                "timeout_seconds": self.timeout_seconds,
                "cache_ttl_seconds": self.cache_ttl_seconds,
            },
        )

    def build_prompt(
        self,
        user_prompt: str,
        data_profile: dict[str, Any] | None = None,
        prompt_variant: str = "general",
    ) -> str:
        """Build the structured prompt sent to the LLM."""

        variant = self._normalize_variant(prompt_variant, user_prompt)
        examples = self.FEW_SHOT_EXAMPLES.get(variant, self.FEW_SHOT_EXAMPLES["general"])
        context = self.PROMPT_VARIANTS.get(variant, self.PROMPT_VARIANTS["general"])
        data_profile_json = json.dumps(data_profile or {}, indent=2, default=str)

        example_block = "\n\n".join(
            f"User Request: {example_prompt}\nJSON:\n{example_json}"
            for example_prompt, example_json in examples
        )

        return f"""
You are an expert Power BI dashboard designer. Parse the following user request and extract structured dashboard requirements.

Return ONLY valid JSON with this structure:
{{
  "dashboard_title": "string",
  "summary": "string",
  "time_grain": "daily|weekly|monthly|quarterly|yearly|mixed|unknown",
  "metrics": [
    {{"name": "Total Revenue", "type": "sum", "description": "...", "source_column": "Revenue"}},
    {{"name": "Profit Margin %", "type": "percentage", "description": "...", "numerator_column": "Profit", "denominator_column": "Revenue"}}
  ],
  "dimensions": [
    {{"name": "Region", "type": "categorical", "values": [], "grain": "none"}}
  ],
  "visuals": [
    {{"type": "bar_chart", "metric": "Total Revenue", "dimension": "Region", "title": "..."}}
  ],
  "filters": [
    {{"field": "Region", "operator": "equals", "value": "West", "description": "..."}}
  ],
  "data_sources": ["sales", "customer", "product"],
  "suggested_tables": [
    {{"name": "Sales", "columns": ["Date", "ProductID", "CustomerID", "Revenue"]}}
  ],
  "suggested_relationships": [
    {{"from_field": "Sales.ProductID", "to_field": "Product.ProductID", "cardinality": "many-to-one"}}
  ]
}}

Important: some metrics are a ratio of two columns, not a value from any
single column -- e.g. "Profit Margin %" (Profit/Revenue), "Average Order
Value" (Revenue/Orders), "Conversion Rate" (Conversions/Visits), "Cost per
Order" (Cost/Orders). For those, set "numerator_column" and
"denominator_column" to real columns from the data profile below (type
"percentage" or "ratio", or "average" for a derived per-unit value like
Average Order Value) and omit "source_column". Only use "source_column" for
a metric that maps directly to one real column (sum, count, or a plain
average of one column).

Prompt Variant: {variant}

{context}

Few-shot examples:
{example_block}

Data profile:
{data_profile_json}

User Request: {user_prompt.strip()}
""".strip()

    async def parse_intent(
        self,
        user_prompt: str,
        data_profile: dict[str, Any] | None = None,
        prompt_variant: str = "general",
        use_cache: bool = True,
    ) -> IntentResult:
        """Parse a prompt into structured dashboard intent."""

        prompt = validate_prompt(user_prompt)
        variant = self._normalize_variant(prompt_variant, prompt)
        cache_key = self._cache_key(prompt, data_profile, variant)

        if use_cache:
            cached = self._get_cache(cache_key)
            if cached is not None:
                self._metric_incr("cache_hits")
                self.logger.debug(
                    "Intent cache hit",
                    extra={"prompt_variant": variant, "cache_key": cache_key},
                )
                return cached
            self._metric_incr("cache_misses")

        if self._is_circuit_open():
            self._metric_incr("circuit_open_fallbacks")
            self.logger.warning(
                "Circuit breaker open, using fallback intent extraction",
                extra={"prompt_variant": variant},
            )
            result = self._fallback_extract(prompt, data_profile, variant, reason="circuit_open")
            self._store_cache(cache_key, result)
            return result

        start = time.monotonic()
        try:
            raw_response = await self._generate_with_retries(prompt, data_profile, variant)
            payload = self._parse_llm_response(raw_response)
            result = self._payload_to_result(
                payload=payload,
                user_prompt=prompt,
                raw_response=raw_response,
                prompt_variant=variant,
            )
            self._record_success()
            self._metric_incr("provider_success")
            self._metric_set("last_latency_ms", int((time.monotonic() - start) * 1000))
            self._store_cache(cache_key, result)
            self.logger.info(
                "Intent parsed successfully",
                extra={
                    "provider": self.provider,
                    "prompt_variant": variant,
                    "metrics_count": len(result.metrics),
                    "dimensions_count": len(result.dimensions),
                    "visuals_count": len(result.visuals),
                },
            )
            return result
        except Exception as exc:
            self._metric_incr("provider_failures")
            self._register_failure()
            self.logger.exception(
                "Intent parsing failed, falling back to heuristic extraction",
                extra={"prompt_variant": variant},
            )
            result = self._fallback_extract(
                prompt,
                data_profile,
                variant,
                reason=type(exc).__name__,
            )
            self._store_cache(cache_key, result)
            return result

    async def _generate_with_retries(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
        prompt_variant: str,
    ) -> str:
        """Invoke the selected provider with retries and exponential backoff."""

        prompt_text = self.build_prompt(prompt, data_profile, prompt_variant)
        provider_order = self._provider_order()
        if not provider_order:
            raise RuntimeError("No LLM provider is configured (missing GEMINI_API_KEY/OPENAI_API_KEY).")
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            for provider_name in provider_order:
                try:
                    self._metric_incr("provider_calls")
                    return await asyncio.wait_for(
                        self._generate_with_provider(provider_name, prompt_text),
                        timeout=self.timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    last_error = exc
                    self._metric_incr("timeouts")
                    self.logger.warning(
                        "LLM provider timed out",
                        extra={"provider": provider_name, "attempt": attempt + 1},
                    )
                except Exception as exc:
                    last_error = exc
                    if self._is_rate_limit_error(exc):
                        self._metric_incr("rate_limits")
                        self.logger.warning(
                            "LLM provider rate limited",
                            extra={"provider": provider_name, "attempt": attempt + 1},
                        )
                    else:
                        self.logger.warning(
                            "LLM provider call failed",
                            extra={
                                "provider": provider_name,
                                "attempt": attempt + 1,
                                "error_type": type(exc).__name__,
                            },
                        )

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._backoff_seconds(attempt))

        if last_error is not None:
            raise last_error
        raise RuntimeError("No provider response was produced.")

    async def _generate_with_provider(self, provider_name: str, prompt_text: str) -> str:
        """Generate raw text from the configured provider."""

        if provider_name == "gemini":
            return await self._generate_with_gemini(prompt_text)
        if provider_name == "openai":
            return await self._generate_with_openai(prompt_text)
        raise ValueError(f"Unsupported LLM provider: {provider_name}")

    async def _generate_with_gemini(self, prompt_text: str) -> str:
        """Generate a response using Google Gemini."""

        api_key = self._secret_value(self.settings.gemini_api_key)
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("google-genai is not installed.") from exc

        def _invoke() -> str:
            client=genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt_text
            )
            return self._extract_text_from_response(response)

        return await asyncio.to_thread(_invoke)

    async def _generate_with_openai(self, prompt_text: str) -> str:
        """Generate a response using OpenAI."""

        api_key = self._secret_value(self.settings.openai_api_key)
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("openai is not installed.") from exc

        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "You extract structured Power BI dashboard intent."},
                {"role": "user", "content": prompt_text},
            ],
            temperature=0.1,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise RuntimeError("OpenAI returned an empty response.")
        return str(content)

    def _parse_llm_response(self, raw_response: str) -> IntentExtractionPayload:
        """Parse and validate the LLM JSON response."""

        json_text = self._extract_json_block(raw_response)
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            self._metric_incr("json_parse_failures")
            raise ValueError("LLM response was not valid JSON.") from exc

        try:
            return IntentExtractionPayload.model_validate(payload)
        except Exception as exc:
            self._metric_incr("json_validation_failures")
            raise ValueError("LLM response JSON did not match the expected schema.") from exc

    def _extract_json_block(self, text: str) -> str:
        """Recover JSON from fenced or noisy model output.

        Looks for a ```-fenced block anywhere in the text (not just at the very
        start), then scans for the first *balanced* top-level {...} object
        within that block instead of greedily matching from the first "{" to
        the last "}" in the whole response, which would swallow any stray
        braces mentioned elsewhere in the model's prose.
        """

        cleaned = text.strip()

        fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        search_space = fence_match.group(1).strip() if fence_match else cleaned

        if search_space.startswith("{") and search_space.endswith("}"):
            return search_space

        candidate = self._extract_balanced_json_object(search_space)
        if candidate:
            return candidate

        raise ValueError("Unable to locate a JSON object in the LLM response.")

    def _extract_balanced_json_object(self, text: str) -> str | None:
        """Return the first balanced top-level {...} object in text, honoring string literals."""

        start = text.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : index + 1]
            start = text.find("{", start + 1)
        return None

    def _payload_to_result(
        self,
        payload: IntentExtractionPayload,
        user_prompt: str,
        raw_response: str,
        prompt_variant: str,
    ) -> IntentResult:
        """Convert the validated payload into the public result model."""

        target_audience = self._infer_audience(user_prompt)
        data_entities = self._entities_from_payload(payload, user_prompt)
        key_measures = [metric.name for metric in payload.metrics] or self._infer_measures(user_prompt)
        visual_recommendations = [visual.type for visual in payload.visuals] or self._infer_visuals(user_prompt)

        return IntentResult(
            dashboard_title=payload.dashboard_title,
            business_goal=payload.summary or self._infer_goal(user_prompt),
            executive_summary=payload.summary or user_prompt,
            time_grain=payload.time_grain,
            metrics=payload.metrics,
            dimensions=payload.dimensions,
            visuals=payload.visuals,
            filters=payload.filters,
            data_sources=[self._normalize_data_source(item) for item in payload.data_sources],
            suggested_tables=payload.suggested_tables,
            suggested_relationships=payload.suggested_relationships,
            target_audience=target_audience,
            data_entities=data_entities,
            recommended_pages=self._recommended_pages(user_prompt),
            key_measures=key_measures,
            visual_recommendations=visual_recommendations,
            notes=payload.notes,
            confidence=payload.confidence,
            raw_response=raw_response,
            provider=self.provider,
            prompt_variant=prompt_variant,
        )

    def _fallback_extract(
        self,
        user_prompt: str,
        data_profile: dict[str, Any] | None,
        prompt_variant: str,
        reason: str,
    ) -> IntentResult:
        """Deterministic fallback when the LLM cannot be used."""

        normalized = user_prompt.lower()
        metric_specs = self._infer_metrics(normalized, data_profile)
        dimension_specs = self._infer_dimensions(normalized, data_profile)
        visual_specs = self._infer_visual_specs(normalized, metric_specs, dimension_specs)
        filters = self._infer_filters(normalized)
        data_sources = self._infer_data_sources(normalized, data_profile)
        tables = self._infer_tables(normalized, data_profile)
        relationships = self._infer_relationships(tables, normalized)

        self._metric_incr("fallback_used")

        return IntentResult(
            dashboard_title=self._derive_title(user_prompt, prompt_variant),
            business_goal=self._infer_goal(user_prompt),
            executive_summary=user_prompt,
            time_grain=self._infer_time_grain(normalized),
            metrics=metric_specs,
            dimensions=dimension_specs,
            visuals=visual_specs,
            filters=filters,
            data_sources=data_sources,
            suggested_tables=tables,
            suggested_relationships=relationships,
            target_audience=self._infer_audience(user_prompt),
            data_entities=self._entities_from_strings(data_sources, tables),
            recommended_pages=self._recommended_pages(user_prompt),
            key_measures=[metric.name for metric in metric_specs],
            visual_recommendations=[visual.type for visual in visual_specs],
            notes=[
                f"Fallback intent extraction used because: {reason}",
                "LLM integration will be used when provider credentials are available.",
            ],
            confidence=0.35,
            raw_response=user_prompt,
            provider=self.provider,
            prompt_variant=prompt_variant,
        )

    def _derive_title(self, user_prompt: str, prompt_variant: str) -> str:
        """Derive a concise dashboard title from the prompt and variant."""

        lowered = user_prompt.lower()
        if "sales" in lowered or prompt_variant == "sales":
            return "Sales Dashboard"
        if any(keyword in lowered for keyword in ["finance", "profit", "budget", "margin"]) or prompt_variant == "financial":
            return "Finance Dashboard"
        if any(keyword in lowered for keyword in ["operations", "sla", "backlog", "throughput"]) or prompt_variant == "operational":
            return "Operations Dashboard"
        if "executive" in lowered or prompt_variant == "executive":
            return "Executive Overview"
        words = [word.strip(".,!?") for word in user_prompt.split()[:6]]
        return " ".join(words).title() or "Untitled Dashboard"

    def _infer_metrics(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
    ) -> list[MetricSpec]:
        candidates: list[MetricSpec] = []
        if any(keyword in prompt for keyword in ["revenue", "sales", "income"]):
            raw_column = self._match_column(data_profile, ["revenue", "sales", "amount"])
            candidates.append(
                MetricSpec(
                    name="Total Revenue",
                    type="sum",
                    description="Total revenue over the selected period.",
                    source_column=self._clean_column_name(raw_column or "total_revenue"),
                    aggregation="SUM",
                 )
             )
        if any(keyword in prompt for keyword in ["profit", "margin"]):
            raw_column = self._match_column(data_profile, ["profit", "margin"])
            candidates.append(
                MetricSpec(
                        name="Profit",
                        type="sum",
                        description="Net profit over the selected period.",
                        source_column=self._clean_column_name(raw_column or "profit"),
                        aggregation="SUM",
                    )
                )
        if any(keyword in prompt for keyword in ["order", "orders", "customers"]):
            raw_column = self._match_column(data_profile, ["order", "order_id"])
            candidates.append(
                MetricSpec(
                    name="Total Orders",
                    type="count",
                    description="Number of orders in scope.",
                    source_column=self._clean_column_name(raw_column or "total_orders"),
                    aggregation="COUNT",
                )
            )
        candidates.extend(self._infer_ratio_metrics(prompt, data_profile))
        if not candidates:
            raw_column = self._match_column(data_profile, ["id", "count"])
            candidates.append(
                MetricSpec(
                    name="Total Count",
                    type="count",
                    description="Default count metric inferred from the user request.",
                    source_column=self._clean_column_name(raw_column or "total_count"),
                    aggregation="COUNT",
                )
            )
        return candidates

    # Ratio/percentage-style KPIs aren't a value from any single column
    # (Profit Margin % = Profit/Revenue, Average Order Value = Revenue/
    # Orders) -- a data-driven template table instead of hardcoding just the
    # two names that originally surfaced this, so recognizing one more
    # common ratio phrasing is a new entry here, not new branching logic.
    # "prompt_keywords": any of these appearing in the (lowercased) prompt
    # triggers the template. "numerator_keywords"/"denominator_keywords" are
    # passed to _match_column() to resolve against the real uploaded/sample
    # data -- if a side doesn't match a real column, it's left unresolved
    # (None) rather than guessing, so find_unresolved_ratio_metrics() in
    # dashboard_review.py can flag exactly which side is the problem.
    _RATIO_METRIC_TEMPLATES: list[dict[str, Any]] = [
        {
            "name": "Profit Margin %",
            "type": "percentage",
            "prompt_keywords": ["profit margin", "margin %", "margin percentage"],
            "numerator_keywords": ["profit", "margin", "net income", "earnings"],
            "denominator_keywords": ["revenue", "sales", "income", "amount"],
            "description": "Profit as a percentage of revenue.",
        },
        {
            "name": "Average Order Value",
            "type": "average",
            "prompt_keywords": ["average order value", "aov"],
            "numerator_keywords": ["revenue", "sales", "amount", "total"],
            "denominator_keywords": ["order", "orders", "transaction", "transactions"],
            "description": "Average revenue per order.",
        },
        {
            "name": "Conversion Rate",
            "type": "percentage",
            "prompt_keywords": ["conversion rate", "conversion %", "conversion percentage"],
            "numerator_keywords": ["conversion", "converted", "purchase"],
            "denominator_keywords": ["visit", "visitor", "lead", "session", "click", "total"],
            "description": "Conversion ratio from the funnel.",
        },
        {
            "name": "Return Rate",
            "type": "percentage",
            "prompt_keywords": ["return rate", "return %", "return percentage"],
            "numerator_keywords": ["return", "returned", "refund"],
            "denominator_keywords": ["order", "orders", "sale", "sales", "total"],
            "description": "Percentage of orders returned.",
        },
        {
            "name": "Cost per Order",
            "type": "ratio",
            "prompt_keywords": ["cost per order"],
            "numerator_keywords": ["cost", "expense", "spend"],
            "denominator_keywords": ["order", "orders"],
            "description": "Average cost per order.",
        },
        {
            "name": "Customer Retention Rate",
            "type": "percentage",
            "prompt_keywords": ["retention rate", "customer retention"],
            "numerator_keywords": ["retained", "returning customer", "repeat customer"],
            "denominator_keywords": ["customer", "customers", "total customer"],
            "description": "Percentage of customers retained.",
        },
    ]

    def _infer_ratio_metrics(self, prompt: str, data_profile: dict[str, Any] | None) -> list[MetricSpec]:
        """Detect common ratio/percentage KPI requests by name and resolve
        numerator/denominator columns from the real data profile.

        A side that can't be matched to a real column is left as None here
        (not guessed) -- resolution against the real data, and reporting
        which side is unresolved, is dashboard_review.py's job at review
        time, not this best-effort text scan's.
        """

        metrics: list[MetricSpec] = []
        for template in self._RATIO_METRIC_TEMPLATES:
            if not any(keyword in prompt for keyword in template["prompt_keywords"]):
                continue
            numerator = self._match_column(data_profile, template["numerator_keywords"])
            denominator = self._match_column(data_profile, template["denominator_keywords"])
            metrics.append(
                MetricSpec(
                    name=template["name"],
                    type=template["type"],
                    description=template["description"],
                    numerator_column=self._clean_column_name(numerator) if numerator else None,
                    denominator_column=self._clean_column_name(denominator) if denominator else None,
                )
            )
        return metrics

    def _infer_dimensions(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
    ) -> list[DimensionSpec]:
        candidates: list[DimensionSpec] = []
        column_names = self._extract_profile_columns(data_profile)
        keyword_map = [
            ("region", "Region", "categorical"),
            ("product", "Product", "categorical"),
            ("category", "Category", "categorical"),
            ("customer", "Customer", "categorical"),
            ("channel", "Channel", "categorical"),
            ("date", "Date", "date"),
            ("month", "Date", "date"),
            ("quarter", "Date", "date"),
            ("year", "Date", "date"),
        ]

        for keyword, name, dim_type in keyword_map:
            if keyword in prompt or self._column_matches(column_names, keyword):
                grain = self._infer_time_grain(prompt) if dim_type == "date" else "none"
                candidates.append(
                    DimensionSpec(
                        name=name,
                        type=dim_type,  # type: ignore[arg-type]
                        values=[],
                        grain=grain if grain != "unknown" else "none",
                        source_column=self._clean_column_name(name),
                    )
                )

        if not candidates:
            candidates.append(DimensionSpec(name="Date", type="date", grain=self._infer_time_grain(prompt) if self._infer_time_grain(prompt) != "unknown" else "monthly"))
        return self._dedupe_by_name(candidates)

    def _infer_visual_specs(
        self,
        prompt: str,
        metrics: list[MetricSpec],
        dimensions: list[DimensionSpec],
    ) -> list[VisualSpec]:
        """Infer visuals with safety checks - no date visuals without date dims."""
        visuals: list[VisualSpec] = []
        metric_name = metrics[0].name if metrics else None
        dimension_name = next((d.name for d in dimensions if d.type != "date" and d.type != "temporal" and d.type !="time"),None)
        date_dimension_name = next((d.name for d in dimensions if d.type in ["date", "temporal", "time"]), None)
        

        # Only create trend visuals if we have a date dimension
        if date_dimension_name and ("trend" in prompt or "over time" in prompt or "monthly" in prompt or "daily" in prompt):
            
            visuals.append(
                VisualSpec(
                    type="line_chart",
                    metric=metric_name,
                    dimension=date_dimension_name,
                    title=f"{metric_name or 'Metric'} Trend",
                    description="Trend chart over time.",
                )
            )
            # If no date, skip trend visuals entirely

        if dimension_name and ("comparison" in prompt or "by" in prompt):
            visuals.append(
                VisualSpec(
                    type="bar_chart",
                    metric=metric_name,
                    dimension=dimension_name or "Region",
                    title=f"{metric_name or 'Metric'} by {dimension_name}",
                    description="Comparison across categories.",
                )
            )

        if "kpi" in prompt or "summary" in prompt or "executive" in prompt:
            visuals.append(
                VisualSpec(
                    type="card",
                    metric=metric_name,
                    dimension=None,
                    title=metric_name or "KPI",
                    description="Single-value KPI card.",
                )
            )

        if not visuals:
            # Fallback: only create a table if no other visuals
            visuals.append(
                VisualSpec(
                    type="table",
                    metric=metric_name,
                    dimension=dimension_name or "Category",
                    title="Data Table",
                    description="Fallback table visual.",
                )
            )
        return self._dedupe_visuals(visuals)

    def _infer_filters(self, prompt: str) -> list[FilterSpec]:
        filters: list[FilterSpec] = []
        region_match = re.search(r"\b(?:in|for|by)\s+([A-Za-z]+)\b", prompt)
        if region_match and region_match.group(1).lower() not in {"month", "year", "day", "week"}:
            filters.append(
                FilterSpec(
                    field="Region",
                    operator="equals",
                    value=region_match.group(1).title(),
                    description="Region filter inferred from prompt.",
                )
            )
        if "last month" in prompt:
            filters.append(
                FilterSpec(
                    field="Date",
                    operator="relative",
                    value="last month",
                    description="Relative date filter inferred from prompt.",
                )
            )
        if "this year" in prompt:
            filters.append(
                FilterSpec(
                    field="Date",
                    operator="relative",
                    value="this year",
                    description="Year-to-date filter inferred from prompt.",
                )
            )
        return filters

    def _infer_data_sources(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
    ) -> list[DataSourceSpec]:
        source_names = []
        if any(keyword in prompt for keyword in ["sales", "revenue", "order"]):
            source_names.append("sales")
        if any(keyword in prompt for keyword in ["customer", "segment", "retention"]):
            source_names.append("customer")
        if any(keyword in prompt for keyword in ["product", "sku", "category"]):
            source_names.append("product")
        if any(keyword in prompt for keyword in ["finance", "budget", "profit", "margin"]):
            source_names.append("finance")
        if any(keyword in prompt for keyword in ["operations", "sla", "backlog", "throughput"]):
            source_names.append("operations")
        if data_profile and isinstance(data_profile, dict) and data_profile.get("source_name"):
            source_names.append(str(data_profile["source_name"]))

        if not source_names:
            source_names.append("dataset")

        return [
            DataSourceSpec(
                name=name,
                description=self._describe_source(name),
                required_columns=self._required_columns_for_source(name),
            )
            for name in self._dedupe_strings(source_names)
        ]

    def _infer_tables(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
    ) -> list[TableSpec]:
        tables: list[TableSpec] = []
        primary_columns = self._extract_profile_columns(data_profile)
        if any(keyword in prompt for keyword in ["sales", "revenue", "order"]):
            tables.append(
                TableSpec(
                    name="Sales",
                    columns=self._columns_for_table(primary_columns, ["Date", "ProductID", "CustomerID", "Revenue", "Sales"]),
                )
            )
        if any(keyword in prompt for keyword in ["product", "sku", "category"]):
            tables.append(
                TableSpec(
                    name="Product",
                    columns=self._columns_for_table(primary_columns, ["ProductID", "ProductName", "Category"]),
                )
            )
        if any(keyword in prompt for keyword in ["customer", "segment", "retention"]):
            tables.append(
                TableSpec(
                    name="Customer",
                    columns=self._columns_for_table(primary_columns, ["CustomerID", "CustomerName", "Segment"]),
                )
            )
        if any(keyword in prompt for keyword in ["finance", "budget", "profit", "margin"]):
            tables.append(
                TableSpec(
                    name="Finance",
                    columns=self._columns_for_table(primary_columns, ["Date", "Revenue", "Profit", "Budget"]),
                )
            )
        if any(keyword in prompt for keyword in ["operations", "sla", "backlog", "throughput"]):
            tables.append(
                TableSpec(
                    name="Operations",
                    columns=self._columns_for_table(primary_columns, ["Date", "Backlog", "SLA", "Throughput"]),
                )
            )
        if not tables:
            tables.append(
                TableSpec(
                    name="Dataset",
                    columns=primary_columns[:6] if primary_columns else ["Date", "Category", "Value"],
                )
            )
        return self._dedupe_tables(tables)

    def _infer_relationships(self, tables: list[TableSpec], prompt: str) -> list[RelationshipSpec]:
        relationships: list[RelationshipSpec] = []
        table_names = {table.name for table in tables}

        if {"Sales", "Product"}.issubset(table_names):
            relationships.append(
                RelationshipSpec(
                    from_field="Sales.ProductID",
                    to_field="Product.ProductID",
                    cardinality="many-to-one",
                    description="Sales rows link to product dimension.",
                )
            )
        if {"Sales", "Customer"}.issubset(table_names):
            relationships.append(
                RelationshipSpec(
                    from_field="Sales.CustomerID",
                    to_field="Customer.CustomerID",
                    cardinality="many-to-one",
                    description="Sales rows link to customer dimension.",
                )
            )
        if {"Finance", "Customer"}.issubset(table_names):
            relationships.append(
                RelationshipSpec(
                    from_field="Finance.CustomerID",
                    to_field="Customer.CustomerID",
                    cardinality="many-to-one",
                    description="Financial records can be linked to customers.",
                )
            )
        if {"Operations", "Customer"}.issubset(table_names):
            relationships.append(
                RelationshipSpec(
                    from_field="Operations.CustomerID",
                    to_field="Customer.CustomerID",
                    cardinality="many-to-one",
                    description="Operational records can be linked to customers.",
                )
            )
        if "date" in prompt and tables:
            relationships.append(
                RelationshipSpec(
                    from_field=f"{tables[0].name}.Date",
                    to_field="DateDim.Date",
                    cardinality="many-to-one",
                    description="Date dimension relationship inferred from the prompt.",
                )
            )
        return self._dedupe_relationships(relationships)

    def get_metrics(self) -> dict[str, Any]:
        """Return parser metrics for monitoring."""

        with self._lock:
            return {
                "provider": self.provider,
                "model_name": self.model_name,
                "cache_entries": len(self.cache),
                "circuit_failure_count": self.circuit_breaker.failure_count,
                "circuit_open": self.circuit_breaker.is_open,
                **dict(self.metrics),
            }

    def clear_cache(self) -> None:
        """Clear the prompt cache."""

        with self._lock:
            self.cache.clear()

    def _store_cache(self, key: str, value: IntentResult) -> None:
        with self._lock:
            self.cache[key] = CacheEntry(
                value=value,
                expires_at=time.monotonic() + self.cache_ttl_seconds,
            )
            while len(self.cache) > self.MAX_CACHE_ENTRIES:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]

    def _get_cache(self, key: str) -> IntentResult | None:
        with self._lock:
            entry = self.cache.get(key)
            if entry is None:
                return None
            if time.monotonic() >= entry.expires_at:
                del self.cache[key]
                return None
            return entry.value

    def _is_circuit_open(self) -> bool:
        with self._lock:
            return self.circuit_breaker.is_open

    def _record_success(self) -> None:
        with self._lock:
            self.circuit_breaker.record_success()

    def _register_failure(self) -> None:
        with self._lock:
            opened = self.circuit_breaker.record_failure(
                self.settings.llm_circuit_breaker_threshold,
                self.settings.llm_circuit_breaker_cooldown_seconds,
            )
            if opened:
                self.metrics["circuit_opened"] += 1

    def _metric_incr(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.metrics[key] += amount

    def _metric_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.metrics[key] = value

    def _provider_order(self) -> list[str]:
        if self.provider == "auto":
            order = ["gemini", "openai"]
        elif self.provider == "openai":
            order = ["openai", "gemini"]
        else:
            order = ["gemini", "openai"]
        return [provider for provider in order if self._provider_available(provider)]

    def _provider_available(self, provider: str) -> bool:
        if provider == "gemini":
            return bool(self._secret_value(self.settings.gemini_api_key))
        if provider == "openai":
            return bool(self._secret_value(self.settings.openai_api_key))
        return False

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "rate limit" in message or "429" in message or "too many requests" in message

    def _backoff_seconds(self, attempt: int) -> float:
        return min(2.0 ** attempt, 8.0)

    def _normalize_variant(self, prompt_variant: str | None, user_prompt: str) -> str:
        if prompt_variant and prompt_variant in self.PROMPT_VARIANTS:
            return prompt_variant

        prompt = user_prompt.lower()
        if any(keyword in prompt for keyword in ["revenue", "profit", "finance", "budget", "margin"]):
            return "financial"
        if any(keyword in prompt for keyword in ["sales", "pipeline", "conversion", "customer", "quota"]):
            return "sales"
        if any(keyword in prompt for keyword in ["operations", "sla", "backlog", "throughput", "support"]):
            return "operational"
        if any(keyword in prompt for keyword in ["executive", "board", "leadership", "summary", "overview"]):
            return "executive"
        return "general"

    def _infer_goal(self, prompt: str) -> str:
        lowered = prompt.lower()
        if any(keyword in lowered for keyword in ["sales", "revenue"]):
            return "Track sales performance and revenue trends."
        if any(keyword in lowered for keyword in ["finance", "profit", "budget", "margin"]):
            return "Monitor financial performance and profitability."
        if any(keyword in lowered for keyword in ["operations", "sla", "throughput", "backlog"]):
            return "Monitor operational efficiency and service performance."
        if "executive" in lowered:
            return "Provide a strategic executive summary dashboard."
        return "Summarize the requested business dashboard objective."

    def _infer_audience(self, prompt: str) -> list[str]:
        lowered = prompt.lower()
        audience = ["business analysts"]
        if "executive" in lowered:
            audience.append("executive leadership")
        if "finance" in lowered:
            audience.append("finance managers")
        if "operations" in lowered:
            audience.append("operations managers")
        return self._dedupe_strings(audience)

    def _recommended_pages(self, prompt: str) -> list[str]:
        lowered = prompt.lower()
        if "executive" in lowered:
            return ["Executive Overview", "Trend Analysis", "Detailed Breakdown"]
        if any(keyword in lowered for keyword in ["sales", "revenue", "finance", "operations"]):
            return ["Overview", "Trends", "Breakdown"]
        return ["Overview"]

    def _infer_time_grain(self, prompt: str) -> str:
        if any(keyword in prompt for keyword in ["daily", "day by day", "per day"]):
            return "daily"
        if any(keyword in prompt for keyword in ["weekly", "per week", "week over week"]):
            return "weekly"
        if any(keyword in prompt for keyword in ["monthly", "per month", "month over month"]):
            return "monthly"
        if any(keyword in prompt for keyword in ["quarterly", "quarter"]):
            return "quarterly"
        if any(keyword in prompt for keyword in ["yearly", "annual", "per year"]):
            return "yearly"
        return "unknown"

    def _normalize_data_source(self, item: str | DataSourceSpec) -> DataSourceSpec:
        if isinstance(item, DataSourceSpec):
            return item
        return DataSourceSpec(
            name=item,
            description=self._describe_source(item),
            required_columns=self._required_columns_for_source(item),
        )

    def _infer_visuals(self, prompt: str) -> list[str]:
        return [visual.type for visual in self._infer_visual_specs(prompt, self._infer_metrics(prompt, None), self._infer_dimensions(prompt, None))]

    def _infer_measures(self, prompt: str) -> list[str]:
        return [metric.name for metric in self._infer_metrics(prompt, None)]

    def _extract_text_from_response(self, response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        candidates = [
            getattr(response, "text", None),
            getattr(response, "content", None),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        if isinstance(response, dict):
            for key in ("text", "content", "output_text"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return str(response)

    def _extract_profile_columns(self, data_profile: dict[str, Any] | None) -> list[str]:
        if not data_profile:
            return []
        columns: list[str] = []
        if isinstance(data_profile.get("columns"), list):
            columns.extend(str(column) for column in data_profile["columns"])
        tables = data_profile.get("tables")
        if isinstance(tables, list):
            for table in tables:
                if isinstance(table, dict):
                    table_columns = table.get("columns", [])
                    if isinstance(table_columns, list):
                        columns.extend(str(column) for column in table_columns)
                    columns.extend(str(table.get("name", "")))
        if isinstance(data_profile.get("sample_columns"), list):
            columns.extend(str(column) for column in data_profile["sample_columns"])
        return self._dedupe_strings([column for column in columns if column])

    def _columns_for_table(self, primary_columns: list[str], defaults: list[str]) -> list[str]:
        if primary_columns:
            return self._dedupe_strings(primary_columns[: len(defaults)] or defaults)
        return defaults

    def _match_column(self, data_profile: dict[str, Any] | None, keywords: list[str]) -> str | None:
        columns = self._extract_profile_columns(data_profile)
        for column in columns:
            lowered = column.lower()
            if any(keyword in lowered for keyword in keywords):
                return column
        return None

    def _column_matches(self, columns: list[str], keyword: str) -> bool:
        return any(keyword in column.lower() for column in columns)

    def _entities_from_payload(self, payload: IntentExtractionPayload, user_prompt: str) -> list[str]:
        entities = [source for source in payload.data_sources]
        entities.extend(table.name for table in payload.suggested_tables)
        if "customer" in user_prompt.lower():
            entities.append("Customers")
        if "sales" in user_prompt.lower():
            entities.append("Sales")
        return self._dedupe_strings(entities)

    def _entities_from_strings(
        self,
        data_sources: list[DataSourceSpec],
        tables: list[TableSpec],
    ) -> list[str]:
        entities = [source.name for source in data_sources] + [table.name for table in tables]
        return self._dedupe_strings(entities)

    def _describe_source(self, name: str) -> str:
        descriptions = {
            "sales": "Transactional sales data and performance metrics.",
            "customer": "Customer master data and segmentation attributes.",
            "product": "Product master and categorization data.",
            "finance": "Financial statements, budgets, and profitability data.",
            "operations": "Operational throughput, SLA, and backlog data.",
            "dataset": "Generic dataset inferred from the prompt.",
        }
        return descriptions.get(name.lower(), f"Data source for {name}.")

    def _required_columns_for_source(self, name: str) -> list[str]:
        defaults = {
            "sales": ["Date", "Revenue", "OrderID", "ProductID", "CustomerID"],
            "customer": ["CustomerID", "CustomerName", "Segment"],
            "product": ["ProductID", "ProductName", "Category"],
            "finance": ["Date", "Revenue", "Profit", "Budget"],
            "operations": ["Date", "Backlog", "SLA", "Throughput"],
        }
        return defaults.get(name.lower(), ["Date", "Name", "Value"])

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def _dedupe_by_name(self, values: list[DimensionSpec]) -> list[DimensionSpec]:
        seen: set[str] = set()
        deduped: list[DimensionSpec] = []
        for value in values:
            if value.name not in seen:
                seen.add(value.name)
                deduped.append(value)
        return deduped

    def _dedupe_visuals(self, values: list[VisualSpec]) -> list[VisualSpec]:
        seen: set[tuple[str | None, str | None, str | None]] = set()
        deduped: list[VisualSpec] = []
        for value in values:
            key = (value.type, value.metric, value.dimension)
            if key not in seen:
                seen.add(key)
                deduped.append(value)
        return deduped

    def _dedupe_tables(self, values: list[TableSpec]) -> list[TableSpec]:
        seen: set[str] = set()
        deduped: list[TableSpec] = []
        for value in values:
            if value.name not in seen:
                seen.add(value.name)
                deduped.append(value)
        return deduped

    def _dedupe_relationships(self, values: list[RelationshipSpec]) -> list[RelationshipSpec]:
        seen: set[tuple[str, str]] = set()
        deduped: list[RelationshipSpec] = []
        for value in values:
            key = (value.from_field, value.to_field)
            if key not in seen:
                seen.add(key)
                deduped.append(value)
        return deduped

    def _cache_key(
        self,
        prompt: str,
        data_profile: dict[str, Any] | None,
        prompt_variant: str,
    ) -> str:
        payload = {
            "provider": self.provider,
            "variant": prompt_variant,
            "prompt": prompt.strip().lower(),
            "data_profile": data_profile or {},
        }
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _secret_value(self, secret: Any) -> str | None:
        if secret is None:
            return None
        value = getattr(secret, "get_secret_value", None)
        if callable(value):
            return value()
        return str(secret)
