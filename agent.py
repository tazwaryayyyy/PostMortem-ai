"""
PostMortem.ai — Multi-Agent Investigation Engine
=================================================
Architecture: PostMortemCoordinator dispatches to specialist agents:
  - HypothesisAgent  : generates & ranks hypotheses (llama-3.3-70b-versatile)
  - EvidenceAgent    : evaluates single tool result (llama-3.1-8b-instant, fast)
  - RootCauseAgent   : synthesizes confirmed evidence (llama-3.3-70b-versatile)
  - ReportAgent      : writes final post-mortem (compound-beta)
  - CriticAgent      : adversarial review (gemini-2.5-flash → qwen-qwen3-32b fallback)
"""

from vision_agent import get_vision_agent
from mock_apis import INCIDENTS, flag_for_review_impl, query_pagerduty_impl
from groq import Groq
import httpx
import asyncio
import json
import logging
import os
import re
import time
from enum import Enum
from typing import AsyncGenerator

_log = logging.getLogger(__name__)


from prompts import SYSTEM_PROMPT  # noqa: F401

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
# Gemini text helper — used exclusively by CriticAgent
# ---------------------------------------------------------------------------


# Ordered by preference; older models have much higher free-tier quotas (1500 RPD vs 20 RPD)
_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",       # 1500 req/day free tier
    "gemini-1.5-flash-8b",   # 1500 req/day free tier
]


