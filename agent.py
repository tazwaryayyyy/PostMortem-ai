import asyncio
import json
import os
from enum import Enum
from typing import AsyncGenerator

from groq import Groq

from mock_apis import (
    INCIDENTS,
    flag_for_review_impl,
    query_github_impl,
    query_logs_impl,
    query_metrics_impl,
    query_pagerduty_impl,
    query_slack_impl,
)
from prompts import SYSTEM_PROMPT  # noqa: F401 — kept for reference

# ---------------------------------------------------------------------------
# Groq client — lazy-initialised so the module can be imported without
# GROQ_API_KEY set (useful for tests and local dev without the key ready).
# ---------------------------------------------------------------------------
_groq_client: Groq | None = None


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq()  # reads GROQ_API_KEY from env
    return _groq_client


class AgentState(str, Enum):
    INGEST = "INGEST"
    HYPOTHESIZE = "HYPOTHESIZE"
    TEST = "TEST"
    EVALUATE = "EVALUATE"
    CONCLUDE = "CONCLUDE"
    FLAGGED = "FLAGGED"
    REPORT = "REPORT"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
TOOL_REGISTRY = {
    "query_logs": query_logs_impl,
    "query_slack": query_slack_impl,
    "query_pagerduty": query_pagerduty_impl,
    "query_github": query_github_impl,
    "query_metrics": query_metrics_impl,
    "flag_for_review": flag_for_review_impl,
}


