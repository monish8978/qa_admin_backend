"""Form scoring engine. Ports apps/api/src/evaluations/scoring.service.ts.

Computes per-question, per-section, and overall weighted scores given a set
of answer records, the form questions/sections, and a scoring strategy.
Critical-failure detection runs over both sections and questions.
"""
from __future__ import annotations

from typing import Any


def _is_critical(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return item.get("isCritical") is True or item.get("critical") is True


def _scale_value(value: float, mn: float, mx: float, scale: float) -> float:
    if mx <= mn:
        return min(max(value, 0), scale)
    clamped = min(max(value, mn), mx)
    return ((clamped - mn) / (mx - mn)) * scale


def _normalize_answer(value: Any, question: dict[str, Any], scale: float) -> float:
    qtype = question.get("type")
    validation = question.get("validation") or {}
    if qtype == "rating":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.0
        mn = validation.get("min", 0)
        mx = validation.get("max", 5)
        return _scale_value(float(value), float(mn), float(mx), scale)
    if qtype == "boolean":
        if value is True or value == 1 or value == "yes" or value == "true":
            return scale
        return 0.0
    if qtype in ("select", "multiselect"):
        num: float | None = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num = float(value)
        elif isinstance(value, str):
            try:
                num = float(value)
            except ValueError:
                return 0.0
        if num is None:
            return 0.0
        if "max" in validation or "min" in validation:
            return _scale_value(
                num, float(validation.get("min", 0)), float(validation.get("max", scale)), scale
            )
        return num
    return 0.0


def _apply_rounding(value: float, policy: str | None) -> float:
    import math

    if policy == "floor":
        return math.floor(value * 100) / 100
    if policy == "ceil":
        return math.ceil(value * 100) / 100
    return round(value * 100) / 100


def score(
    answers: dict[str, dict[str, Any]],
    questions: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    pass_mark = strategy.get("passMark", 70)
    scale = strategy.get("scale", 100)
    rounding_policy = strategy.get("roundingPolicy")

    questions_by_section: dict[str, list[dict[str, Any]]] = {}
    for q in questions:
        questions_by_section.setdefault(q["sectionId"], []).append(q)

    section_scores: dict[str, float] = {}
    section_breakdown: dict[str, dict[str, Any]] = {}
    question_breakdown: dict[str, dict[str, Any]] = {}
    critical_failures: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_total = 0.0

    for section in sections:
        qs = questions_by_section.get(section["id"], [])
        if not qs:
            continue
        total_q_weight = sum(q.get("weight", 0) for q in qs)
        section_raw = 0.0

        for q in qs:
            ans = answers.get(q["key"])
            if not ans:
                continue
            normalized = _normalize_answer(ans.get("value"), q, scale)
            question_breakdown[q["key"]] = {
                "sectionId": q["sectionId"],
                "type": q.get("type"),
                "weight": q.get("weight", 0),
                "rawValue": ans.get("value"),
                "normalizedScore": _apply_rounding(normalized, rounding_policy),
            }
            section_raw += (normalized / scale) * q.get("weight", 0)

        section_score = (section_raw / total_q_weight) * scale if total_q_weight > 0 else 0.0
        rounded = _apply_rounding(section_score, rounding_policy)

        if _is_critical(section) and rounded < pass_mark:
            critical_failures.append(
                {"level": "section", "id": section["id"], "title": section.get("title", "")}
            )

        for q in qs:
            if not _is_critical(q):
                continue
            ans = answers.get(q["key"])
            normalized = _normalize_answer(ans.get("value") if ans else None, q, scale)
            if normalized < pass_mark:
                critical_failures.append(
                    {
                        "level": "question",
                        "id": q.get("id"),
                        "key": q["key"],
                        "label": q.get("label", ""),
                    }
                )

        section_scores[section["id"]] = rounded
        section_breakdown[section["id"]] = {
            "title": section.get("title", ""),
            "raw": rounded,
            "weight": section.get("weight", 0),
            "weighted": rounded * section.get("weight", 0),
            "questionCount": len(qs),
        }
        total_weight += section.get("weight", 0)
        weighted_total += rounded * section.get("weight", 0)

    overall_raw = weighted_total / total_weight if total_weight > 0 else 0.0
    overall_score = _apply_rounding(overall_raw, rounding_policy)
    critical_failure = len(critical_failures) > 0
    pass_fail = (not critical_failure) and overall_score >= pass_mark

    return {
        "answers": answers,
        "sectionScores": section_scores,
        "overallScore": overall_score,
        "passFail": pass_fail,
        "criticalFailure": critical_failure,
        "computation": {
            "sectionBreakdown": section_breakdown,
            "questionBreakdown": question_breakdown,
            "passMark": pass_mark,
            "scale": scale,
            "criticalFailures": critical_failures,
        },
    }
