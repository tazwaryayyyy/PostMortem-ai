import json
from pathlib import Path

INCIDENTS: dict = {}


def load_incidents():
    global INCIDENTS
    for name in ["incident_a", "incident_b", "incident_c"]:
        path = Path(f"incidents/{name}.json")
        if path.exists():
            with open(path) as f:
                INCIDENTS[name] = json.load(f)


def _get_incident(incident_id: str) -> dict:
    """Safely fetch incident data, falling back to incident_a."""
    return INCIDENTS.get(incident_id) or INCIDENTS.get("incident_a") or {}


def query_logs_impl(
    service: str,
    start_time: str,
    end_time: str,
    level: str = "error",
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    all_logs = incident.get("signals", {}).get("logs", [])
    results = [
        log
        for log in all_logs
        if log.get("service") == service and log.get("level") == level
    ]
    return {
        "query": f"logs.{service}.{level}",
        "count": len(results),
        "entries": results[:10],
    }


def query_slack_impl(
    channel: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    all_messages = incident.get("signals", {}).get("slack", [])
    results = [m for m in all_messages if m.get("channel") == channel]
    return {
        "query": f"slack.{channel}",
        "count": len(results),
        "messages": results,
    }


def query_pagerduty_impl(
    start_time: str,
    end_time: str,
    service: str = None,
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    results = incident.get("signals", {}).get("pagerduty", [])
    if service:
        results = [r for r in results if r.get("service") == service]
    return {
        "query": "pagerduty.alerts",
        "count": len(results),
        "alerts": results,
    }


def query_github_impl(
    repo: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    all_commits = incident.get("signals", {}).get("github", [])
    results = [c for c in all_commits if c.get("repo") == repo]
    return {
        "query": f"github.{repo}",
        "count": len(results),
        "commits": results,
    }


def query_metrics_impl(
    service: str,
    metric: str,
    start_time: str,
    end_time: str,
    incident_id: str = "incident_a",
) -> dict:
    incident = _get_incident(incident_id)
    metrics_data = incident.get("signals", {}).get("metrics", {}).get(service, {})
    time_series = metrics_data.get(metric, [])
    return {
        "query": f"metrics.{service}.{metric}",
        "count": len(time_series),
        "datapoints": time_series,
    }


def flag_for_review_impl(
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
