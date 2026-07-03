"""In-process API smoke test — exercises every router via FastAPI TestClient.

Verifies:
  - imports + lifespan
  - public endpoints return 200
  - protected endpoints return 401 with our error envelope
  - openapi.json renders and registers all routers
  - login with bad creds returns the proper error code
  - Prometheus /health/metrics yields a text payload
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from fastapi.testclient import TestClient

# Make sure REDIS_ENABLED=false for the smoke test so the queue-metrics task and
# any redis-backed paths don't spam errors.
os.environ.setdefault("REDIS_ENABLED", "false")

# Ensure cwd has the .env loaded properly (TestClient triggers app startup).
HERE = os.path.dirname(__file__)
os.chdir(HERE)

# Add app root to sys.path
sys.path.insert(0, HERE)

from app.main import app  # noqa: E402

PASS, FAIL = 0, 0
results: list[tuple[str, str, int, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def run() -> None:
    with TestClient(app) as client:
        # ---------- Public ----------
        print("\n[public]")
        r = client.get("/api/v1/health")
        check("GET /health", r.status_code == 200 and r.json().get("status") == "ok", f"{r.status_code}")

        r = client.get("/api/v1/health/ready")
        body = r.json()
        check(
            "GET /health/ready",
            r.status_code == 200 and "checks" in body,
            f"{r.status_code} checks={body.get('checks')}",
        )

        r = client.get("/api/v1/health/metrics")
        check(
            "GET /health/metrics",
            r.status_code == 200
            and "http_requests_total" in r.text
            and "queue_jobs_waiting" in r.text,
            f"{r.status_code} bytes={len(r.text)}",
        )

        r = client.get("/api/v1/openapi.json")
        spec = r.json()
        path_count = len(spec.get("paths", {}))
        check(
            "GET /openapi.json",
            r.status_code == 200 and path_count >= 60,
            f"{r.status_code} paths={path_count}",
        )

        # ---------- Auth ----------
        print("\n[auth]")
        r = client.post("/api/v1/auth/login", json={"email": "nope@example.com", "password": "wrong"})
        body = r.json()
        check(
            "POST /auth/login (bad creds)",
            r.status_code in (400, 401, 403)
            and isinstance(body.get("error"), dict)
            and "code" in body["error"],
            f"{r.status_code} code={body.get('error', {}).get('code')}",
        )

        r = client.post("/api/v1/auth/login", json={"email": "x"})  # validation error
        check(
            "POST /auth/login (validation)",
            r.status_code == 422 and body.get("error") is not None,
            f"{r.status_code}",
        )

        r = client.get("/api/v1/auth/me")
        check("GET /auth/me (no token)", r.status_code == 401, f"{r.status_code}")

        # ---------- Protected routes (all should 401 without token) ----------
        print("\n[protected — expect 401]")
        protected = [
            ("GET", "/api/v1/users"),
            ("GET", "/api/v1/departments"),
            ("GET", "/api/v1/forms"),
            ("GET", "/api/v1/conversations"),
            ("GET", "/api/v1/evaluations"),
            ("GET", "/api/v1/evaluations/queue/qa"),
            ("GET", "/api/v1/evaluations/queue/verifier"),
            ("GET", "/api/v1/evaluations/queue/escalation"),
            ("GET", "/api/v1/evaluations/queue/audit"),
            ("GET", "/api/v1/llm-config"),
            ("GET", "/api/v1/billing/subscription"),
            ("GET", "/api/v1/billing/usage"),
            ("GET", "/api/v1/outbound-webhooks"),
            ("GET", "/api/v1/outbound-webhooks/deliveries"),
            ("GET", "/api/v1/routing/mappings"),
            ("GET", "/api/v1/routing/settings"),
            ("GET", "/api/v1/routing/audits"),
            ("GET", "/api/v1/routing/mappings/stats"),
            ("GET", "/api/v1/settings/escalation"),
            ("GET", "/api/v1/settings/blind-review"),
            ("GET", "/api/v1/settings/email"),
            ("GET", "/api/v1/settings/onboarding-status"),
            ("GET", "/api/v1/analytics/overview"),
            ("GET", "/api/v1/analytics/conversation-volume"),
            ("GET", "/api/v1/analytics/score-trends"),
            ("GET", "/api/v1/analytics/agent-performance"),
            ("GET", "/api/v1/analytics/qa-reviewer-performance"),
            ("GET", "/api/v1/analytics/verifier-report"),
            ("GET", "/api/v1/analytics/sla-report"),
            ("GET", "/api/v1/analytics/ai-usage-trends"),
            ("GET", "/api/v1/analytics/deviation-trends"),
            ("GET", "/api/v1/analytics/escalation-stats"),
            ("GET", "/api/v1/analytics/form-score-distribution"),
            ("GET", "/api/v1/analytics/question-deviations"),
            ("GET", "/api/v1/analytics/rejection-reasons"),
            ("GET", "/api/v1/analytics/verifier-overrides"),
            ("GET", "/api/v1/evaluations/logs/prompt-audit"),
        ]
        status_counter: Counter[int] = Counter()
        for method, path in protected:
            r = client.request(method, path)
            status_counter[r.status_code] += 1
            ok = r.status_code == 401
            results.append((method, path, r.status_code, "" if ok else r.text[:160]))
            if not ok:
                print(f"  FAIL  {method} {path}  -> {r.status_code} {r.text[:160]}")
        print(f"  {sum(1 for _,_,s,_ in results if s == 401)}/{len(protected)} returned 401 (status counts: {dict(status_counter)})")
        check("protected endpoints all 401", all(s == 401 for _, _, s, _ in results), "")

        # ---------- Webhook ingest needs X-Api-Key ----------
        print("\n[ingest]")
        r = client.post("/api/v1/webhooks/ingest", json={"conversations": []})
        # FastAPI rejects missing required header with 422 before reaching the handler;
        # if X-Api-Key is supplied but unknown, the handler returns 401.
        check(
            "POST /webhooks/ingest (no api key)",
            r.status_code in (401, 403, 422),
            f"{r.status_code}",
        )

        # ---------- Stripe webhook needs signature ----------
        r = client.post("/api/v1/billing/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"})
        check(
            "POST /billing/stripe/webhook (bad sig)",
            r.status_code in (200, 400, 401, 503),
            f"{r.status_code}",
        )

    print(f"\nRESULT: pass={PASS} fail={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run()
