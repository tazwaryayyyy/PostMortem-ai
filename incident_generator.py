"""
PostMortem.ai — Incident Generator
====================================
Generates realistic incident JSON files on-demand using Groq LLM.
Demonstrates infinite scenario coverage for demos.
"""

import json
import os
import uuid
from pathlib import Path

from groq import Groq

_GENERATION_PROMPT = """You are a site reliability engineer creating a realistic incident scenario for a post-mortem investigation training tool.

Generate a complete incident JSON for the following parameters:
- Service: {service_name}
- Incident type: {incident_type}
- Severity: {severity}

The incident MUST have exactly 3 hypotheses in expected_hypothesis_sequence:
- Hypothesis 1: An obvious-but-wrong hypothesis ("should_reject": true)
- Hypothesis 2: Another plausible-but-wrong hypothesis ("should_reject": true)
- Hypothesis 3: The actual root cause ("should_reject": false)

Return ONLY valid JSON, no prose. Match this exact schema:

{{
  "incident_id": "generated_{uuid}",
  "title": "Short descriptive title",
  "start_time": "2024-06-10T14:00:00Z",
  "end_time": "2024-06-10T15:30:00Z",
  "correct_root_cause": "Detailed technical root cause explanation",
  "detection_gap": "Why monitoring failed to catch this faster",
  "contributing_factors": ["factor1", "factor2", "factor3"],
  "impact": {{
    "duration_minutes": 90,
    "affected_users": "~5,000",
    "revenue_impact": "$45,000",
    "severity": "{severity}"
  }},
  "signals": {{
    "pagerduty": [
      {{"timestamp": "...", "title": "...", "severity": "critical", "service": "{service_name}"}}
    ],
    "slack": [
      {{"timestamp": "...", "channel": "#incidents", "user": "@oncall", "message": "..."}}
    ],
    "logs": [
      {{"timestamp": "...", "service": "{service_name}", "level": "error", "message": "..."}}
    ],
    "github": [
      {{"timestamp": "...", "repo": "{service_name}", "author": "dev1", "message": "...", "sha": "abc1234"}}
    ],
    "metrics": {{
      "{service_name}": {{
        "latency_p99_ms": [
          {{"timestamp": "...", "value": 120}},
          {{"timestamp": "...", "value": 4500}}
        ]
      }}
    }}
  }},
  "expected_hypothesis_sequence": [
    {{
      "hypothesis": "Wrong hypothesis 1",
      "should_reject": true,
      "data_to_query": ["query_metrics({service_name}, latency_p99_ms)"],
      "rejection_evidence": "Specific data that disproves this"
    }},
    {{
      "hypothesis": "Wrong hypothesis 2",
      "should_reject": true,
      "data_to_query": ["query_github({service_name})"],
      "rejection_evidence": "Specific data that disproves this"
    }},
    {{
      "hypothesis": "Correct root cause hypothesis",
      "should_reject": false,
      "data_to_query": ["query_logs({service_name})"],
      "confirming_evidence": "Specific data that confirms this root cause"
    }}
  ],
  "action_items": [
    "Specific actionable fix 1",
    "Specific actionable fix 2",
    "Specific monitoring improvement",
    "Runbook update"
  ]
}}"""


def generate_incident(
    service_name: str,
    incident_type: str,
    severity: str,
) -> dict:
    """
    Generate a realistic incident JSON using Groq LLM.

    Parameters
    ----------
    service_name : str
        Name of the affected service (e.g. 'checkout-api').
    incident_type : str
        Type of incident: latency | error | crash | silent-failure.
    severity : str
        Incident severity: P1 | P2 | P3.

    Returns
    -------
    dict
        Generated incident data with a unique incident_id.
    """
    client = Groq()
    incident_uuid = uuid.uuid4().hex[:8]

    # Sanitize service_name to prevent prompt injection via special characters.
    import re as _re
    service_name = _re.sub(r"[^a-zA-Z0-9\-_.]", "-", service_name)[:60]

    prompt = _GENERATION_PROMPT.format(
        service_name=service_name,
        incident_type=incident_type,
        severity=severity,
        uuid=incident_uuid,
    )

    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
        max_tokens=2500,
    )

    raw = resp.choices[0].message.content or ""

    # Strip markdown fences and slice to outermost JSON braces
    raw = raw.strip()
    if raw.startswith("```"):
        import re as _re_fence
        raw = _re_fence.sub(r"^```[a-zA-Z]*\s*\n?", "", raw)
        raw = _re_fence.sub(r"\n?```.*$", "", raw,
                            flags=_re_fence.MULTILINE).strip()
    _s, _e = raw.find("{"), raw.rfind("}") + 1
    if _s != -1 and _e > _s:
        raw = raw[_s:_e]

    incident_data = json.loads(raw)

    # Ensure the incident_id is unique and predictable
    incident_id = f"generated_{incident_uuid}"
    incident_data["incident_id"] = incident_id

    # Persist to incidents directory (absolute path avoids cwd-dependent failures)
    output_path = Path(__file__).parent / "incidents" / f"{incident_id}.json"
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(incident_data, f, indent=2)

    return incident_data
