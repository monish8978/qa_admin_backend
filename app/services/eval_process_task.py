"""Celery task: eval.process — port of apps/api/src/evaluations/eval-process.worker.ts.

Runs the AI scoring pipeline for a single evaluation:
  1. Load tenant DB connection, pull conversation + form + routing metadata.
  2. Honor missing/disabled LLM config by short-circuiting to QA queue.
  3. Build prompt → callLlmWithFailover → validate → score → routeByConfidence.
  4. Persist AI layer, append prompt audit log, upsert workflow queue.
  5. Upsert master usage_metric tokens/cost.
  6. On failure → mark AI_FAILED + outbound webhook + re-raise to retry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..celery_app import celery_app
from ..common.encryption import decrypt
from ..common.enums import WorkflowState
from ..db import SessionLocal
from ..models.master import (
    ConversationRoutingSetting,
    LlmConfig,
    OutboundWebhook,
    Tenant,
    UsageMetric,
)
from ..models.tenant import Conversation, Evaluation, FormDefinition, WorkflowQueue
from .department_routing import (
    get_active_departments,
    resolve_conversation_department,
    select_least_loaded_user,
)
from .llm_cost import estimate_cost_cents
from .scoring_service import score as score_form
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.worker.eval_process")

_LLM_TIMEOUT_SECONDS = 60.0
_WEBHOOK_TIMEOUT_SECONDS = 5.0


def _extract_llm_status_code(err: Exception) -> int | None:
    if not isinstance(err, RuntimeError):
        return None
    match = re.search(r"LLM API error\s+(\d{3})", str(err))
    if not match:
        return None
    return int(match.group(1))


def _is_retryable_eval_error(err: Exception) -> bool:
    if isinstance(
        err,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ),
    ):
        return True

    status_code = _extract_llm_status_code(err)
    if status_code is not None:
        return status_code == 429 or status_code >= 500

    return False


def _classify_eval_error(err: Exception) -> str:
    if isinstance(err, json.JSONDecodeError):
        return "LLM_INVALID_JSON"
    if isinstance(err, ValueError):
        return "LLM_RESPONSE_VALIDATION_FAILED"
    if isinstance(err, httpx.TimeoutException):
        return "LLM_TIMEOUT"
    if isinstance(
        err,
        (
            httpx.NetworkError,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ),
    ):
        return "LLM_NETWORK_ERROR"
    status_code = _extract_llm_status_code(err)
    if status_code is not None:
        if status_code == 429:
            return "LLM_RATE_LIMITED"
        if status_code >= 500:
            return "LLM_PROVIDER_UNAVAILABLE"
        return "LLM_PROVIDER_REJECTED_REQUEST"
    return "AI_PIPELINE_ERROR"


def _hash_for_debug(value: Any) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _prompt_log_path() -> Path:
    configured = (os.environ.get("LLM_PROMPT_LOG_PATH") or "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path.cwd() / "apps" / "api" / "logs" / "llm-prompt-audit.jsonl").resolve()


def _append_prompt_audit_log(entry: dict[str, Any]) -> None:
    try:
        path = _prompt_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as err:  # noqa: BLE001
        log.warning("prompt_log_write_failed: %s", err)


def _resolve_llm_request_url(provider: str, model: str, endpoint: str | None) -> str:
    normalized = endpoint.rstrip("/") if endpoint else None
    if provider == "OPENAI":
        return "https://api.openai.com/v1/chat/completions"
    if provider == "AZURE_OPENAI":
        base = normalized or "https://api.openai.com"
        return f"{base}/openai/deployments/{model}/chat/completions?api-version=2024-02-01"
    if not normalized:
        return "https://api.openai.com/v1/chat/completions"
    
    if "/v1/responses" in normalized.lower():
        normalized = normalized.replace("/v1/responses", "/v1/chat/completions")
    elif normalized.lower().endswith("/responses"):
        normalized = normalized[:-10] + "/chat/completions"

    if normalized.lower().endswith("/chat/completions") or "/chat/completions?" in normalized.lower():
        return normalized
        
    if "/chat/sync" in normalized.lower():
        return normalized
        
    if normalized.lower().endswith("/v1"):
        return f"{normalized}/chat/completions"
        
    return f"{normalized}/v1/chat/completions"


def _call_llm(
    provider: str,
    model: str,
    endpoint: str | None,
    api_key: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    url = _resolve_llm_request_url(provider, model, endpoint)
    headers = {"Content-Type": "application/json"}
    if provider == "AZURE_OPENAI":
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    # Only append HuggingFace custom proxy fields if using the local scalable proxy
    if endpoint and ("172.16.3.215:8000" in endpoint or "/chat/sync" in endpoint):
        body["query"] = prompt
        body["hf_model"] = model
        body["hf_token"] = api_key
    with httpx.Client(timeout=_LLM_TIMEOUT_SECONDS) as client:
        resp = client.post(url, headers=headers, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text}")
    return resp.json()


def _call_llm_with_failover(cfg: LlmConfig, prompt: str) -> dict[str, Any]:
    primary_key = decrypt(cfg.apiKeyEnc)
    try:
        data = _call_llm(
            cfg.provider, cfg.model, cfg.endpoint, primary_key,
            prompt, cfg.maxTokens, cfg.temperature,
        )
        return {"data": data, "provider": cfg.provider, "model": cfg.model, "usedBackup": False}
    except Exception as primary_err:  # noqa: BLE001
        if not (cfg.backupProvider and cfg.backupModel and cfg.backupApiKeyEnc):
            raise
        backup_key = decrypt(cfg.backupApiKeyEnc)
        data = _call_llm(
            cfg.backupProvider, cfg.backupModel, cfg.endpoint, backup_key,
            prompt, cfg.maxTokens, cfg.temperature,
        )
        log.warning("eval.process: primary LLM failed, used backup. err=%s", primary_err)
        return {
            "data": data,
            "provider": cfg.backupProvider,
            "model": cfg.backupModel,
            "usedBackup": True,
        }


def _format_transcript(content: Any) -> str:
    import json
    if isinstance(content, list):
        formatted = []
        for msg in content:
            if isinstance(msg, dict) and "speaker" in msg and "text" in msg:
                speaker = str(msg["speaker"]).capitalize()
                text = str(msg["text"])
                formatted.append(f"{speaker}: {text}")
        if formatted:
            return "\n".join(formatted)
    return json.dumps(content, indent=2, default=str)


def _build_prompt(
    conversation: Conversation,
    questions: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> str:
    section_map = {s["id"]: s.get("title", s["id"]) for s in sections}
    sorted_q = sorted(questions, key=lambda q: q.get("order", 0))
    blocks: list[str] = []
    for q in sorted_q:
        lines = [
            f'[{section_map.get(q["sectionId"], q["sectionId"])}] Q:{q["key"]} '
            f'(type={q.get("type")}, weight={q.get("weight", 1)})',
            f'  Label: {q.get("label", "")}',
        ]
        if q.get("rubric"):
            r = q["rubric"]
            anchors = " | ".join(f'{a["value"]}: {a["label"]}' for a in r.get("anchors", []))
            lines.append(f'  Rubric: {r.get("goal", "")}\n  {anchors}')
        if q.get("options"):
            opts = ", ".join(f'{o["value"]}={o["label"]}' for o in q["options"])
            lines.append(f"  Options: {opts}")
        blocks.append("\n".join(lines))
    question_block = "\n\n".join(blocks)
    return (
        "You are an expert Quality Assurance (QA) evaluation AI for customer support.\n"
        "Evaluate the following customer support conversation against the QA form criteria below.\n"
        "NOTE: The conversation may be in English, Hindi, or a mix of both (Hinglish). Please read and understand the context carefully before scoring.\n\n"
        "For each question, respond with a JSON object where each key is the questionKey and each value is:\n"
        '{ "value": <answer>, "reasoning": <step-by-step thinking in 1-2 sentences>, "confidence": <0..1> }\n\n'
        "IMPORTANT RULES:\n"
        "1. For 'select' or 'multiselect' questions, your <answer> MUST be the numerical value of the chosen option (e.g. 5), NOT the text label.\n"
        "2. Always provide a clear 'reasoning' based on exact quotes or actions from the transcript.\n"
        "3. Only output valid JSON. No markdown wrappers. No explanation outside the JSON.\n\n"
        "=== CONVERSATION ===\n"
        f"{_format_transcript(conversation.content)}\n\n"
        "=== QA FORM QUESTIONS ===\n"
        f"{question_block}\n\n"
        "=== RESPONSE (JSON only) ==="
    )


def _validate_llm_answers(raw: dict[str, Any], questions: list[dict[str, Any]]) -> dict[str, Any]:
    cleaned = {}
    valid_keys = {q["key"] for q in questions}
    
    # Build a map from possible LLM representations to the actual question key
    key_mapping = {}
    for q in questions:
        k = q["key"]
        key_mapping[k.lower()] = k
        key_mapping[f"q{k}".lower()] = k
        key_mapping[f"q:{k}".lower()] = k
        key_mapping[f"q :{k}".lower()] = k
        if k.isdigit():
            key_mapping[k] = k
            key_mapping[f"q{k}"] = k
            key_mapping[f"q:{k}"] = k

    for key, val in raw.items():
        lookup_key = str(key).strip().lower()
        if lookup_key in key_mapping:
            cleaned_key = key_mapping[lookup_key]
        else:
            cleaned_key = key
            if key.startswith("Q:"):
                cleaned_key = key[2:]
            elif key.startswith("Q :"):
                cleaned_key = key[3:]
            elif key.startswith("Q") and key[1:].isdigit():
                cleaned_key = key[1:]
            cleaned_key = cleaned_key.strip()
        cleaned[cleaned_key] = val

    for key, val in cleaned.items():
        if key not in valid_keys:
            raise ValueError(f"LLM output contains unknown question key: {key}")
        if not isinstance(val, dict):
            raise ValueError(f"LLM output for {key} must be an object")
        if "value" not in val:
            raise ValueError(f"LLM output for {key} missing required field: value")
        if "reasoning" in val and val["reasoning"] is not None and not isinstance(val["reasoning"], str):
            raise ValueError(f"LLM output for {key} has invalid reasoning type")
        conf = val.get("confidence")
        if conf is not None and (not isinstance(conf, (int, float)) or conf < 0 or conf > 1):
            raise ValueError(f"LLM output for {key} has invalid confidence value")
    for q in questions:
        if q["key"] not in cleaned:
            raise ValueError(f"LLM output missing question key: {q['key']}")
    return cleaned


def _route_by_confidence(raw_answers: dict[str, Any]) -> dict[str, Any]:
    confidences: list[float] = []
    for v in raw_answers.values():
        c = v.get("confidence") if isinstance(v, dict) else None
        if isinstance(c, (int, float)) and c == c:  # not NaN
            confidences.append(max(0.0, min(1.0, float(c))))
    if not confidences:
        return {"confidenceScore": None, "queuePriority": 5, "routeLabel": "NO_CONFIDENCE"}
    confidence = min(confidences)
    if confidence < 0.6:
        return {
            "confidenceScore": confidence,
            "queuePriority": 1,
            "routeLabel": "LOW_CONFIDENCE_MANDATORY_REVIEW",
        }
    if confidence < 0.9:
        return {"confidenceScore": confidence, "queuePriority": 5, "routeLabel": "NORMAL_CONFIDENCE"}
    return {"confidenceScore": confidence, "queuePriority": 6, "routeLabel": "HIGH_CONFIDENCE"}


def _deliver_failed_webhook(
    tenant_id: str, evaluation_id: str, conversation_id: str
) -> None:
    try:
        with SessionLocal() as master:
            hooks = list(
                master.execute(
                    select(OutboundWebhook).where(
                        (OutboundWebhook.tenantId == tenant_id)
                        & (OutboundWebhook.status == "ACTIVE")
                    )
                ).scalars()
            )
            matching = [h for h in hooks if "evaluation.failed" in (h.events or [])]
            if not matching:
                return
            payload = json.dumps(
                {
                    "event": "evaluation.failed",
                    "tenantId": tenant_id,
                    "evaluationId": evaluation_id,
                    "conversationId": conversation_id,
                    "workflowState": WorkflowState.AI_FAILED.value,
                    "finalScore": None,
                    "passFail": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            for hook in matching:
                try:
                    secret = decrypt(hook.secretEnc)
                    sig = "sha256=" + hmac.new(
                        secret.encode(), payload.encode(), hashlib.sha256
                    ).hexdigest()
                    with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
                        client.post(
                            hook.url,
                            content=payload,
                            headers={
                                "Content-Type": "application/json",
                                "X-QA-Signature": sig,
                                "X-QA-Event": "evaluation.failed",
                                "User-Agent": "QA-Platform/1.0",
                            },
                        )
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def _upsert_usage_metric(
    master, tenant_id: str, period_start: datetime, period_end: datetime,
    tokens: int, cost_cents: int,
) -> None:
    stmt = pg_insert(UsageMetric).values(
        tenantId=tenant_id,
        periodStart=period_start,
        periodEnd=period_end,
        conversationsProcessed=0,
        aiTokensUsed=tokens,
        aiCostCents=cost_cents,
        activeUsers=0,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageMetric.tenantId, UsageMetric.periodStart, UsageMetric.periodEnd],
        set_={
            "aiTokensUsed": UsageMetric.aiTokensUsed + tokens,
            "aiCostCents": UsageMetric.aiCostCents + cost_cents,
        },
    )
    master.execute(stmt)
    master.commit()


def _upsert_queue_via_pool(
    ts, evaluation_id: str, *, queue_type: str, department_id: str | None,
    assigned_to: str | None, priority: int,
) -> None:
    existing = ts.execute(
        select(WorkflowQueue).where(WorkflowQueue.evaluationId == evaluation_id)
    ).scalar_one_or_none()
    if existing:
        existing.queueType = queue_type
        existing.departmentId = department_id
        existing.assignedTo = assigned_to
        existing.priority = priority
    else:
        ts.add(
            WorkflowQueue(
                evaluationId=evaluation_id,
                queueType=queue_type,
                departmentId=department_id,
                assignedTo=assigned_to,
                priority=priority,
            )
        )


@celery_app.task(
    name="eval.process",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    acks_late=True,
)
def eval_process(
    self,
    *,
    tenantId: str,
    conversationId: str,
    evaluationId: str,
    formDefinitionId: str,
    formVersion: int | None = None,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    try:
        with SessionLocal() as master:
            tenant = master.get(Tenant, tenantId)
            if not tenant:
                raise RuntimeError(f"Tenant {tenantId} not found")
            active_departments = get_active_departments(master, tenantId)
            routing = master.execute(
                select(ConversationRoutingSetting).where(
                    ConversationRoutingSetting.tenantId == tenantId
                )
            ).scalar_one_or_none()
            assignment_mode = (
                routing.assignmentMode if routing and routing.assignmentMode else "ROUND_ROBIN"
            )
            llm_config = master.execute(
                select(LlmConfig).where(LlmConfig.tenantId == tenantId)
            ).scalar_one_or_none()

            with pool.session(tenantId) as ts:
                conversation = ts.get(Conversation, conversationId)
                if not conversation:
                    raise RuntimeError(f"Conversation {conversationId} not found")

                # Idempotency guard: atomically claim the evaluation by moving it
                # from a processable state into AI_IN_PROGRESS. Duplicate Celery
                # deliveries (retries / double enqueue) get rowcount 0 and exit
                # early, preventing parallel LLM calls and corrupted state.
                from sqlalchemy import update as _sa_update

                claimed = ts.execute(
                    _sa_update(Evaluation)
                    .where(
                        Evaluation.id == evaluationId,
                        Evaluation.workflowState.in_(
                            [WorkflowState.AI_PENDING.value, WorkflowState.AI_FAILED.value]
                        ),
                    )
                    .values(workflowState=WorkflowState.AI_IN_PROGRESS.value)
                )
                if claimed.rowcount != 1:
                    current = ts.get(Evaluation, evaluationId)
                    ts.commit()
                    return {
                        "skipped": True,
                        "reason": "already_processed",
                        "state": current.workflowState if current else "missing",
                    }
                ts.commit()

                routed_dept: dict[str, Any] | None = None
                if conversation.departmentId:
                    routed_dept = next(
                        (d for d in active_departments if d["id"] == conversation.departmentId),
                        None,
                    )
                else:
                    routed_dept = resolve_conversation_department(
                        active_departments, conversation.channel, conversation.cmetadata
                    )

                # No LLM config or disabled — shortcut to QA queue
                if not llm_config or not llm_config.enabled:
                    qa_assignee = None
                    if (
                        routed_dept
                        and routed_dept.get("autoAssignEnabled")
                        and assignment_mode == "ROUND_ROBIN"
                    ):
                        qa_assignee = select_least_loaded_user(
                            master, ts, tenant_id=tenantId,
                            department_id=routed_dept["id"], queue_type="QA_QUEUE",
                        )
                    ev = ts.get(Evaluation, evaluationId)
                    if not ev:
                        raise RuntimeError(f"Evaluation {evaluationId} not found")
                    now = datetime.now(timezone.utc) if qa_assignee else None
                    ev.departmentId = routed_dept["id"] if routed_dept else None
                    ev.workflowState = (
                        WorkflowState.QA_IN_PROGRESS.value
                        if qa_assignee
                        else WorkflowState.QA_PENDING.value
                    )
                    ev.qaUserId = qa_assignee["id"] if qa_assignee else None
                    ev.qaStartedAt = now
                    _upsert_queue_via_pool(
                        ts, evaluationId,
                        queue_type="QA_QUEUE",
                        department_id=routed_dept["id"] if routed_dept else None,
                        assigned_to=qa_assignee["id"] if qa_assignee else None,
                        priority=5,
                    )
                    conversation.status = "QA_REVIEW"
                    conversation.departmentId = routed_dept["id"] if routed_dept else None
                    ts.commit()
                    return {
                        "skipped": True,
                        "reason": "llm_disabled" if llm_config else "no_llm_config",
                    }

                # Run LLM pipeline
                form = ts.get(FormDefinition, formDefinitionId)
                if not form:
                    raise RuntimeError(f"Form {formDefinitionId} not found")
                ev = ts.get(Evaluation, evaluationId)
                if not ev:
                    raise RuntimeError(f"Evaluation {evaluationId} not found")
                ev.workflowState = WorkflowState.AI_IN_PROGRESS.value
                conversation.status = "EVALUATING"
                ts.commit()

                questions = list(form.questions or [])
                sections = list(form.sections or [])
                prompt = _build_prompt(conversation, questions, sections)
                content_hash = _hash_for_debug(conversation.content)
                prompt_hash = _hash_for_debug(prompt)

                _append_prompt_audit_log({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tenantId": tenantId,
                    "evaluationId": evaluationId,
                    "conversationId": conversationId,
                    "formDefinitionId": formDefinitionId,
                    "provider": llm_config.provider,
                    "model": llm_config.model,
                    "promptHash": prompt_hash,
                    "contentHash": content_hash,
                    "prompt": prompt,
                })

                req_start = time.time()
                try:
                    llm_result = _call_llm_with_failover(llm_config, prompt)
                    data = llm_result["data"]
                    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
                    raw_answers = json.loads(content)
                    validated_answers = _validate_llm_answers(raw_answers, questions)

                    answers_hash = _hash_for_debug(validated_answers)
                    duration_ms = int((time.time() - req_start) * 1000)

                    answers: dict[str, Any] = {
                        k: {
                            "value": v.get("value"),
                            "reasoning": v.get("reasoning"),
                            "confidence": v.get("confidence"),
                        }
                        for k, v in validated_answers.items()
                    }
                    score_result = score_form(
                        answers, questions, sections, dict(form.scoringStrategy or {})
                    )

                    ai_layer = {
                        "answers": score_result["answers"],
                        "sectionScores": score_result["sectionScores"],
                        "overallScore": score_result["overallScore"],
                        "passFail": score_result["passFail"],
                    }

                    usage = data.get("usage") or {}
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    completion_tokens = int(usage.get("completion_tokens") or 0)
                    cost_cents = estimate_cost_cents(
                        llm_config.provider, llm_config.model, prompt_tokens, completion_tokens
                    )

                    confidence_routing = _route_by_confidence(raw_answers)

                    qa_assignee = None
                    if (
                        routed_dept
                        and routed_dept.get("autoAssignEnabled")
                        and assignment_mode == "ROUND_ROBIN"
                    ):
                        qa_assignee = select_least_loaded_user(
                            master, ts, tenant_id=tenantId,
                            department_id=routed_dept["id"], queue_type="QA_QUEUE",
                        )
                    qa_start_time = datetime.now(timezone.utc) if qa_assignee else None

                    ai_metadata = {
                        "provider": llm_result["provider"],
                        "model": llm_result["model"],
                        "promptTokens": prompt_tokens,
                        "completionTokens": completion_tokens,
                        "costCents": cost_cents,
                        "durationMs": duration_ms,
                        "usedBackupProvider": llm_result["usedBackup"],
                        "confidenceRoute": confidence_routing["routeLabel"],
                        "promptHash": prompt_hash,
                        "contentHash": content_hash,
                        "answersHash": answers_hash,
                        "questionCount": len(questions),
                        "questionBreakdown": score_result["computation"].get("questionBreakdown"),
                    }

                    _append_prompt_audit_log({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "tenantId": tenantId,
                        "evaluationId": evaluationId,
                        "conversationId": conversationId,
                        "formDefinitionId": formDefinitionId,
                        "provider": llm_result["provider"],
                        "model": llm_result["model"],
                        "promptHash": prompt_hash,
                        "contentHash": content_hash,
                        "prompt": prompt,
                        "responseContent": content,
                        "answersHash": answers_hash,
                        "aiScore": score_result["overallScore"],
                    })

                    ev.departmentId = routed_dept["id"] if routed_dept else None
                    ev.workflowState = (
                        WorkflowState.QA_IN_PROGRESS.value
                        if qa_assignee
                        else WorkflowState.QA_PENDING.value
                    )
                    ev.aiResponseData = ai_layer
                    ev.aiScore = score_result["overallScore"]
                    ev.aiMetadata = ai_metadata
                    ev.confidenceScore = confidence_routing["confidenceScore"]
                    ev.aiCompletedAt = datetime.now(timezone.utc)
                    ev.qaUserId = qa_assignee["id"] if qa_assignee else None
                    ev.qaStartedAt = qa_start_time

                    _upsert_queue_via_pool(
                        ts, evaluationId,
                        queue_type="QA_QUEUE",
                        department_id=routed_dept["id"] if routed_dept else None,
                        assigned_to=qa_assignee["id"] if qa_assignee else None,
                        priority=confidence_routing["queuePriority"],
                    )
                    conversation.status = "QA_REVIEW"
                    conversation.departmentId = routed_dept["id"] if routed_dept else None
                    ts.commit()

                    total_tokens = prompt_tokens + completion_tokens
                    if total_tokens > 0:
                        # Use the shared period helper so the UsageMetric unique
                        # key matches the meter / billing / auth writers.
                        from .usage_meter_service import current_period

                        period_start, period_end = current_period()
                        try:
                            _upsert_usage_metric(
                                master, tenantId, period_start, period_end,
                                total_tokens, cost_cents,
                            )
                        except Exception:  # noqa: BLE001
                            log.warning("usage metric upsert failed", exc_info=True)

                    return {
                        "aiScore": score_result["overallScore"],
                        "durationMs": duration_ms,
                    }

                except Exception as err:  # noqa: BLE001
                    log.exception("eval.process: error processing %s", evaluationId)
                    retryable = _is_retryable_eval_error(err)
                    will_retry = retryable and self.request.retries < self.max_retries
                    err_code = _classify_eval_error(err)
                    try:
                        ev2 = ts.get(Evaluation, evaluationId)
                        if ev2:
                            ev2.workflowState = (
                                WorkflowState.AI_IN_PROGRESS.value
                                if will_retry
                                else WorkflowState.AI_FAILED.value
                            )
                            ev2.aiMetadata = {
                                # Store a stable error code only — never the raw
                                # exception text, which may embed LLM response
                                # bodies / API keys / customer PII.
                                "errorCode": err_code,
                                "errorType": type(err).__name__,
                                "retryable": retryable,
                                "willRetry": will_retry,
                                "retryAttempt": self.request.retries + 1,
                                "maxRetries": self.max_retries,
                            }
                        conv2 = ts.get(Conversation, conversationId)
                        if conv2:
                            conv2.status = "EVALUATING" if will_retry else "FAILED"
                        ts.commit()
                    except Exception:  # noqa: BLE001
                        ts.rollback()
                    if will_retry:
                        raise self.retry(exc=err)
                    _deliver_failed_webhook(tenantId, evaluationId, conversationId)
                    raise
    except Exception:
        raise

