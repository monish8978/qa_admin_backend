"""Analytics service — mirrors apps/api/src/analytics/analytics.service.ts.

Most queries run against the tenant DB; a few read UsageMetric from master.
Results are cached in Redis for 60 s when Redis is available.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models.master import UsageMetric
from ..models.tenant import DeviationRecord, Evaluation, WorkflowQueue
from ..redis_client import get_redis
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.analytics")
_CACHE_TTL = 60

_CRIT_FAIL_NOT_CLAUSE = """
NOT (
    COALESCE((e."finalResponseData"->>'criticalFailure')::boolean, false)
    OR COALESCE((e."verifierFinalData"->>'criticalFailure')::boolean, false)
    OR COALESCE((e."qaAdjustedData"->>'criticalFailure')::boolean, false)
    OR COALESCE((e."aiResponseData"->>'criticalFailure')::boolean, false)
)
"""


def _cache_key(tenant_id: str, metric: str, from_: datetime, to: datetime, role: str | None = None, user_id: str | None = None) -> str:
    parts = ["analytics", tenant_id, metric, from_.isoformat(), to.isoformat()]
    if role:
        parts.append(role)
    if user_id:
        parts.append(user_id)
    return ":".join(parts)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(o)!r}")


def _cached(tenant_id: str, metric: str, from_: datetime, to: datetime, producer, role: str | None = None, user_id: str | None = None):
    r = get_redis()
    key = _cache_key(tenant_id, metric, from_, to, role, user_id)
    if r is not None:
        try:
            blob = r.get(key)
            if blob:
                return json.loads(blob)
        except Exception:  # noqa: BLE001
            pass
    value = producer()
    if r is not None:
        try:
            r.set(key, json.dumps(value, default=_json_default), ex=_CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
    return value


def _round1(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v) * 10) / 10


def _round1_rate(num: float | int, denom: float | int) -> float:
    if denom == 0:
        return 0.0
    return round((num / denom) * 1000) / 10


# ─── public API ───────────────────────────────────────────────────────────────

def overview(tenant_id: str, from_: datetime, to: datetime, role: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            conv_filter = ""
            eval_filter = ""
            queue_filter = ""
            params = {"a": from_, "b": to}
            if role == "QA" and user_id:
                conv_filter = ' AND id IN (SELECT "conversationId" FROM evaluations WHERE "qaUserId" = :uid)'
                eval_filter = ' AND "qaUserId" = :uid'
                queue_filter = ' AND "evaluationId" IN (SELECT id FROM evaluations WHERE "qaUserId" = :uid)'
                params["uid"] = user_id
            elif role == "VERIFIER" and user_id:
                conv_filter = ' AND id IN (SELECT "conversationId" FROM evaluations WHERE "verifierUserId" = :uid)'
                eval_filter = ' AND "verifierUserId" = :uid'
                queue_filter = ' AND "evaluationId" IN (SELECT id FROM evaluations WHERE "verifierUserId" = :uid)'
                params["uid"] = user_id

            total_conv = ts.execute(
                text(
                    f'SELECT COUNT(*) FROM conversations WHERE "receivedAt" >= :a AND "receivedAt" <= :b{conv_filter}'
                ),
                params,
            ).scalar_one()
            completed = ts.execute(
                text(
                    f'SELECT COUNT(*) FROM evaluations '
                    f'WHERE "workflowState" = \'LOCKED\' AND "updatedAt" >= :a AND "updatedAt" <= :b{eval_filter}'
                ),
                params,
            ).scalar_one()
            pending_qa = ts.execute(
                text(f'SELECT COUNT(*) FROM workflow_queues WHERE "queueType" = \'QA_QUEUE\'{queue_filter}'),
                params if (role and user_id) else None,
            ).scalar_one()
            pending_ver = ts.execute(
                text(
                    f'SELECT COUNT(*) FROM workflow_queues WHERE "queueType" = \'VERIFIER_QUEUE\'{queue_filter}'
                ),
                params if (role and user_id) else None,
            ).scalar_one()
            score_row = ts.execute(
                text(
                    f'SELECT AVG("finalScore") AS avg_final FROM evaluations '
                    f'WHERE "workflowState" = \'LOCKED\' AND "lockedAt" >= :a AND "lockedAt" <= :b{eval_filter}'
                ),
                params,
            ).mappings().first()
            avg_final = score_row["avg_final"] if score_row else None

            pass_count = ts.execute(
                text(
                    f"""
                    SELECT COUNT(*) FROM evaluations e
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                      {eval_filter.replace('"qaUserId"', 'e."qaUserId"').replace('"verifierUserId"', 'e."verifierUserId"')}
                      AND e."passFail" = true
                      AND {_CRIT_FAIL_NOT_CLAUSE}
                    """
                ),
                params,
            ).scalar_one()

            dev_filter = ""
            if role == "QA" and user_id:
                dev_filter = ' AND "evaluationId" IN (SELECT id FROM evaluations WHERE "qaUserId" = :uid)'
            elif role == "VERIFIER" and user_id:
                dev_filter = ' AND "evaluationId" IN (SELECT id FROM evaluations WHERE "verifierUserId" = :uid)'

            dev_row = ts.execute(
                text(
                    f'SELECT AVG(deviation) AS d FROM deviation_records '
                    f'WHERE "createdAt" >= :a AND "createdAt" <= :b{dev_filter}'
                ),
                params,
            ).mappings().first()
            avg_dev = dev_row["d"] if dev_row else None

        pass_rate = (pass_count / completed * 100) if completed else 0
        return {
            "totalConversations": int(total_conv),
            "completedEvaluations": int(completed),
            "pendingQA": int(pending_qa),
            "pendingVerifier": int(pending_ver),
            "avgFinalScore": float(avg_final) if avg_final is not None else None,
            "passRate": round(pass_rate * 10) / 10,
            "avgAiQaDeviation": float(avg_dev) if avg_dev is not None else None,
        }

    return _cached(tenant_id, "overview", from_, to, _produce, role, user_id)



def agent_performance(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = ts.execute(
                text(
                    f"""
                    SELECT c."agentId", c."agentName",
                           COUNT(e.id) AS count,
                           AVG(e."finalScore") AS avg_score,
                           SUM(CASE WHEN e."passFail" = true AND {_CRIT_FAIL_NOT_CLAUSE}
                                    THEN 1 ELSE 0 END) AS pass_count
                    FROM conversations c
                    JOIN evaluations e ON e."conversationId" = c.id
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                    GROUP BY c."agentId", c."agentName"
                    ORDER BY avg_score DESC NULLS LAST
                    LIMIT 50
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        return [
            {
                "agentId": r["agentId"],
                "agentName": r["agentName"],
                "totalEvaluations": int(r["count"]),
                "avgScore": _round1(r["avg_score"]),
                "passRate": _round1_rate(int(r["pass_count"]), int(r["count"])),
            }
            for r in rows
        ]

    return _cached(tenant_id, "agent_performance", from_, to, _produce)


