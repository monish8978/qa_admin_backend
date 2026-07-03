"""Live HTTP smoke test against a running uvicorn instance.

Usage:
    python manual_smoke.py [base_url]
Default base_url: http://127.0.0.1:8005
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8005"
API = f"{BASE}/api/v1"

results: list[tuple[str, str, int, str]] = []  # (name, method+path, status, note)


def record(name: str, method: str, path: str, resp: httpx.Response, expect: set[int], note: str = "") -> bool:
    ok = resp.status_code in expect
    mark = "PASS" if ok else "FAIL"
    snippet = ""
    if not ok:
        snippet = resp.text[:160].replace("\n", " ")
    results.append((mark, f"{method} {path}", resp.status_code, note or snippet))
    return ok


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=20.0)

    # ---------- Public ----------
    record("health", "GET", "/api/v1/health", client.get("/api/v1/health"), {200})
    record("ready", "GET", "/api/v1/health/ready", client.get("/api/v1/health/ready"), {200})
    r = client.get("/api/v1/health/metrics")
    record("metrics", "GET", "/api/v1/health/metrics", r, {200},
           note=f"bytes={len(r.text)} http_total={'http_requests_total' in r.text}")
    record("openapi", "GET", "/api/v1/openapi.json", client.get("/api/v1/openapi.json"), {200})
    record("docs", "GET", "/api/docs", client.get("/api/docs"), {200})

    # ---------- Auth: bad creds ----------
    r = client.post("/api/v1/auth/login", json={"email": "nope@example.com", "password": "wrongpass"})
    record("login bad creds", "POST", "/api/v1/auth/login", r, {401})

    # validation
    r = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    record("login validation", "POST", "/api/v1/auth/login", r, {422})

    # ---------- Auth: signup + login ----------
    # Prefer logging in as the seeded admin against the fully-provisioned
    # `dev-tenant` so per-tenant endpoints (forms, conversations, evaluations,
    # analytics) are exercised against a real provisioned tenant DB.
    seeded_email = "admin@dev.local"
    seeded_password = "DevAdmin123!"
    token: str | None = None
    tenant_slug: str | None = "dev-tenant"

    r = client.post(
        "/api/v1/auth/login",
        json={"email": seeded_email, "password": seeded_password},
        headers={"X-Tenant-Slug": "dev-tenant"},
    )
    record("login seeded", "POST", "/api/v1/auth/login", r, {200, 201})
    if r.status_code in (200, 201):
        d = r.json()
        token = (d.get("data") or {}).get("accessToken") or d.get("accessToken")

    # If seeded login failed, fall back to fresh signup (still validates the flow).
    if not token:
        suffix = uuid.uuid4().hex[:8]
        email = f"smoke-{suffix}@example.com"
        password = "Passw0rd!123ABC"
        slug = f"smoke-{suffix}"
        signup_body = {
            "tenantName": f"Smoke Co {suffix}",
            "tenantSlug": slug,
            "adminEmail": email,
            "adminName": "Smoke Tester",
            "password": password,
            "plan": "BASIC",
        }
        r = client.post("/api/v1/auth/signup", json=signup_body)
        record("signup fallback", "POST", "/api/v1/auth/signup", r, {200, 201}, note=f"slug={slug}")
        if r.status_code in (200, 201):
            d = r.json()
            token = (d.get("data") or {}).get("accessToken") or d.get("accessToken")

    auth_headers: dict[str, str] = {}
    if token:
        auth_headers["Authorization"] = f"Bearer {token}"

    # ---------- Protected: should be 401 without token ----------
    r = client.get("/api/v1/users")
    record("users no-auth", "GET", "/api/v1/users", r, {401})

    # ---------- Webhooks/ingest: missing api key ----------
    r = client.post("/api/v1/webhooks/ingest", json={})
    record("ingest no-key", "POST", "/api/v1/webhooks/ingest", r, {400, 401, 403, 422})

    # ---------- Stripe webhook bad signature ----------
    r = client.post("/api/v1/billing/stripe/webhook", content=b"{}", headers={"stripe-signature": "bad"})
    record("stripe bad sig", "POST", "/api/v1/billing/stripe/webhook", r, {400, 401, 422})

    # ---------- Protected GET endpoints with token (broad sweep) ----------
    protected_gets = [
        "/auth/me",
        "/users",
        "/departments",
        "/forms",
        "/conversations",
        "/evaluations",
        "/evaluations/queue/qa",
        "/evaluations/queue/audit",
        "/evaluations/queue/escalation",
        "/evaluations/queue/verifier",
        "/evaluations/logs/prompt-audit",
        "/analytics/overview",
        "/analytics/agent-performance",
        "/analytics/ai-usage-trends",
        "/analytics/conversation-volume",
        "/analytics/deviation-trends",
        "/analytics/escalation-stats",
        "/analytics/form-score-distribution",
        "/analytics/qa-reviewer-performance",
        "/analytics/question-deviations",
        "/analytics/rejection-reasons",
        "/analytics/score-trends",
        "/analytics/sla-report",
        "/analytics/verifier-overrides",
        "/analytics/verifier-report",
        "/billing/subscription",
        "/billing/usage",
        "/llm-config",
        "/outbound-webhooks",
        "/outbound-webhooks/deliveries",
        "/routing/audits",
        "/routing/mappings",
        "/routing/mappings/stats",
        "/routing/settings",
        "/settings/blind-review",
        "/settings/email",
        "/settings/escalation",
        "/settings/onboarding-status",
    ]
    if token:
        for path in protected_gets:
            try:
                r = client.get(f"/api/v1{path}", headers=auth_headers)
            except httpx.HTTPError as exc:
                results.append(("FAIL", f"GET /api/v1{path}", 0, f"exc={exc}"))
                continue
            # Any non-5xx is acceptable for a smoke probe (404 means route absent; 403 means RBAC etc.)
            ok_set = {200, 201, 202, 204, 400, 401, 403, 404, 422}
            record(f"auth GET {path}", "GET", f"/api/v1{path}", r, ok_set)

    client.close()

    # ---------- Report ----------
    pass_count = sum(1 for r in results if r[0] == "PASS")
    fail_count = sum(1 for r in results if r[0] == "FAIL")
    print(f"\n=== Smoke results: {pass_count} pass / {fail_count} fail ===\n")
    for mark, route, status, note in results:
        print(f"  [{mark}] {status:>3}  {route}  {note}")
    print(f"\nRESULT: pass={pass_count} fail={fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
