"""LLM cost estimation. Ports apps/api/src/evaluations/llm-cost.util.ts.

Returns estimated cost in US cents (integer, rounded up). Unknown models or
custom/self-hosted providers return 0.
"""
from __future__ import annotations

import math

_PRICING_TABLE: list[tuple[str, dict[str, int]]] = [
    ("gpt-4o-mini", {"inputCentsPerM": 15, "outputCentsPerM": 60}),
    ("gpt-4o", {"inputCentsPerM": 500, "outputCentsPerM": 1500}),
    ("gpt-4-turbo", {"inputCentsPerM": 1000, "outputCentsPerM": 3000}),
    ("gpt-4-32k", {"inputCentsPerM": 6000, "outputCentsPerM": 12000}),
    ("gpt-4", {"inputCentsPerM": 3000, "outputCentsPerM": 6000}),
    ("gpt-3.5-turbo", {"inputCentsPerM": 50, "outputCentsPerM": 150}),
    ("claude-3-5-sonnet", {"inputCentsPerM": 300, "outputCentsPerM": 1500}),
    ("claude-3-5-haiku", {"inputCentsPerM": 80, "outputCentsPerM": 400}),
    ("claude-3-opus", {"inputCentsPerM": 1500, "outputCentsPerM": 7500}),
    ("claude-3-sonnet", {"inputCentsPerM": 300, "outputCentsPerM": 1500}),
    ("claude-3-haiku", {"inputCentsPerM": 25, "outputCentsPerM": 125}),
]


def estimate_cost_cents(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> int:
    if not model or (prompt_tokens == 0 and completion_tokens == 0):
        return 0
    lower = model.lower()
    pricing = next((p for prefix, p in _PRICING_TABLE if lower.startswith(prefix)), None)
    if not pricing:
        return 0
    input_cost = (prompt_tokens / 1_000_000) * pricing["inputCentsPerM"]
    output_cost = (completion_tokens / 1_000_000) * pricing["outputCentsPerM"]
    return math.ceil(input_cost + output_cost)
