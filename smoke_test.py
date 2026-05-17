"""Quick smoke test — run with: python smoke_test.py"""
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GROQ_API_KEY", "dummy-smoke-key")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-smoke-key")

from main import app  # noqa: E402
import agent as agent_module  # noqa: E402
from agent import CriticAgent, PostMortemCoordinator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}  {detail}")
        failed += 1


print("\n=== /health ===")
r = client.get("/health")
h = r.json()
print(" ", h)
check("status 200", r.status_code == 200)
check("status=ok", h.get("status") == "ok")
check("uptime_seconds present", "uptime_seconds" in h)
check("models_in_use present", "models_in_use" in h)
check("investigations_completed present", "investigations_completed" in h)
check("incidents_available >= 5", h.get("incidents_available", 0) >= 5)

print("\n=== /incidents ===")
r = client.get("/incidents")
ids = [i["id"] for i in r.json().get("incidents", [])]
print("  ids:", ids)
check("status 200", r.status_code == 200)
check("incident_a present", "incident_a" in ids)
check("incident_d present", "incident_d" in ids)
check("incident_e present", "incident_e" in ids)

print("\n=== /incidents/random ===")
r = client.get("/incidents/random")
print(" ", r.json())
check("status 200", r.status_code == 200)
check("id field present", "id" in r.json())

print("\n=== /history ===")
r = client.get("/history")
print(" ", r.json())
check("status 200", r.status_code == 200)
check("investigations key present", "investigations" in r.json())

print("\n=== /metrics ===")
r = client.get("/metrics")
print(" ", r.json())
check("status 200", r.status_code == 200)
check("total_investigations present", "total_investigations" in r.json())
check("confidence_distribution present", "confidence_distribution" in r.json())
check("impact analyzed present", "total_impact_analyzed_usd" in r.json())
check("annual review savings present", "estimated_annual_review_savings_usd" in r.json())
dist = r.json().get("confidence_distribution", {})
check("empty high bucket is numeric", dist.get("high_90_plus") == 0)
check("empty medium bucket is numeric", dist.get("medium_80_89") == 0)
check("empty low bucket is numeric", dist.get("low_under_80") == 0)

print("\n=== POST /tools/slack ===")
r = client.post("/tools/slack",
                json={"channel": "#incidents", "incident_id": "incident_a"})
body = r.json()
print(" ", body)
check("status 200", r.status_code == 200)
check("messages returned", len(body.get("messages", [])) >= 2)
check("query_time_ms numeric", isinstance(body.get("query_time_ms"), int))

print("\n=== POST /tools/metrics ===")
r = client.post("/tools/metrics",
                json={"service": "auth", "metric": "latency_p99", "incident_id": "incident_a"})
body = r.json()
print(" ", body)
check("status 200", r.status_code == 200)
check("datapoints returned", len(body.get("datapoints", [])) >= 2)
check("percentiles present", "p95" in body and "p99" in body)

print("\n=== POST /tools/logs ===")
r = client.post("/tools/logs",
                json={"service": "auth", "level": "error", "incident_id": "incident_a"})
body = r.json()
print(" ", body)
check("status 200", r.status_code == 200)
check("logs returned", body.get("total_count", 0) >= 1)

print("\n=== POST /tools/github ===")
r = client.post("/tools/github",
                json={"repo": "api-service", "incident_id": "incident_a"})
body = r.json()
print(" ", body)
check("status 200", r.status_code == 200)
check("commits returned", len(body.get("commits", [])) >= 1)

print("\n=== Critic fallback ===")
original_chat = agent_module._chat_with_fallback
try:
    agent_module._chat_with_fallback = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    critic_result = CriticAgent().run({"root_cause": "x", "confidence": 88}, [], "smoke")
    print(" ", critic_result)
    check("critic fallback agrees", critic_result.get("agrees") is True)
    check("critic fallback marked", critic_result.get("fallback") is True)
finally:
    agent_module._chat_with_fallback = original_chat

print("\n=== Enterprise postmortem fields ===")
coordinator = PostMortemCoordinator("incident_a")
coordinator.conclusion = {
    "description": "Recent auth deploy broke OAuth token validation algorithm",
    "confidence": 87,
    "confirming_evidence": "Auth deploy changed token validation. Logs confirm algorithm mismatch.",
    "data_to_query": ["query_github(api-service)", "query_logs(auth, error)"],
}
coordinator.rejected = [{
    "description": "Database connection pool exhaustion",
    "rejection_evidence": "DB connections stayed below exhaustion threshold.",
    "data_to_query": ["query_metrics(database, active_connections)"],
}]
coordinator.vision_findings = {
    "is_inferred": True,
    "visual_pattern": "Auth errors spike while database panels remain healthy.",
    "affected_panels": ["Auth errors"],
    "affected_services": ["auth"],
}
pm = coordinator._generate_postmortem()
print(" ", {k: pm.get(k) for k in ["business_value", "recurrence_risk", "visual_decision"]})
check("business value present", "business_value" in pm)
check("evidence table present", len(pm.get("evidence_table", [])) >= 2)
check("owner suggestions present", len(pm.get("owner_suggestions", [])) >= 1)
check("recurrence risk present", "score" in pm.get("recurrence_risk", {}))
check("visual decision present", pm.get("visual_decision") is not None)

print("\n=== POST /webhook/pagerduty ===")
payload = {
    "incident": {
        "id": "PT4KHLK",
        "title": "High error rate on payment-service",
        "service": {"name": "payment-service"},
        "created_at": "2026-05-13T12:00:00Z",
        "urgency": "high",
    }
}
r = client.post("/webhook/pagerduty", json=payload)
print(" ", r.json())
check("status 200", r.status_code == 200)
check("status=received", r.json().get("status") == "received")
check("matched_incident_id present", "matched_incident_id" in r.json())

print("\n=== POST /generate-incident (bad severity rejected) ===")
r = client.post("/generate-incident",
                json={"service_name": "x", "incident_type": "latency", "severity": "INVALID"})
check("rejects bad severity with 400", r.status_code == 400)

print("\n=== GET /investigate 404 for unknown incident ===")
r = client.get("/investigate?incident_id=does_not_exist")
check("404 for unknown incident", r.status_code == 404)

print(f"\n{'='*40}")
print(f"  {passed} passed, {failed} failed")
print(f"{'='*40}")
sys.exit(0 if failed == 0 else 1)
