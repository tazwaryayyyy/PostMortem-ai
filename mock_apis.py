import json
import os
from pathlib import Path

import httpx

INCIDENTS: dict = {}

# Tool API base URL — override via TOOL_API_BASE_URL env var for deployments
_TOOL_BASE = os.getenv("TOOL_API_BASE_URL", "http://localhost:8000")


def load_incidents():
    """Load all incident JSON files from the incidents/ directory."""
    global INCIDENTS
    incidents_dir = Path("incidents")
    if not incidents_dir.exists():
        return
    for path in incidents_dir.glob("*.json"):
        name = path.stem  # filename without extension
        try:
            with open(path, encoding="utf-8") as f:
                INCIDENTS[name] = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # Skip malformed files silently


def _get_incident(incident_id: str) -> dict:
    """Safely fetch incident data, falling back to incident_a."""
    return INCIDENTS.get(incident_id) or INCIDENTS.get("incident_a") or {}


async def query_logs_impl(
    service: str,
    start_time: str,
    end_time: str,
    level: str = "error",
    incident_id: str = "incident_a",
) -> dict:
    """Call POST /tools/logs — returns live-enriched log data via HTTP."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{_TOOL_BASE}/tools/logs",
                json={
                    "service": service,
                    "start_time": start_time,
                    "end_time": end_time,
                    "level": level,
                    "incident_id": incident_id,
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        # Fallback: direct incident data (server not reachable / tests)
        incident = _get_incident(incident_id)
        results = [
            log for log in incident.get("signals", {}).get("logs", [])
            if log.get("service") == service and log.get("level") == level
        ]
        return {"logs": results, "total_count": len(results), "query_time_ms": 0, "source": "fallback"}


async def query_slack_impl(
    channel: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    """Call POST /tools/slack — returns messages with sentiment and escalation metrics."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{_TOOL_BASE}/tools/slack",
                json={"channel": channel, "incident_id": incident_id},
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        incident = _get_incident(incident_id)
        results = [m for m in incident.get("signals", {}).get(
            "slack", []) if m.get("channel") == channel]
        return {"messages": results, "count": len(results), "source": "fallback"}


async def query_pagerduty_impl(
    start_time: str,
    end_time: str,
    service: str = None,
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    results = incident.get("signals", {}).get("pagerduty", [])
    if service:
        results = [r for r in results if r.get("service") == service]
    return {"query": "pagerduty.alerts", "count": len(results), "alerts": results}


async def query_github_impl(
    repo: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    """Call POST /tools/github — returns commits annotated with risk and timing."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{_TOOL_BASE}/tools/github",
                json={
                    "repo": repo,
                    "start_time": start_time,
                    "end_time": end_time,
                    "incident_id": incident_id,
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        incident = _get_incident(incident_id)
        results = [c for c in incident.get("signals", {}).get(
            "github", []) if c.get("repo") == repo]
        return {"commits": results, "risk_commits": [], "deploy_count": len(results), "source": "fallback"}


async def query_metrics_impl(
    service: str,
    metric: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    """Call POST /tools/metrics — returns time-series with noise, percentiles, anomaly flag."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{_TOOL_BASE}/tools/metrics",
                json={
                    "service": service,
                    "metric": metric,
                    "start_time": start_time,
                    "end_time": end_time,
                    "incident_id": incident_id,
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        incident = _get_incident(incident_id)
        metrics_data = incident.get("signals", {}).get(
            "metrics", {}).get(service, {})
        time_series = metrics_data.get(metric, [])
        return {"datapoints": time_series, "count": len(time_series), "source": "fallback"}


async def flag_for_review_impl(
    reason: str,
    current_hypothesis: str,
    confidence: float,
    incident_id: str = "incident_a",
) -> dict:
    return {
        "action": "FLAGGED_FOR_REVIEW",
        "reason": reason,
        "hypothesis": current_hypothesis,
        "confidence": confidence,
        "message": "Agent cannot conclude with sufficient confidence. Human review required.",
    }


# Load on module import
load_incidents()