class PostMortemAgent:
    """
    Autonomous incident investigation agent.

    Streams SSE-compatible event dicts via ``run_streaming()``.
    All investigation logic is deterministic (driven by incident JSON) so that
    demo behaviour is reproducible and the rejection moments are always crisp.
    """

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        self.incident = INCIDENTS.get(incident_id)
        if not self.incident:
            raise ValueError(f"Unknown incident ID: {incident_id!r}")

        self.state: AgentState = AgentState.INGEST
        self.hypotheses: list[dict] = []
        self.evidence: list[dict] = []
        self.rejected: list[dict] = []
        self.conclusion: dict | None = None
        self.timeline: list[dict] = []

    # ------------------------------------------------------------------
    # Public streaming entry-point
    # ------------------------------------------------------------------

    async def run_streaming(self) -> AsyncGenerator[dict, None]:
        """Yield SSE event dicts. Caller serialises to JSON."""

        # ── INGEST ──────────────────────────────────────────────────────
        self.state = AgentState.INGEST
        yield self._ev("state_change", state=self.state, message="Collecting signals from all sources...")
        await asyncio.sleep(0.8)

        signals = self.incident["signals"]
        display_signals = self._build_display_signals(signals)
        source_names = [s for s in ("pagerduty", "slack", "logs", "github", "metrics") if signals.get(s)]
        signal_count = sum(
            len(signals.get(k, [])) for k in ("pagerduty", "slack", "logs", "github")
        )

        yield self._ev(
            "signals_collected",
            count=signal_count,
            sources=source_names,
            raw_signals=display_signals,
        )
        await asyncio.sleep(0.4)

        for sig in display_signals:
            yield self._ev("signal_entry", signal=sig)
            await asyncio.sleep(0.18)

        # ── HYPOTHESIZE ─────────────────────────────────────────────────
        self.state = AgentState.HYPOTHESIZE
        yield self._ev("state_change", state=self.state, message="Analyzing contradictions. Generating hypotheses...")
        await asyncio.sleep(1.0)

        raw_hypotheses = self.incident["expected_hypothesis_sequence"]
        self.hypotheses = []
        for i, h in enumerate(raw_hypotheses):
            is_last = i == len(raw_hypotheses) - 1
            hyp = {
                "id": i + 1,
                "description": h["hypothesis"],
                "confidence": 65 if h.get("should_reject") else 80,
                "data_to_query": h["data_to_query"],
                "should_reject": bool(h.get("should_reject")),
                "rejection_evidence": h.get("rejection_evidence", ""),
                "confirming_evidence": h.get("confirming_evidence", ""),
            }
            self.hypotheses.append(hyp)
            yield self._ev(
                "hypothesis_created",
                id=hyp["id"],
                description=hyp["description"],
                confidence=hyp["confidence"],
            )
            await asyncio.sleep(0.65)

        # ── TEST + EVALUATE each hypothesis ─────────────────────────────
        for hyp in self.hypotheses:
            self.state = AgentState.TEST
            yield self._ev(
                "state_change",
                state=self.state,
                message=f"Testing: {hyp['description']}",
            )
            await asyncio.sleep(0.8)

            # Execute every tool action listed for this hypothesis
            for tool_action in hyp["data_to_query"]:
                tool_name = tool_action.split("(")[0]
                yield self._ev("tool_call", tool=tool_name, message=f"Querying {tool_action}...")
                await asyncio.sleep(1.3)

                result = self._execute_tool_action(tool_action)
                yield self._ev("tool_result", tool=tool_name, data=result)
                await asyncio.sleep(0.5)

            # ── EVALUATE ────────────────────────────────────────────────
            self.state = AgentState.EVALUATE
            yield self._ev("state_change", state=self.state, message="Evaluating evidence against hypothesis...")
            await asyncio.sleep(1.0)

            if hyp["should_reject"]:
                self.rejected.append(hyp)
                self.timeline.append(
                    {
                        "timestamp": self.incident["start_time"],
                        "event": f"Hypothesis considered: {hyp['description']}",
                        "misleading": True,
                        "note": hyp["rejection_evidence"],
                    }
                )
                yield self._ev(
                    "hypothesis_rejected",
                    id=hyp["id"],
                    reason=hyp["rejection_evidence"],
                )
                await asyncio.sleep(1.6)  # Let the rejection land visually

            else:
                # Determine base confidence per incident
                confidence_map = {"incident_a": 87, "incident_b": 91, "incident_c": 79}
                confidence = confidence_map.get(self.incident_id, 85)
                hyp["confidence"] = confidence

                # Incident C: confidence-gate moment
                if confidence < 80:
                    yield self._ev(
                        "confidence_low",
                        confidence=confidence,
                        message=(
                            f"Current confidence: {confidence}%. "
                            "Below my 80% threshold. Requesting additional data to confirm..."
                        ),
                    )
                    await asyncio.sleep(1.6)

                    yield self._ev(
                        "tool_call",
                        tool="query_logs",
                        message="Requesting transaction-level payment logs...",
                    )
                    await asyncio.sleep(1.9)

                    extra_data = {
                        "message": "HTTP 429 responses found in payment transaction logs",
                        "count": 47,
                        "entries": [
                            {
                                "timestamp": "2024-01-25T16:01:00Z",
                                "service": "payment",
                                "level": "warn",
                                "message": "HTTP 429: Too Many Requests — retry-after: 60s",
                            }
                        ],
                    }
                    yield self._ev("tool_result", tool="query_logs", data=extra_data)
                    await asyncio.sleep(0.8)

                    confidence = 91
                    hyp["confidence"] = confidence

                self.conclusion = hyp
                self.timeline.append(
                    {
                        "timestamp": self.incident["start_time"],
                        "event": f"Root cause confirmed: {hyp['description']}",
                        "misleading": False,
                        "note": hyp["confirming_evidence"],
                    }
                )
                yield self._ev(
                    "hypothesis_confirmed",
                    id=hyp["id"],
                    confidence=confidence,
                    evidence=hyp["confirming_evidence"],
                )
                await asyncio.sleep(0.8)
                break  # Stop testing further hypotheses

        # ── CONCLUDE or FLAG ────────────────────────────────────────────
        if self.conclusion and self.conclusion.get("confidence", 0) >= 80:
            self.state = AgentState.CONCLUDE
            yield self._ev(
                "state_change",
                state=self.state,
                message=(
                    f"Root cause identified with {self.conclusion['confidence']}% confidence. "
                    "Preparing post-mortem..."
                ),
            )
            await asyncio.sleep(0.5)

            yield self._ev(
                "governance_gate",
                root_cause=self.conclusion["description"],
                confidence=self.conclusion["confidence"],
                message="Root cause identified. Approve post-mortem generation?",
            )
            # The frontend triggers the post-mortem via a separate POST.
            # For the streaming demo we auto-continue after a brief pause.
            await asyncio.sleep(1.0)

        else:
            self.state = AgentState.FLAGGED
            yield self._ev(
                "flagged_for_review",
                reason="Could not determine root cause with sufficient confidence.",
                confidence=self.conclusion.get("confidence", 0) if self.conclusion else 0,
            )

        # ── REPORT ──────────────────────────────────────────────────────
        self.state = AgentState.REPORT
        postmortem = self._generate_postmortem()
        yield self._ev("postmortem", data=postmortem)

    # ------------------------------------------------------------------
    # Post-mortem generation
    # ------------------------------------------------------------------

    def _generate_postmortem(self) -> dict:
        if not self.conclusion:
            return {"error": "No conclusion reached — investigation flagged for human review."}

        impact = self.incident.get("impact", {})
        return {
            "incident_id": self.incident_id,
            "title": self.incident["title"],
            "root_cause": self.incident.get("correct_root_cause", self.conclusion["description"]),
            "confidence": self.conclusion.get("confidence", 0),
            "evidence": [
                e.strip()
                for e in self.conclusion["confirming_evidence"].split(". ")
                if e.strip()
            ],
            "rejected": [
                f"{r['description']} — {r['rejection_evidence']}"
                for r in self.rejected
            ],
            "timeline": self.timeline,
            "impact": {
                "duration_minutes": impact.get("duration_minutes", 0),
                "affected_users": impact.get("affected_users", "Unknown"),
                "revenue_impact": impact.get("revenue_impact", "Unknown"),
                "severity": impact.get("severity", "P2"),
            },
            "action_items": self.incident.get("action_items", []),
            "detection_gap": self.incident.get("detection_gap", ""),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ev(event_type: str, **kwargs) -> dict:
        return {"type": event_type, **kwargs}

    def _build_display_signals(self, signals: dict) -> list[dict]:
        display: list[dict] = []
        for alert in signals.get("pagerduty", []):
            display.append(
                {
                    "timestamp": alert["timestamp"],
                    "source": "PagerDuty",
                    "level": "error",
                    "message": alert["title"],
                }
            )
        for msg in signals.get("slack", [])[:4]:
            display.append(
                {
                    "timestamp": msg["timestamp"],
                    "source": f"Slack {msg['channel']}",
                    "level": "warn",
                    "message": f"{msg['user']}: {msg['message']}",
                }
            )
        for log in signals.get("logs", [])[:5]:
            display.append(
                {
                    "timestamp": log["timestamp"],
                    "source": log.get("service", "unknown"),
                    "level": log.get("level", "info"),
                    "message": log["message"],
                }
            )
        for commit in signals.get("github", []):
            display.append(
                {
                    "timestamp": commit["timestamp"],
                    "source": "GitHub",
                    "level": "info",
                    "message": f"Deploy: {commit['message']}",
                }
            )
        return sorted(display, key=lambda x: x["timestamp"])

    def _execute_tool_action(self, action_str: str) -> dict:
        """Parse a tool-action string from incident JSON and execute it."""
        start = self.incident["start_time"]
        end = self.incident["end_time"]
        iid = self.incident_id

        # Strip whitespace
        action_str = action_str.strip()

        if action_str.startswith("query_metrics("):
            inner = action_str[len("query_metrics("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if len(parts) > 0 else "api"
            metric = parts[1] if len(parts) > 1 else "cpu_percent"
            return query_metrics_impl(
                service=service, metric=metric,
                start_time=start, end_time=end, incident_id=iid,
            )

        elif action_str.startswith("query_logs("):
            inner = action_str[len("query_logs("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if len(parts) > 0 else "api"
            level = parts[1] if len(parts) > 1 else "error"
            return query_logs_impl(
                service=service, level=level,
                start_time=start, end_time=end, incident_id=iid,
            )

        elif action_str.startswith("query_github("):
            inner = action_str[len("query_github("):-1].strip()
            return query_github_impl(
                repo=inner, start_time=start, end_time=end, incident_id=iid,
            )

        elif action_str.startswith("query_slack("):
            inner = action_str[len("query_slack("):-1].strip() or "#incidents"
            return query_slack_impl(
                channel=inner, start_time=start, end_time=end, incident_id=iid,
            )

        elif action_str.startswith("query_pagerduty("):
            return query_pagerduty_impl(
                start_time=start, end_time=end, incident_id=iid,
            )

        # Fallback
        return {"message": f"Executed: {action_str}", "count": 0}
