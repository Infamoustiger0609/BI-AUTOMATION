import asyncio

import pytest

from app.services.intent_parser import IntentParser


def _run(coro):
    return asyncio.run(coro)


def test_prompt_variant_selection():
    parser = IntentParser()
    prompt = parser.build_prompt("Show revenue and profit by month", prompt_variant="financial")
    assert "financial" in prompt.lower()
    assert "Revenue" in prompt
    assert "Few-shot examples" in prompt


def test_intent_parser_parses_json_response(monkeypatch):
    parser = IntentParser(provider="gemini")
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])

    async def fake_generate(provider_name, prompt_text):
        assert provider_name == "gemini"
        assert "User Request:" in prompt_text
        return """
        {
          "dashboard_title": "Sales Dashboard",
          "summary": "Track monthly sales performance.",
          "time_grain": "monthly",
          "metrics": [
            {"name": "Total Revenue", "type": "sum", "description": "Total revenue", "source_column": "Revenue"}
          ],
          "dimensions": [
            {"name": "Region", "type": "categorical", "values": [], "grain": "none"}
          ],
          "visuals": [
            {"type": "bar_chart", "metric": "Total Revenue", "dimension": "Region", "title": "Revenue by Region"}
          ],
          "filters": [],
          "data_sources": ["sales"],
          "suggested_tables": [
            {"name": "Sales", "columns": ["Date", "Region", "Revenue"]}
          ],
          "suggested_relationships": [],
          "notes": ["parsed"],
          "confidence": 0.91
        }
        """

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    result = _run(parser.parse_intent("Create a sales dashboard by region and month"))

    assert result.dashboard_title == "Sales Dashboard"
    assert result.time_grain == "monthly"
    assert result.metrics[0].name == "Total Revenue"
    assert result.visuals[0].type == "bar_chart"
    assert result.provider == "gemini"


def test_intent_parser_recovers_from_fenced_json(monkeypatch):
    parser = IntentParser(provider="gemini")
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])

    async def fake_generate(provider_name, prompt_text):
        return """
        Here is the JSON:
        ```json
        {
          "dashboard_title": "Executive Overview",
          "summary": "Executive level summary.",
          "time_grain": "monthly",
          "metrics": [],
          "dimensions": [],
          "visuals": [],
          "filters": [],
          "data_sources": [],
          "suggested_tables": [],
          "suggested_relationships": [],
          "notes": [],
          "confidence": 0.75
        }
        ```
        """

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    result = _run(parser.parse_intent("Create an executive dashboard"))
    assert result.dashboard_title == "Executive Overview"
    assert result.confidence == 0.75


def test_intent_parser_recovers_from_fenced_json_with_stray_braces_before_fence(monkeypatch):
    parser = IntentParser(provider="gemini")
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])

    async def fake_generate(provider_name, prompt_text):
        return """
        I will return {"example": true} and then the real payload:
        ```json
        {
          "dashboard_title": "Executive Overview",
          "summary": "Executive level summary.",
          "time_grain": "monthly",
          "metrics": [],
          "dimensions": [],
          "visuals": [],
          "filters": [],
          "data_sources": [],
          "suggested_tables": [],
          "suggested_relationships": [],
          "notes": [],
          "confidence": 0.75
        }
        ```
        """

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    result = _run(parser.parse_intent("Create an executive dashboard"))
    assert result.dashboard_title == "Executive Overview"
    assert result.confidence == 0.75


def test_intent_parser_falls_back_on_invalid_json(monkeypatch):
    parser = IntentParser(provider="gemini")
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])

    async def fake_generate(provider_name, prompt_text):
        return "This is not JSON at all."

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    result = _run(parser.parse_intent("Build a sales dashboard"))
    assert result.metrics
    assert result.visuals
    assert "fallback" in " ".join(result.notes).lower()
    assert result.confidence > 0


def test_intent_parser_uses_cache(monkeypatch):
    parser = IntentParser(provider="gemini")
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])
    calls = {"count": 0}

    async def fake_generate(provider_name, prompt_text):
        calls["count"] += 1
        return """
        {
          "dashboard_title": "Sales Dashboard",
          "summary": "Track sales performance.",
          "time_grain": "monthly",
          "metrics": [],
          "dimensions": [],
          "visuals": [],
          "filters": [],
          "data_sources": [],
          "suggested_tables": [],
          "suggested_relationships": [],
          "notes": [],
          "confidence": 0.8
        }
        """

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    first = _run(parser.parse_intent("Show sales by region"))
    second = _run(parser.parse_intent("Show sales by region"))

    assert first.dashboard_title == second.dashboard_title
    assert calls["count"] == 1
    assert parser.get_metrics()["cache_hits"] == 1


def test_intent_parser_circuit_breaker_falls_back(monkeypatch):
    parser = IntentParser(provider="gemini", max_retries=1)
    parser.settings.llm_circuit_breaker_threshold = 1
    monkeypatch.setattr(parser, "_provider_order", lambda: ["gemini"])
    calls = {"count": 0}

    async def fake_generate(provider_name, prompt_text):
        calls["count"] += 1
        raise RuntimeError("rate limit exceeded")

    monkeypatch.setattr(parser, "_generate_with_provider", fake_generate)

    first = _run(parser.parse_intent("Create a finance dashboard"))
    assert first.provider == "gemini"
    assert first.metrics
    assert parser.get_metrics()["provider_failures"] == 1
    assert parser.circuit_breaker.is_open is True

    # A second, differently-worded prompt (so it isn't served from cache) must
    # short-circuit through the open breaker without calling the provider again.
    second = _run(parser.parse_intent("Build a different finance dashboard"))
    assert second.metrics
    assert calls["count"] == 1
    assert parser.get_metrics()["circuit_open_fallbacks"] == 1


@pytest.mark.parametrize(
    "prompt,variant",
    [
        ("Show revenue and profit", "financial"),
        ("Track pipeline and conversion", "sales"),
        ("Monitor backlog and SLA", "operational"),
        ("Give me a board summary", "executive"),
    ],
)
def test_variant_prompt_contains_domain_context(prompt, variant):
    parser = IntentParser()
    built = parser.build_prompt(prompt, prompt_variant=variant)
    assert variant in built.lower()
    assert "Return ONLY valid JSON" in built