def deviation_trends(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = list(
                ts.execute(
                    select(DeviationRecord)
                    .where(
                        (DeviationRecord.createdAt >= from_)
                        & (DeviationRecord.createdAt <= to)
                    )
                    .order_by(DeviationRecord.createdAt.asc())
                ).scalars()
            )
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            day = r.createdAt.date().isoformat()
            if day not in by_day:
                by_day[day] = {"date": day, "AI_VS_QA": [], "QA_VS_VERIFIER": []}
            if r.type in by_day[day]:
                by_day[day][r.type].append(r.deviation)
        return [
            {
                "date": d["date"],
                "avgAiQaDeviation": (sum(d["AI_VS_QA"]) / len(d["AI_VS_QA"]))
                if d["AI_VS_QA"] else None,
                "avgQaVerifierDeviation": (
                    sum(d["QA_VS_VERIFIER"]) / len(d["QA_VS_VERIFIER"])
                )
                if d["QA_VS_VERIFIER"] else None,
            }
            for d in by_day.values()
        ]

    return _cached(tenant_id, "deviation_trends", from_, to, _produce)


def question_deviations(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            records = list(
                ts.execute(
                    select(DeviationRecord).where(
                        (DeviationRecord.type == "AI_VS_QA")
                        & (DeviationRecord.questionKey.is_not(None))
                        & (DeviationRecord.createdAt >= from_)
                        & (DeviationRecord.createdAt <= to)
                    )
                ).scalars()
            )
            total = ts.execute(
                select(func.count()).select_from(Evaluation).where(
                    Evaluation.workflowState.in_(
                        [
                            "QA_COMPLETED",
                            "VERIFIER_PENDING",
                            "VERIFIER_IN_PROGRESS",
                            "LOCKED",
                            "ESCALATED",
                        ]
                    ),
                    Evaluation.qaCompletedAt >= from_,
                    Evaluation.qaCompletedAt <= to,
                )
            ).scalar_one()

        counts: dict[str, dict[str, Any]] = {}
        for r in records:
            k = r.questionKey
            if k not in counts:
                counts[k] = {"questionKey": k, "sectionId": r.sectionId, "count": 0}
            counts[k]["count"] += 1
        ranked = sorted(counts.values(), key=lambda c: c["count"], reverse=True)[:20]
        return [
            {
                "questionKey": r["questionKey"],
                "sectionId": r["sectionId"],
                "overrideCount": r["count"],
                "overrideRate": _round1_rate(r["count"], total) if total else 0,
            }
            for r in ranked
        ]

    return _cached(tenant_id, "question_deviations", from_, to, _produce)


def escalation_stats(tenant_id: str, from_: datetime, to: datetime, role: str | None = None, user_id: str | None = None) -> dict[str, int]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            eq = select(func.count()).select_from(Evaluation).where(
                Evaluation.isEscalated.is_(True),
                Evaluation.qaCompletedAt >= from_,
                Evaluation.qaCompletedAt <= to,
            )
            wq = select(func.count()).select_from(WorkflowQueue).where(
                WorkflowQueue.queueType == "ESCALATION_QUEUE"
            )
            if role == "QA" and user_id:
                eq = eq.where(Evaluation.qaUserId == user_id)
                wq = wq.join(Evaluation, WorkflowQueue.evaluationId == Evaluation.id).where(Evaluation.qaUserId == user_id)
            elif role == "VERIFIER" and user_id:
                eq = eq.where(Evaluation.verifierUserId == user_id)
                wq = wq.join(Evaluation, WorkflowQueue.evaluationId == Evaluation.id).where(Evaluation.verifierUserId == user_id)

            escalated = ts.execute(eq).scalar_one()
            pending = ts.execute(wq).scalar_one()
        return {"escalated": int(escalated), "pendingEscalation": int(pending)}

    return _cached(tenant_id, "escalation_stats", from_, to, _produce, role, user_id)



def verifier_overrides(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            records = list(
                ts.execute(
                    select(DeviationRecord).where(
                        (DeviationRecord.type == "QA_VS_VERIFIER")
                        & (DeviationRecord.questionKey.is_not(None))
                        & (DeviationRecord.createdAt >= from_)
                        & (DeviationRecord.createdAt <= to)
                    )
                ).scalars()
            )
            total = ts.execute(
                select(func.count()).select_from(Evaluation).where(
                    Evaluation.workflowState == "LOCKED",
                    Evaluation.verifierCompletedAt >= from_,
                    Evaluation.verifierCompletedAt <= to,
                )
            ).scalar_one()
        counts: dict[str, dict[str, Any]] = {}
        for r in records:
            k = r.questionKey
            if k not in counts:
                counts[k] = {"questionKey": k, "sectionId": r.sectionId, "count": 0}
            counts[k]["count"] += 1
        ranked = sorted(counts.values(), key=lambda c: c["count"], reverse=True)[:20]
        return [
            {
                "questionKey": r["questionKey"],
                "sectionId": r["sectionId"],
                "overrideCount": r["count"],
                "overrideRate": _round1_rate(r["count"], total) if total else 0,
            }
            for r in ranked
        ]

    return _cached(tenant_id, "verifier_overrides", from_, to, _produce)


def rejection_reasons(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = ts.execute(
                text(
                    'SELECT metadata FROM audit_logs '
                    'WHERE action = \'verifier_reject\' '
                    '  AND "createdAt" >= :a AND "createdAt" <= :b'
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        counts: dict[str, int] = {}
        for row in rows:
            meta = row["metadata"] or {}
            reason = (meta.get("reason") if isinstance(meta, dict) else None) or "Unspecified"
            counts[reason] = counts.get(reason, 0) + 1
        total = sum(counts.values())
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
        return [
            {"reason": reason, "count": cnt, "rate": _round1_rate(cnt, total) if total else 0}
            for reason, cnt in ranked
        ]

    return _cached(tenant_id, "rejection_reasons", from_, to, _produce)


def score_trends(tenant_id: str, from_: datetime, to: datetime) -> dict[str, list[dict[str, Any]]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            by_day = ts.execute(
                text(
                    f"""
                    SELECT DATE(e."lockedAt") AS date,
                           AVG(e."finalScore") AS avg_score,
                           COUNT(*) AS count,
                           SUM(CASE WHEN e."passFail" = true AND {_CRIT_FAIL_NOT_CLAUSE}
                                    THEN 1 ELSE 0 END) AS pass_count
                    FROM evaluations e
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                    GROUP BY DATE(e."lockedAt")
                    ORDER BY date ASC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
            by_channel = ts.execute(
                text(
                    f"""
                    SELECT c."channel",
                           AVG(e."finalScore") AS avg_score,
                           COUNT(*) AS count,
                           SUM(CASE WHEN e."passFail" = true AND {_CRIT_FAIL_NOT_CLAUSE}
                                    THEN 1 ELSE 0 END) AS pass_count
                    FROM evaluations e
                    JOIN conversations c ON c.id = e."conversationId"
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                    GROUP BY c."channel"
                    ORDER BY count DESC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        return {
            "byDay": [
                {
                    "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                    "avgScore": _round1(r["avg_score"]),
                    "count": int(r["count"]),
                    "passRate": _round1_rate(int(r["pass_count"]), int(r["count"])),
                }
                for r in by_day
            ],
            "byChannel": [
                {
                    "channel": r["channel"],
                    "avgScore": _round1(r["avg_score"]),
                    "count": int(r["count"]),
                    "passRate": _round1_rate(int(r["pass_count"]), int(r["count"])),
                }
                for r in by_channel
            ],
        }

    return _cached(tenant_id, "score_trends", from_, to, _produce)


def ai_usage_trends(
    master: Session, tenant_id: str, from_: datetime, to: datetime
) -> list[dict[str, Any]]:
    def _produce():
        rows = list(
            master.execute(
                select(UsageMetric)
                .where(
                    (UsageMetric.tenantId == tenant_id)
                    & (UsageMetric.periodStart >= from_)
                    & (UsageMetric.periodEnd <= to)
                )
                .order_by(UsageMetric.periodStart.asc())
            ).scalars()
        )
        return [
            {
                "period": m.periodStart.strftime("%Y-%m"),
                "periodStart": m.periodStart.isoformat(),
                "periodEnd": m.periodEnd.isoformat(),
                "conversationsProcessed": m.conversationsProcessed,
                "aiTokensUsed": int(m.aiTokensUsed),
                "aiCostCents": m.aiCostCents,
                "aiCostDollars": m.aiCostCents / 100,
                "activeUsers": m.activeUsers,
            }
            for m in rows
        ]

    return _cached(tenant_id, "ai_usage_trends", from_, to, _produce)


def qa_reviewer_performance(
    tenant_id: str, from_: datetime, to: datetime
) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = ts.execute(
                text(
                    """
                    SELECT e."qaUserId",
                           COUNT(*) AS count,
                           AVG(e."qaScore") AS avg_qa,
                           AVG(EXTRACT(EPOCH FROM (e."qaCompletedAt" - e."qaStartedAt")) * 1000)
                               AS avg_turn_ms
                    FROM evaluations e
                    WHERE e."qaUserId" IS NOT NULL
                      AND e."qaCompletedAt" >= :a AND e."qaCompletedAt" <= :b
                    GROUP BY e."qaUserId"
                    ORDER BY count DESC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        return [
            {
                "qaUserId": r["qaUserId"],
                "totalReviewed": int(r["count"]),
                "avgQaScore": _round1(r["avg_qa"]),
                "avgTurnaroundMinutes": (
                    round(float(r["avg_turn_ms"]) / 60000) if r["avg_turn_ms"] else None
                ),
            }
            for r in rows
        ]

    return _cached(tenant_id, "qa_reviewer_performance", from_, to, _produce)


def verifier_report(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            verified_rows = ts.execute(
                text(
                    """
                    SELECT e."verifierUserId" AS uid,
                           COUNT(*) AS verified,
                           AVG(e."verifierScore") AS avg_score
                    FROM evaluations e
                    WHERE e."verifierUserId" IS NOT NULL
                      AND e."verifierCompletedAt" >= :a AND e."verifierCompletedAt" <= :b
                    GROUP BY e."verifierUserId"
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
            # A rejection clears the evaluation's verifierUserId and never sets
            # verifierCompletedAt, so rejected evaluations cannot be attributed
            # from the evaluations table. Count them from the durable audit log,
            # where actorId is the verifier who performed the rejection.
            rejected_rows = ts.execute(
                text(
                    """
                    SELECT "actorId" AS uid, COUNT(*) AS rejected
                    FROM audit_logs
                    WHERE action = 'verifier_reject'
                      AND "createdAt" >= :a AND "createdAt" <= :b
                    GROUP BY "actorId"
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()

        verified_map = {r["uid"]: r for r in verified_rows}
        rejected_map = {r["uid"]: int(r["rejected"]) for r in rejected_rows}

        report: list[dict[str, Any]] = []
        for uid in set(verified_map) | set(rejected_map):
            vr = verified_map.get(uid)
            verified = int(vr["verified"]) if vr else 0
            rejected = rejected_map.get(uid, 0)
            total_reviewed = verified + rejected
            report.append(
                {
                    "verifierUserId": uid,
                    "totalVerified": verified,
                    "totalRejected": rejected,
                    "rejectRate": _round1_rate(rejected, total_reviewed)
                    if total_reviewed
                    else 0,
                    "avgVerifierScore": _round1(vr["avg_score"]) if vr else None,
                }
            )
        report.sort(key=lambda r: r["totalVerified"], reverse=True)
        return report

    return _cached(tenant_id, "verifier_report", from_, to, _produce)


def conversation_volume(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            conv_rows = ts.execute(
                text(
                    """
                    SELECT DATE(c."receivedAt") AS date, COUNT(*) AS count
                    FROM conversations c
                    WHERE c."receivedAt" >= :a AND c."receivedAt" <= :b
                    GROUP BY DATE(c."receivedAt") ORDER BY date ASC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
            eval_rows = ts.execute(
                text(
                    """
                    SELECT DATE(e."createdAt") AS date, COUNT(*) AS count
                    FROM evaluations e
                    WHERE e."createdAt" >= :a AND e."createdAt" <= :b
                    GROUP BY DATE(e."createdAt") ORDER BY date ASC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        merged: dict[str, dict[str, Any]] = {}
        for r in conv_rows:
            d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
            merged[d] = {"date": d, "conversations": int(r["count"]), "evaluations": 0}
        for r in eval_rows:
            d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
            merged.setdefault(d, {"date": d, "conversations": 0, "evaluations": 0})
            merged[d]["evaluations"] = int(r["count"])
        return sorted(merged.values(), key=lambda x: x["date"])

    return _cached(tenant_id, "conversation_volume", from_, to, _produce)


def sla_report(tenant_id: str, from_: datetime, to: datetime) -> dict[str, Any]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = ts.execute(
                text(
                    """
                    SELECT DATE(e."lockedAt") AS date,
                           AVG(EXTRACT(EPOCH FROM (e."lockedAt" - c."receivedAt")) / 3600)
                               AS avg_h,
                           MIN(EXTRACT(EPOCH FROM (e."lockedAt" - c."receivedAt")) / 3600)
                               AS min_h,
                           MAX(EXTRACT(EPOCH FROM (e."lockedAt" - c."receivedAt")) / 3600)
                               AS max_h,
                           COUNT(*) AS count
                    FROM evaluations e
                    JOIN conversations c ON c.id = e."conversationId"
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                    GROUP BY DATE(e."lockedAt") ORDER BY date ASC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        total_count = sum(int(r["count"]) for r in rows if r["avg_h"] is not None)
        overall_avg = (
            sum((float(r["avg_h"]) * int(r["count"])) for r in rows if r["avg_h"] is not None)
            / total_count
            if total_count
            else None
        )
        return {
            "summary": {
                "avgTurnaroundHours": _round1(overall_avg),
                "totalCompleted": total_count,
            },
            "byDay": [
                {
                    "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                    "avgTurnaroundHours": _round1(r["avg_h"]),
                    "minTurnaroundHours": _round1(r["min_h"]),
                    "maxTurnaroundHours": _round1(r["max_h"]),
                    "count": int(r["count"]),
                }
                for r in rows
            ],
        }

    return _cached(tenant_id, "sla_report", from_, to, _produce)


def form_score_distribution(tenant_id: str, from_: datetime, to: datetime) -> list[dict[str, Any]]:
    def _produce():
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            rows = ts.execute(
                text(
                    """
                    SELECT fd."formKey", fd."name" AS form_name,
                           FLOOR(e."finalScore" / 10) * 10 AS bucket,
                           COUNT(*) AS count
                    FROM evaluations e
                    JOIN form_definitions fd ON fd.id = e."formDefinitionId"
                    WHERE e."workflowState" = 'LOCKED'
                      AND e."finalScore" IS NOT NULL
                      AND e."lockedAt" >= :a AND e."lockedAt" <= :b
                    GROUP BY fd."formKey", fd."name", bucket
                    ORDER BY fd."formKey", bucket ASC
                    """
                ),
                {"a": from_, "b": to},
            ).mappings().all()
        by_form: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = r["formKey"]
            if key not in by_form:
                by_form[key] = {
                    "formKey": key,
                    "formName": r["form_name"],
                    "buckets": [],
                }
            min_v = min(int(r["bucket"]), 90)
            by_form[key]["buckets"].append(
                {
                    "label": f"{min_v}\u2013{min_v + 10}%",
                    "min": min_v,
                    "max": min_v + 10,
                    "count": int(r["count"]),
                }
            )
        return list(by_form.values())

    return _cached(tenant_id, "form_score_distribution", from_, to, _produce)
