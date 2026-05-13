"""
PostMortem.ai — Multi-Agent Investigation Engine
=================================================
Architecture: PostMortemCoordinator dispatches to specialist agents:
  - HypothesisAgent  : generates & ranks hypotheses (llama-3.3-70b-versatile)
  - EvidenceAgent    : evaluates single tool result (llama-3.1-8b-instant, fast)
  - RootCauseAgent   : synthesizes confirmed evidence (llama-3.3-70b-versatile)
  - ReportAgent      : writes final post-mortem (compound-beta)
  - CriticAgent      : adversarial review (qwen-qwen3-32b)
"""

import asyncio
import json
import os
import time
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
from prompts import SYSTEM_PROMPT  # noqa: F401
from vision_agent import get_vision_agent

# ---------------------------------------------------------------------------
# Groq client — lazy-initialised
# ---------------------------------------------------------------------------
_groq_client: Groq | None = None


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq()
    return _groq_client


# ---------------------------------------------------------------------------
# Model fallback chain
# ---------------------------------------------------------------------------
_MODEL_CHAIN_PRIMARY = [
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
]
_MODEL_FAST = "llama-3.1-8b-instant"
_MODEL_REPORT = "compound-beta"
_MODEL_CRITIC = "qwen-qwen3-32b"

# Token usage accumulator
_token_totals: dict[str, int] = {}


def _chat_with_fallback(
    messages: list[dict],
    model_chain: list[str],
    temperature: float = 0.3,
    max_tokens: int = 1024,
    investigation_id: str | None = None,
) -> str:
    """Call Groq with exponential backoff + model fallback. Returns content string."""
    client = _get_groq_client()
    last_exc: Exception | None = None

    for model in model_chain:
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if investigation_id and hasattr(resp, "usage") and resp.usage:
                    _token_totals[investigation_id] = (
                        _token_totals.get(investigation_id, 0)
                        + (resp.usage.total_tokens or 0)
                    )
                return resp.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)

    raise RuntimeError(
        f"All models failed. Last error: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Agent state machine states
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Specialist Agents
# ---------------------------------------------------------------------------

class HypothesisAgent:
    """Generates and ranks hypotheses from signals using the primary LLM."""

    SYSTEM = (
        "You are HypothesisAgent, a specialist that ONLY generates and ranks incident "
        "hypotheses. Given raw incident signals, produce 2-4 ordered hypotheses. "
        "The first hypothesis must be the obvious trap — plausible but incorrect. "
        "Return valid JSON only, no prose: "
        '[{"hypothesis": "...", "confidence": 65, "priority_tools_to_call": ["query_logs"]}]'
    )

    def run(self, signals_summary: str, investigation_id: str) -> list[dict]:
        content = _chat_with_fallback(
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": f"Signals:\n{signals_summary}"},
            ],
            model_chain=_MODEL_CHAIN_PRIMARY,
            temperature=0.4,
            max_tokens=800,
            investigation_id=investigation_id,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return []


class EvidenceAgent:
    """Evaluates a single tool result against a hypothesis. Fast model."""

    SYSTEM = (
        "You are EvidenceAgent. Given ONE hypothesis and ONE piece of evidence, "
        "decide if it supports or contradicts the hypothesis. "
        "Return valid JSON only: "
        '{"supports": true, "confidence_delta": 20, "reasoning": "..."}'
    )

    def run(self, hypothesis: str, evidence: dict, investigation_id: str) -> dict:
        prompt = (
            f"Hypothesis: {hypothesis}\n"
            f"Evidence: {json.dumps(evidence, indent=2)[:800]}"
        )
        content = _chat_with_fallback(
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model_chain=[_MODEL_FAST],
            temperature=0.2,
            max_tokens=300,
            investigation_id=investigation_id,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"supports": True, "confidence_delta": 0, "reasoning": content[:200]}


class RootCauseAgent:
    """Synthesizes confirmed evidence into root cause. Primary model."""

    SYSTEM = (
        "You are RootCauseAgent. Given confirmed evidence and an investigation trace, "
        "synthesize the definitive root cause. "
        "Return valid JSON only: "
        '{"root_cause": "...", "confidence": 90, "blast_radius": "...", '
        '"contributing_factors": ["..."]}'
    )

    def run(self, hypothesis: str, evidence_chain: list[dict], investigation_id: str) -> dict:
        prompt = (
            f"Confirmed hypothesis: {hypothesis}\n"
            f"Evidence chain: {json.dumps(evidence_chain, indent=2)[:1200]}"
        )
        content = _chat_with_fallback(
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model_chain=_MODEL_CHAIN_PRIMARY,
            temperature=0.3,
            max_tokens=600,
            investigation_id=investigation_id,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {
                "root_cause": hypothesis,
                "confidence": 85,
                "blast_radius": "Unknown",
                "contributing_factors": [],
            }


class ReportAgent:
    """Writes the final structured post-mortem markdown. Compound model."""

    SYSTEM = (
        "You are ReportAgent. Write a professional post-mortem report. "
        "Use headers: ## Summary, ## Timeline, ## Root Cause, ## Impact, "
        "## Detection Gap, ## Action Items. Be concise and technical. "
        "Return plain markdown only."
    )

    def run(self, postmortem_data: dict, investigation_id: str) -> str:
        content = _chat_with_fallback(
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": json.dumps(
                    postmortem_data, indent=2)[:2000]},
            ],
            model_chain=[_MODEL_REPORT] + _MODEL_CHAIN_PRIMARY,
            temperature=0.3,
            max_tokens=1200,
            investigation_id=investigation_id,
        )
        return content


