"""Quick smoke test — run with: python smoke_test.py"""
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GROQ_API_KEY", "dummy-smoke-key")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-smoke-key")

from main import app  # noqa: E402
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
