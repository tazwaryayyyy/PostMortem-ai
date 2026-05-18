"""
PostMortem.ai — Vision Agent
=============================
Uses Google Gemini 2.5 Flash (multimodal) to analyze screenshot uploads of
dashboards, Grafana charts, error pages, and runbooks.

Gemini is used EXCLUSIVELY for image analysis. All text reasoning stays on Groq.
"""

import base64
import json
import os
from typing import Union

_TEXT_INFERENCE_PROMPT = """You are a senior SRE analyzing a production incident.
Given the following raw incident signals, describe what a Grafana dashboard
would visually show during this incident: which panels would be spiking,
what the anomaly pattern looks like, and where the visual root cause is visible.

Signals:
{signal_summary}

Return ONLY valid JSON, no prose, no markdown fences:
{{
  "visual_pattern": "2-3 sentence description of what the dashboard shows",
  "anomaly_timestamp": "HH:MM if inferable from signals, else null",
  "affected_panels": ["Panel names that would be red/spiking"],
  "visual_evidence": "One paragraph describing the visual anomaly as if seen on Grafana",
  "anomalies": [
    {{"timestamp": "", "description": "visual anomaly description", "metric_value": null, "severity": "high"}}
  ],
  "affected_services": ["service names from signals"],
  "severity": "critical | high | medium | low"
}}"""


_ANALYSIS_PROMPT = """You are a site reliability engineer analyzing a screenshot from a production incident.
Examine this image carefully and extract structured incident signals.

Return ONLY valid JSON matching this exact schema — no prose, no markdown fences:
{
  "anomalies": [
    {
      "timestamp": "HH:MM or ISO8601 if visible, else ''",
      "description": "one-sentence technical description of the anomaly",
      "metric_value": "numeric value if visible, else null",
      "severity": "critical | high | medium | low"
    }
  ],
  "affected_services": ["service name strings extracted from labels/legends"],
  "severity": "critical | high | medium | low",
  "visual_evidence": "2-3 sentence summary of what the chart/screenshot shows",
  "chart_type": "time_series | heatmap | log_view | dashboard | error_page | unknown",
  "time_range_visible": "time range shown in chart if readable, else null"
}

Be specific. If you see a spike at 14:05, say '14:05 spike'. If you see OOM errors, name them.
If the image contains no useful signal (e.g. a blank screen), return anomalies as empty array.
"""


class VisionAgent:
    """
    Analyzes screenshot images using Gemini 2.5 Flash multimodal API.

    Accepts raw bytes (PNG/JPG) or a base64-encoded string.
    Returns structured JSON with anomalies, affected services, and visual evidence.
    """

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        """Lazy-init Google GenAI client — reads GOOGLE_API_KEY from env."""
        if self._client is None:
            try:
                from google import genai  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "google-genai package not installed. "
                    "Run: pip install google-genai"
                ) from exc
            api_key = os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "GOOGLE_API_KEY environment variable is not set.")
            self._client = genai.Client(api_key=api_key)
        return self._client

    def analyze(self, image_data: Union[bytes, str], mime_type: str = "image/png") -> dict:
        """
        Analyze an image and return structured incident signals.

        Parameters
        ----------
        image_data : bytes or str
            Raw image bytes or a base64-encoded string.
        mime_type : str
            MIME type of the image (image/png or image/jpeg).

        Returns
        -------
        dict
            Structured analysis with keys: anomalies, affected_services,
            severity, visual_evidence, chart_type, time_range_visible.
        """
        if isinstance(image_data, str):
            # Assume base64 — decode to bytes
            image_bytes = base64.b64decode(image_data)
        else:
            image_bytes = image_data

        from google.genai import types  # type: ignore

        client = self._get_client()

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                _ANALYSIS_PROMPT,
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )

        raw_text = response.text.strip()

        # Strip markdown fences and slice to outermost JSON object
        if raw_text.startswith("```"):
            import re as _re
            raw_text = _re.sub(r"^```[a-zA-Z]*\s*\n?", "", raw_text)
            raw_text = _re.sub(r"\n?```\s*$", "", raw_text).strip()
        _s, _e = raw_text.find("{"), raw_text.rfind("}") + 1
        if _s != -1 and _e > _s:
            raw_text = raw_text[_s:_e]
        import re as _re2
        raw_text = _re2.sub(r'\bNone\b', 'null', raw_text)
        raw_text = _re2.sub(r'\bTrue\b', 'true', raw_text)
        raw_text = _re2.sub(r'\bFalse\b', 'false', raw_text)

        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            # Graceful degradation — return partial result
            result = {
                "anomalies": [],
                "affected_services": [],
                "severity": "unknown",
                "visual_evidence": "Visual analysis unavailable — could not parse model response.",
                "chart_type": "unknown",
                "time_range_visible": None,
                "parse_error": "Gemini response was not valid JSON",
            }

        return result

    def analyze_base64(self, b64_string: str, mime_type: str = "image/png") -> dict:
        """Convenience wrapper — accepts base64 string directly."""
        return self.analyze(b64_string, mime_type=mime_type)

    def text_inference(self, signal_summary: str) -> dict:
        """
        Call Gemini with a text-only prompt to infer visual dashboard patterns.

        Used in every investigation (even without a real screenshot) so that
        Gemini is demonstrably active on all runs. Labelled as
        '\U0001f916 Gemini Visual Inference' in the UI.
        """
        from google.genai import types  # type: ignore

        client = self._get_client()
        prompt = _TEXT_INFERENCE_PROMPT.format(
            signal_summary=signal_summary[:2000])

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=800,
            ),
        )

        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            import re as _re
            raw_text = _re.sub(r"^```[a-zA-Z]*\s*\n?", "", raw_text)
            raw_text = _re.sub(r"\n?```\s*$", "", raw_text).strip()
        _s, _e = raw_text.find("{"), raw_text.rfind("}") + 1
        if _s != -1 and _e > _s:
            raw_text = raw_text[_s:_e]
        import re as _re2
        raw_text = _re2.sub(r'\bNone\b', 'null', raw_text)
        raw_text = _re2.sub(r'\bTrue\b', 'true', raw_text)
        raw_text = _re2.sub(r'\bFalse\b', 'false', raw_text)

        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            result = {
                "visual_pattern": "Visual inference unavailable — could not parse model response.",
                "anomaly_timestamp": None,
                "affected_panels": [],
                "visual_evidence": "Visual inference unavailable — could not parse model response.",
                "anomalies": [],
                "affected_services": [],
                "severity": "unknown",
            }

        # Flag: synthetic inference, not real screenshot
        result["is_inferred"] = True
        result.setdefault("anomalies", [])
        result.setdefault("affected_services", [])
        result.setdefault("affected_panels", [])
        return result


# Module-level singleton for reuse across requests
_vision_agent: VisionAgent | None = None


def get_vision_agent() -> VisionAgent:
    """Return the module-level VisionAgent singleton."""
    global _vision_agent
    if _vision_agent is None:
        _vision_agent = VisionAgent()
    return _vision_agent