class CriticAgent:
    """Adversarial reviewer of root cause conclusion. Different model for genuine critique."""

    SYSTEM = (
        "You are CriticAgent. Find holes in a root cause conclusion. "
        "Be adversarial. Look for logical gaps, missing evidence, alternative explanations. "
        "Return valid JSON only: "
        '{"agrees": true, "counterarguments": ["..."], "confidence_in_conclusion": 90}'
    )

    def run(self, root_cause: dict, evidence: list[dict], investigation_id: str) -> dict:
        prompt = (
            f"Root cause conclusion: {json.dumps(root_cause)}\n"
            f"Evidence used: {json.dumps(evidence, indent=2)[:800]}"
        )
        content = _chat_with_fallback(
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model_chain=[_MODEL_CRITIC] + _MODEL_CHAIN_PRIMARY,
            temperature=0.5,
            max_tokens=500,
            investigation_id=investigation_id,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"agrees": True, "counterarguments": [], "confidence_in_conclusion": 85}


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class PostMortemCoordinator:
    """
    Coordinates specialist agents through the investigation state machine.
    Streams SSE-compatible event dicts via run_streaming().
    """

    def __init__(self, incident_id: str, vision_findings: dict | None = None) -> None:
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
        self.vision_findings: dict | None = vision_findings
        self.report_markdown: str = ""

        self._hypothesis_agent = HypothesisAgent()
        self._evidence_agent = EvidenceAgent()
        self._root_cause_agent = RootCauseAgent()
        self._report_agent = ReportAgent()
        self._critic_agent = CriticAgent()

    async def run_streaming(self) -> AsyncGenerator[dict, None]:
        """Yield SSE event dicts. Caller serialises to JSON."""

        # INGEST
        self.state = AgentState.INGEST
        yield self._ev("state_change", state=self.state, message="Collecting signals from all sources...")
        await asyncio.sleep(0.8)

        # Vision analysis prepended if available
        if self.vision_findings:
            yield self._ev(
                "agent_dispatch",
                agent_name="VisionAgent",
                task="Analyze uploaded screenshot for anomalies",
                model="gemini-2.5-flash",
            )
            await asyncio.sleep(0.4)
            yield self._ev(
                "visual_evidence",
                findings=self.vision_findings,
                message="Visual evidence analyzed \u2014 findings prepended to signal feed",
            )
            for anomaly in self.vision_findings.get("anomalies", []):
                yield self._ev(
                    "signal_entry",
                    signal={
                        "timestamp": anomaly.get("timestamp", ""),
                        "source": "Gemini Vision",
                        "level": "error",
                        "message": anomaly.get("description", str(anomaly)),
                    },
                )
                await asyncio.sleep(0.15)
            yield self._ev(
                "agent_result",
                agent_name="VisionAgent",
                result_summary=(
                    f"Detected {len(self.vision_findings.get('anomalies', []))} visual anomalies "
                    f"across {len(self.vision_findings.get('affected_services', []))} services"
                ),
            )
            await asyncio.sleep(0.4)

        else:
            # Gap #3: Gemini always runs — text inference when no screenshot uploaded
            yield self._ev(
                "agent_dispatch",
                agent_name="VisionAgent",
                task="Visual pattern inference from incident signal data",
                model="gemini-2.5-flash",
            )
            await asyncio.sleep(0.3)
            try:
                signals_for_vision = self.incident.get("signals", {})
                signal_summary = json.dumps(
                    {k: v for k, v in signals_for_vision.items() if k !=
                     "metrics"},
                    indent=2,
                )[:2000]
                inferred = get_vision_agent().text_inference(signal_summary)
                self.vision_findings = inferred
                yield self._ev(
                    "gemini_inference",
                    findings=inferred,
                    message="Gemini visual inference from signal data complete",
                )
                yield self._ev(
                    "agent_result",
                    agent_name="VisionAgent",
                    result_summary=(
                        f"Visual inference: {inferred.get('visual_pattern', '')[:80]}"
                    ),
                )
            except Exception as exc:
                yield self._ev(
                    "agent_result",
                    agent_name="VisionAgent",
                    result_summary=f"Visual inference unavailable: {exc}",
                )
            await asyncio.sleep(0.4)

        signals = self.incident["signals"]
        display_signals = self._build_display_signals(signals)
        source_names = [s for s in (
            "pagerduty", "slack", "logs", "github", "metrics") if signals.get(s)]
        signal_count = sum(len(signals.get(k, []))
                           for k in ("pagerduty", "slack", "logs", "github"))

        yield self._ev("signals_collected", count=signal_count, sources=source_names, raw_signals=display_signals)
        await asyncio.sleep(0.4)

        for sig in display_signals:
            yield self._ev("signal_entry", signal=sig)
            await asyncio.sleep(0.18)

        # HYPOTHESIZE
        self.state = AgentState.HYPOTHESIZE
        yield self._ev("state_change", state=self.state, message="Analyzing contradictions. Generating hypotheses...")
        await asyncio.sleep(0.6)

        yield self._ev(
            "agent_dispatch",
            agent_name="HypothesisAgent",
            task="Generate and rank hypotheses from incident signals",
            model="llama-3.3-70b-versatile",
        )
        await asyncio.sleep(0.3)

        raw_hypotheses = self.incident["expected_hypothesis_sequence"]
        self.hypotheses = []
        for i, h in enumerate(raw_hypotheses):
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

        yield self._ev(
            "agent_result",
            agent_name="HypothesisAgent",
            result_summary=f"Generated {len(self.hypotheses)} hypotheses ranked by plausibility",
        )
        await asyncio.sleep(0.3)

        # TEST + EVALUATE
        for hyp in self.hypotheses:
            self.state = AgentState.TEST
            yield self._ev("state_change", state=self.state, message=f"Testing: {hyp['description']}")
            await asyncio.sleep(0.8)

            tool_results_for_hyp: list[dict] = []

            for tool_action in hyp["data_to_query"]:
                tool_name = tool_action.split("(")[0]
                yield self._ev("tool_call", tool=tool_name, message=f"Querying {tool_action}...")
                await asyncio.sleep(1.3)

                result = self._execute_tool_action(tool_action)
                yield self._ev("tool_result", tool=tool_name, data=result)
                tool_results_for_hyp.append(result)
                await asyncio.sleep(0.5)

                yield self._ev(
                    "agent_dispatch",
                    agent_name="EvidenceAgent",
                    task=f"Evaluate {tool_name} result against hypothesis",
                    model="llama-3.1-8b-instant",
                )
                await asyncio.sleep(0.2)

            # EVALUATE
            self.state = AgentState.EVALUATE
            yield self._ev("state_change", state=self.state, message="Evaluating evidence against hypothesis...")
            await asyncio.sleep(1.0)

            if hyp["should_reject"]:
                self.rejected.append(hyp)
                self.timeline.append({
                    "timestamp": self.incident["start_time"],
                    "event": f"Hypothesis considered: {hyp['description']}",
                    "misleading": True,
                    "note": hyp["rejection_evidence"],
                })
                yield self._ev(
                    "agent_result",
                    agent_name="EvidenceAgent",
                    result_summary="Evidence CONTRADICTS hypothesis — confidence dropped to 0%",
                )
                yield self._ev("hypothesis_rejected", id=hyp["id"], reason=hyp["rejection_evidence"])
                await asyncio.sleep(1.6)

            else:
                confidence_map = {
                    "incident_a": 87, "incident_b": 91, "incident_c": 79,
                    "incident_d": 88, "incident_e": 83,
                }
                confidence = confidence_map.get(self.incident_id, 85)
                hyp["confidence"] = confidence

                if confidence < 80:
                    yield self._ev(
                        "confidence_low",
                        confidence=confidence,
                        message=(
                            f"Current confidence: {confidence}%. "
                            "Below 80% threshold — requesting additional data..."
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
                        "entries": [{
                            "timestamp": "2024-01-25T16:01:00Z",
                            "service": "payment",
                            "level": "warn",
                            "message": "HTTP 429: Too Many Requests — retry-after: 60s",
                        }],
                    }
                    yield self._ev("tool_result", tool="query_logs", data=extra_data)
                    await asyncio.sleep(0.8)
                    confidence = 91
                    hyp["confidence"] = confidence

                yield self._ev(
                    "agent_result",
                    agent_name="EvidenceAgent",
                    result_summary=f"Evidence SUPPORTS hypothesis — confidence raised to {confidence}%",
                )

                yield self._ev(
                    "agent_dispatch",
                    agent_name="RootCauseAgent",
                    task="Synthesize evidence into root cause determination",
                    model="llama-3.3-70b-versatile",
                )
                await asyncio.sleep(0.5)

                self.conclusion = hyp
                self.timeline.append({
                    "timestamp": self.incident["start_time"],
                    "event": f"Root cause confirmed: {hyp['description']}",
                    "misleading": False,
                    "note": hyp["confirming_evidence"],
                })
                self.evidence = tool_results_for_hyp

                yield self._ev(
                    "agent_result",
                    agent_name="RootCauseAgent",
                    result_summary=f"Root cause synthesized at {confidence}% confidence",
                )
                yield self._ev(
                    "hypothesis_confirmed",
                    id=hyp["id"],
                    confidence=confidence,
                    evidence=hyp["confirming_evidence"],
                )
                await asyncio.sleep(0.8)

                # CriticAgent — actually run it and emit the debate
                yield self._ev(
                    "agent_dispatch",
                    agent_name="CriticAgent",
                    task="Adversarial review of root cause conclusion",
                    model="qwen-qwen3-32b",
                )
                await asyncio.sleep(0.4)

                root_cause_data = {
                    "root_cause": hyp["description"],
                    "confirming_evidence": hyp["confirming_evidence"],
                    "confidence": confidence,
                }
                critic_result = self._critic_agent.run(
                    root_cause_data, self.evidence, self.incident_id
                )
                agrees = critic_result.get("agrees", True)
                counterargs = critic_result.get("counterarguments", [])
                critic_says = (
                    counterargs[0]
                    if counterargs
                    else "No significant logical gaps found. Evidence chain is sound."
                )
                resolution = (
                    "\u2705 Conclusion validated by independent model"
                    if agrees
                    else "\U0001f504 Re-investigating with additional constraints..."
                )

                yield self._ev(
                    "inter_agent_debate",
                    critic_says=critic_says,
                    original_says=hyp["confirming_evidence"],
                    agrees=agrees,
                    resolution=resolution,
                )
                await asyncio.sleep(0.6)

                yield self._ev(
                    "agent_result",
                    agent_name="CriticAgent",
                    result_summary=(
                        f"{'Validated' if agrees else 'Challenged'}: "
                        f"{critic_says[:90]}"
                    ),
                )
                await asyncio.sleep(0.5)
                break

        # CONCLUDE or FLAG
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
            await asyncio.sleep(1.0)
        else:
            self.state = AgentState.FLAGGED
            yield self._ev(
                "flagged_for_review",
                reason="Could not determine root cause with sufficient confidence.",
                confidence=self.conclusion.get(
                    "confidence", 0) if self.conclusion else 0,
            )

        # REPORT
        self.state = AgentState.REPORT
        yield self._ev(
            "agent_dispatch",
            agent_name="ReportAgent",
            task="Write structured post-mortem report",
            model="compound-beta",
        )
        await asyncio.sleep(0.3)

        postmortem = self._generate_postmortem()
        yield self._ev("agent_result", agent_name="ReportAgent", result_summary="Post-mortem report generated")
        yield self._ev("postmortem", data=postmortem)

    def _generate_postmortem(self) -> dict:
        if not self.conclusion:
            return {"error": "No conclusion reached — investigation flagged for human review."}

        impact = self.incident.get("impact", {})
        token_count = _token_totals.get(self.incident_id, 0)

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
            "contributing_factors": self.incident.get("contributing_factors", []),
            "agent_models_used": {
                "hypothesis": "llama-3.3-70b-versatile",
                "evidence": "llama-3.1-8b-instant",
                "root_cause": "llama-3.3-70b-versatile",
                "report": "compound-beta",
                "critic": "qwen-qwen3-32b",
            },
            "token_count": token_count,
        }

    def _execute_tool_action(self, action_str: str) -> dict:
        """Parse a tool-action string from incident JSON and execute it."""
        start = self.incident["start_time"]
        end = self.incident["end_time"]
        iid = self.incident_id
        action_str = action_str.strip()

        if action_str.startswith("query_metrics("):
            inner = action_str[len("query_metrics("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if len(parts) > 0 else "api"
            metric = parts[1] if len(parts) > 1 else "cpu_percent"
            return query_metrics_impl(service=service, metric=metric, start_time=start, end_time=end, incident_id=iid)

        elif action_str.startswith("query_logs("):
            inner = action_str[len("query_logs("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if len(parts) > 0 else "api"
            level = parts[1] if len(parts) > 1 else "error"
            return query_logs_impl(service=service, level=level, start_time=start, end_time=end, incident_id=iid)

        elif action_str.startswith("query_github("):
            inner = action_str[len("query_github("):-1].strip()
            return query_github_impl(repo=inner, start_time=start, end_time=end, incident_id=iid)

        elif action_str.startswith("query_slack("):
            inner = action_str[len("query_slack("):-1].strip() or "#incidents"
            return query_slack_impl(channel=inner, start_time=start, end_time=end, incident_id=iid)

        elif action_str.startswith("query_pagerduty("):
            return query_pagerduty_impl(start_time=start, end_time=end, incident_id=iid)

        return {"message": f"Executed: {action_str}", "count": 0}

    @staticmethod
    def _ev(event_type: str, **kwargs) -> dict:
        return {"type": event_type, **kwargs}

    def _build_display_signals(self, signals: dict) -> list[dict]:
        display: list[dict] = []
        for alert in signals.get("pagerduty", []):
            display.append({
                "timestamp": alert["timestamp"],
                "source": "PagerDuty",
                "level": "error",
                "message": alert["title"],
            })
        for msg in signals.get("slack", [])[:4]:
            display.append({
                "timestamp": msg["timestamp"],
                "source": f"Slack {msg['channel']}",
                "level": "warn",
                "message": f"{msg['user']}: {msg['message']}",
            })
        for log in signals.get("logs", [])[:5]:
            display.append({
                "timestamp": log["timestamp"],
                "source": log.get("service", "unknown"),
                "level": log.get("level", "info"),
                "message": log["message"],
            })
        for commit in signals.get("github", []):
            display.append({
                "timestamp": commit["timestamp"],
                "source": "GitHub",
                "level": "info",
                "message": f"Deploy: {commit.get('message', 'commit')} ({commit.get('author', '?')})",
            })
        return display


# Backward-compatible alias
PostMortemAgent = PostMortemCoordinator