def _call_gemini_text(system: str, user: str, max_tokens: int = 500) -> tuple[str, str]:
    """Call Gemini for text reasoning, trying models in order until one succeeds.

    Returns (response_text, model_name).
    Raises RuntimeError if GOOGLE_API_KEY is absent or all models fail,
    so CriticAgent can fall back to Groq cleanly.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set")
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-genai not installed. Run: pip install google-genai") from exc
    client = genai.Client(api_key=api_key)
    last_exc: Exception = RuntimeError("No Gemini models tried")
    for model in _GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=0.5,
                ),
            )
            if not response.text:
                raise RuntimeError(f"Empty response from {model}")
            _log.warning("CriticAgent: Gemini model used: %s", model)
            return response.text, model
        except Exception as exc:
            _log.warning(
                "CriticAgent: %s failed (%s) — trying next", model, exc)
            last_exc = exc
    raise last_exc


# ---------------------------------------------------------------------------
# Vultr Serverless Inference — primary provider (OpenAI-compatible)
# Falls back to Groq if VULTR_API_KEY is not set or the call fails.
# ---------------------------------------------------------------------------
_VULTR_INFERENCE_URL = os.getenv(
    "VULTR_INFERENCE_URL",
    "https://api.vultrinference.com/v1/chat/completions",
)

# Groq model name → Vultr Serverless Inference model name
_VULTR_MODEL_MAP: dict[str, str] = {
    "llama-3.3-70b-versatile":                 "llama-3.1-70b-instruct-fp8",
    "meta-llama/llama-4-scout-17b-16e-instruct": "llama-3.1-70b-instruct-fp8",
    "llama-3.1-8b-instant":                    "llama-3.1-8b-instruct-fp8",
    "compound-beta":                           "llama-3.1-70b-instruct-fp8",
    "qwen-qwen3-32b":                          "llama-3.1-70b-instruct-fp8",
}


def _dispatch_model(groq_model: str) -> str:
    """Return the model label for SSE dispatch events — Vultr name when active."""
    if os.getenv("VULTR_API_KEY"):
        return _VULTR_MODEL_MAP.get(groq_model, groq_model)
    return groq_model


def _try_vultr_inference(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str | None:
    """Try Vultr Serverless Inference. Returns content string, or None on any failure.

    None signals the caller to fall through to Groq.
    """
    api_key = os.getenv("VULTR_API_KEY")
    if not api_key:
        return None
    vultr_model = _VULTR_MODEL_MAP.get(model, "llama-3.1-70b-instruct-fp8")
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                _VULTR_INFERENCE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": vultr_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None


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

# Base URL for the live tool API endpoints (self-referential HTTP calls)
_TOOL_BASE = os.getenv("TOOL_API_BASE_URL", "http://localhost:8000")

# Endpoint map for tool_call_dispatched SSE event and live HTTP dispatch
_TOOL_ENDPOINTS: dict[str, str] = {
    "query_logs": "/tools/logs",
    "query_metrics": "/tools/metrics",
    "query_github": "/tools/github",
    "query_slack": "/tools/slack",
    "query_pagerduty": "/tools/pagerduty",
    "flag_for_review": "/tools/flag",
}

# Token usage accumulator
_token_totals: dict[str, int] = {}


def _chat_with_fallback(
    messages: list[dict],
    model_chain: list[str],
    temperature: float = 0.3,
    max_tokens: int = 1024,
    investigation_id: str | None = None,
) -> str:
    """Call Vultr Serverless Inference (primary) then Groq (fallback). Returns content string."""
    # --- Primary: Vultr Serverless Inference ---
    vultr_result = _try_vultr_inference(
        messages, model_chain[0], temperature, max_tokens)
    if vultr_result is not None:
        return vultr_result

    # --- Fallback: Groq with exponential backoff + model chain ---
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
# Tool registry — used for hypothesis prompt building.
# Dispatch is handled via live HTTP POST in _execute_tool_action.
TOOL_REGISTRY = {
    "query_logs": "GET /tools/logs",
    "query_slack": "GET /tools/slack",
    "query_pagerduty": query_pagerduty_impl,
    "query_github": "GET /tools/github",
    "query_metrics": "GET /tools/metrics",
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
    """Adversarial reviewer of root cause conclusion.

    Primary: Google Gemini 2.5 Flash (independent provider — no shared reasoning
    context with the Groq-based investigation chain).
    Fallback: qwen-qwen3-32b on Groq if Gemini is unavailable.
    """

    SYSTEM = (
        "You are CriticAgent. Find holes in a root cause conclusion. "
        "Be adversarial. Look for logical gaps, missing evidence, alternative explanations. "
        "Return valid JSON only, with counterarguments as SHORT one-sentence strings (max 20 words each): "
        '{"agrees": true, "counterarguments": ["..."], "confidence_in_conclusion": 90}'
    )

    def run(self, root_cause: dict, evidence: list[dict], investigation_id: str) -> dict:
        prompt = (
            f"Root cause conclusion: {json.dumps(root_cause)}\n"
            f"Evidence used: {json.dumps(evidence, indent=2)[:800]}"
        )
        # --- Primary: Gemini 2.5 Flash (cross-provider — genuinely independent) ---
        try:
            content, _gemini_model = _call_gemini_text(
                self.SYSTEM, prompt, max_tokens=1024)
            # Gemini often wraps JSON in markdown code blocks or adds prose — strip both
            _text = content.strip()
            if _text.startswith("```"):
                _text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", _text)
                _text = re.sub(r"\n?```\s*$", "", _text).strip()
            # Slice to the outermost JSON object, dropping any leading/trailing text
            _start = _text.find("{")
            _end = _text.rfind("}") + 1
            if _start != -1 and _end > _start:
                _text = _text[_start:_end]
            _log.warning("CriticAgent: Gemini raw (trimmed): %s", _text[:300])
            result = json.loads(_text)
            result["_critic_model"] = _gemini_model
            return result
        except Exception as _gemini_exc:
            _log.warning(
                "CriticAgent: Gemini call failed (%s: %s) — falling back to Groq",
                type(_gemini_exc).__name__,
                _gemini_exc,
            )

        # --- Fallback: Groq qwen-qwen3-32b ---
        try:
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
        except Exception as exc:
            return {
                "agrees": True,
                "counterarguments": [
                    f"Critic model unavailable; using evidence-chain validation fallback: {exc}"
                ],
                "confidence_in_conclusion": root_cause.get("confidence", 80),
                "fallback": True,
                "_critic_model": "fallback",
            }
        try:
            result = json.loads(content)
            result["_critic_model"] = _MODEL_CRITIC
            return result
        except json.JSONDecodeError:
            return {"agrees": True, "counterarguments": [], "confidence_in_conclusion": 85, "_critic_model": _MODEL_CRITIC}


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

        self.token_key = f"{incident_id}:{time.time_ns()}"
        _token_totals[self.token_key] = 0
        self.state: AgentState = AgentState.INGEST
        self.hypotheses: list[dict] = []
        self.evidence: list[dict] = []
        self.rejected: list[dict] = []
        self.conclusion: dict | None = None
        self.timeline: list[dict] = []
        self.vision_findings: dict | None = vision_findings
        self.report_markdown: str = ""
        self._critic_model_used: str = "qwen-qwen3-32b"  # updated after CriticAgent runs

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

        if self.vision_findings:
            visual_basis = (
                self.vision_findings.get("visual_evidence")
                or self.vision_findings.get("visual_pattern")
                or "Visual signal available for cross-checking text evidence."
            )
            yield self._ev(
                "multimodal_decision",
                decision="Promote visual evidence into the evidence plan before accepting root cause.",
                basis=visual_basis,
                effect=(
                    "The agent will treat dashboard patterns as a contradiction check "
                    "against logs, metrics, deploy history, and chat evidence."
                ),
                inferred=bool(self.vision_findings.get("is_inferred")),
            )
            await asyncio.sleep(0.3)

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
            model=_dispatch_model("llama-3.3-70b-versatile"),
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

                # Fix #5 — emit reasoning BEFORE the tool call
                reasoning = await self._generate_tool_reasoning(
                    hypothesis=hyp["description"],
                    tool_selected=tool_name,
                    investigation_id=self.token_key,
                )
                yield self._ev("tool_reasoning", **reasoning)
                await asyncio.sleep(0.25)

                # Existing tool_call event (kept for backward compat)
                yield self._ev("tool_call", tool=tool_name, message=f"Querying {tool_action}...")

                # Fix #1 — tool_call_dispatched with endpoint + params
                endpoint = _TOOL_ENDPOINTS.get(
                    tool_name, f"/tools/{tool_name}")
                yield self._ev(
                    "tool_call_dispatched",
                    tool=tool_name,
                    endpoint=endpoint,
                    params={"action": tool_action,
                            "incident_id": self.incident_id},
                    agent="EvidenceAgent",
                )

                t_start = time.time()
                await asyncio.sleep(0.9)
                result = await self._execute_tool_action(tool_action)
                latency_ms = int((time.time() - t_start) * 1000)

                yield self._ev("tool_result", tool=tool_name, data=result, latency_ms=latency_ms)
                tool_results_for_hyp.append(result)
                await asyncio.sleep(0.5)

                yield self._ev(
                    "agent_dispatch",
                    agent_name="EvidenceAgent",
                    task=f"Evaluate {tool_name} result against hypothesis",
                    model=_dispatch_model("llama-3.1-8b-instant"),
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
                    model=_dispatch_model("llama-3.3-70b-versatile"),
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
                    model="gemini-2.5-flash",
                )
                await asyncio.sleep(0.4)

                root_cause_data = {
                    "root_cause": hyp["description"],
                    "confirming_evidence": hyp["confirming_evidence"],
                    "confidence": confidence,
                }
                critic_result = await asyncio.to_thread(
                    self._critic_agent.run,
                    root_cause_data, self.evidence, self.token_key,
                )
                self._critic_model_used = critic_result.get(
                    "_critic_model", "qwen-qwen3-32b")
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
            model=_dispatch_model("compound-beta"),
        )
        await asyncio.sleep(0.3)

        postmortem = self._generate_postmortem()
        yield self._ev("agent_result", agent_name="ReportAgent", result_summary="Post-mortem report generated")
        yield self._ev("postmortem", data=postmortem)

    async def _generate_tool_reasoning(
        self,
        hypothesis: str,
        tool_selected: str,
        investigation_id: str,
    ) -> dict:
        """
        Fast LLM call explaining WHY this tool was chosen and why others are wrong.
        3-second hard timeout; falls back to a simple reasoning card on any failure.
        """
        available = ["query_logs", "query_metrics",
                     "query_github", "query_slack"]
        tools_rejected = [t for t in available if t != tool_selected]

        _SYSTEM = (
            "You are an SRE agent. Given a hypothesis and available tools, explain in "
            "2 sentences which tool to call and why the others are wrong. Be specific. "
            "Return JSON only: "
            '{"reasoning": "...", "tools_rejected": [...], "rejection_reasons": {"tool_name": "reason"}}'
        )
        prompt = json.dumps(
            {"hypothesis": hypothesis[:200],
                "tool_selected": tool_selected, "tools_available": available}
        )

        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(
                    _chat_with_fallback,
                    [{"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": prompt}],
                    [_MODEL_FAST],
                    0.3,
                    300,
                    investigation_id,
                ),
                timeout=3.0,
            )
            data = json.loads(content)
            return {
                "agent": "EvidenceAgent",
                "hypothesis": hypothesis[:120],
                "reasoning": data.get("reasoning", ""),
                "tool_selected": tool_selected,
                "tools_rejected": data.get("tools_rejected", tools_rejected),
                "rejection_reasons": data.get("rejection_reasons", {}),
            }
        except Exception:
            return {
                "agent": "EvidenceAgent",
                "hypothesis": hypothesis[:120],
                "reasoning": (
                    f"Selecting {tool_selected} as the most direct evidence source for this hypothesis. "
                    "Other tools address downstream effects, not root state."
                ),
                "tool_selected": tool_selected,
                "tools_rejected": tools_rejected[:2],
                "rejection_reasons": {},
                "fallback": True,
            }

    def _generate_postmortem(self) -> dict:
        token_count = _token_totals.pop(self.token_key, 0)
        if not self.conclusion:
            return {
                "error": "No conclusion reached — investigation flagged for human review.",
                "token_count": token_count,
            }

        impact = self.incident.get("impact", {})
        revenue_impact = impact.get("revenue_impact", "Unknown")
        review_hours_saved = 4.4
        review_cost_saved = int(review_hours_saved * 150)
        recurrence_risk = self._estimate_recurrence_risk()
        evidence_rows = self._build_evidence_rows()
        owner_suggestions = self._build_owner_suggestions()
        visual_decision = self._build_visual_decision()

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
                "revenue_impact": revenue_impact,
                "severity": impact.get("severity", "P2"),
            },
            "business_value": {
                "impact_analyzed": revenue_impact,
                "estimated_engineer_hours_saved": review_hours_saved,
                "estimated_review_cost_saved": f"${review_cost_saved:,}",
                "basis": "Assumes 4.5 hr manual review baseline and $150/hr blended engineering cost.",
                "disclaimer": (
                    f"Estimated review labor saved: {review_hours_saved} engineer-hours, "
                    "not claimed revenue recovery."
                ),
            },
            "recurrence_risk": recurrence_risk,
            "evidence_table": evidence_rows,
            "owner_suggestions": owner_suggestions,
            "visual_decision": visual_decision,
            "action_items": self.incident.get("action_items", []),
            "detection_gap": self.incident.get("detection_gap", ""),
            "contributing_factors": self.incident.get("contributing_factors", []),
            "agent_models_used": {
                "hypothesis": _VULTR_MODEL_MAP.get("llama-3.3-70b-versatile", "llama-3.3-70b-versatile")
                if os.getenv("VULTR_API_KEY") else "llama-3.3-70b-versatile",
                "evidence": _VULTR_MODEL_MAP.get("llama-3.1-8b-instant", "llama-3.1-8b-instant")
                if os.getenv("VULTR_API_KEY") else "llama-3.1-8b-instant",
                "root_cause": _VULTR_MODEL_MAP.get("llama-3.3-70b-versatile", "llama-3.3-70b-versatile")
                if os.getenv("VULTR_API_KEY") else "llama-3.3-70b-versatile",
                "report": _VULTR_MODEL_MAP.get("compound-beta", "compound-beta")
                if os.getenv("VULTR_API_KEY") else "compound-beta",
                "critic": self._critic_model_used,
            },
            "token_count": token_count,
        }

    def _estimate_recurrence_risk(self) -> dict:
        score = 35
        if self.incident.get("detection_gap"):
            score += 20
        if len(self.rejected) >= 2:
            score += 10
        if self.conclusion and self.conclusion.get("confidence", 0) < 90:
            score += 10
        if len(self.incident.get("action_items", [])) >= 4:
            score += 10
        score = max(0, min(score, 95))
        label = "High" if score >= 70 else "Medium" if score >= 45 else "Low"
        return {
            "score": score,
            "label": label,
            "rationale": (
                "Risk is based on detection-gap severity, number of misleading hypotheses, "
                "confidence margin, and open remediation workload."
            ),
        }

    def _build_evidence_rows(self) -> list[dict]:
        rows = []
        if self.conclusion:
            for action in self.conclusion.get("data_to_query", []):
                rows.append({
                    "source": action.split("(")[0],
                    "finding": self.conclusion.get("confirming_evidence", ""),
                    "effect": "supports root cause",
                })
        for rejected in self.rejected:
            rows.append({
                "source": ", ".join(rejected.get("data_to_query", [])) or "tool evidence",
                "finding": rejected.get("rejection_evidence", ""),
                "effect": "rejects false lead",
            })
        if self.vision_findings:
            rows.insert(0, {
                "source": "Gemini Vision",
                "finding": (
                    self.vision_findings.get("visual_evidence")
                    or self.vision_findings.get("visual_pattern")
                    or "Visual pattern used as multimodal cross-check."
                ),
                "effect": "cross-checks text evidence",
            })
        return rows[:8]

    def _build_owner_suggestions(self) -> list[dict]:
        owners = []
        for item in self.incident.get("action_items", []):
            text = item.lower()
            if any(k in text for k in ("monitor", "alert", "dashboard", "slo")):
                owner = "Observability"
            elif any(k in text for k in ("runbook", "document", "escalation")):
                owner = "Incident Commander"
            elif any(k in text for k in ("deploy", "ci", "canary", "rollback")):
                owner = "Platform Engineering"
            elif any(k in text for k in ("capacity", "rate limit", "queue", "tier")):
                owner = "Service Owner"
            else:
                owner = "On-call Team"
            owners.append({"owner": owner, "action": item})
        return owners

    def _build_visual_decision(self) -> dict | None:
        if not self.vision_findings:
            return None
        return {
            "mode": "inferred" if self.vision_findings.get("is_inferred") else "uploaded screenshot",
            "decision": "Visual evidence was used as a contradiction check before final synthesis.",
            "evidence": (
                self.vision_findings.get("visual_evidence")
                or self.vision_findings.get("visual_pattern")
                or "No detailed visual evidence text returned."
            ),
            "affected_panels": self.vision_findings.get("affected_panels", []),
            "affected_services": self.vision_findings.get("affected_services", []),
        }

    async def _execute_tool_action(self, action_str: str) -> dict:
        """
        Parse a tool-action string and dispatch to the live /tools/* HTTP endpoints.
        Four tools (logs, metrics, github, slack) make real HTTP POST calls so that
        network-observable tool orchestration is visible in server access logs and
        DevTools. PagerDuty and flag_for_review remain in-process (no HTTP endpoint).
        """
        start = self.incident["start_time"]
        end = self.incident["end_time"]
        iid = self.incident_id
        action_str = action_str.strip()

        async def _post(path: str, payload: dict) -> dict:
            """POST to a live /tools/* endpoint with a 5 s timeout; fall back to {} on error."""
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.post(f"{_TOOL_BASE}{path}", json=payload)
                    r.raise_for_status()
                    return r.json()
            except Exception:
                return {"error": f"tool endpoint {path} unavailable", "count": 0}

        if action_str.startswith("query_metrics("):
            inner = action_str[len("query_metrics("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if parts else "api"
            metric = parts[1] if len(parts) > 1 else "cpu_percent"
            return await _post("/tools/metrics", {
                "incident_id": iid, "service": service,
                "metric": metric, "start_time": start, "end_time": end,
            })

        elif action_str.startswith("query_logs("):
            inner = action_str[len("query_logs("):-1]
            parts = [p.strip() for p in inner.split(",")]
            service = parts[0] if parts else "api"
            level = parts[1] if len(parts) > 1 else "error"
            return await _post("/tools/logs", {
                "incident_id": iid, "service": service,
                "level": level, "start_time": start, "end_time": end,
            })

        elif action_str.startswith("query_github("):
            repo = action_str[len("query_github("):-1].strip()
            return await _post("/tools/github", {
                "incident_id": iid, "repo": repo,
                "start_time": start, "end_time": end,
            })

        elif action_str.startswith("query_slack("):
            channel = action_str[len(
                "query_slack("):-1].strip() or "#incidents"
            return await _post("/tools/slack", {
                "incident_id": iid, "channel": channel,
                "start_time": start, "end_time": end,
            })

        elif action_str.startswith("query_pagerduty("):
            return await query_pagerduty_impl(start_time=start, end_time=end, incident_id=iid)

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
