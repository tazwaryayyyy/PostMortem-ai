"""
PostMortem.ai — FastAPI backend
================================
Run locally:   uvicorn main:app --reload --port 8000
Run on Vultr:  uvicorn main:app --host 0.0.0.0 --port 80
"""

import asyncio
import json
import random
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import PostMortemAgent
from mock_apis import INCIDENTS, load_incidents

load_dotenv()

# ── Re-load incidents in case the module was imported before files existed ──
load_incidents()

app = FastAPI(title="PostMortem.ai", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static UI
app.mount("/static", StaticFiles(directory="ui"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse("ui/index.html")


@app.get("/incidents")
async def list_incidents():
    """Return metadata for all available incidents."""
    return {
        "incidents": [
            {
                "id": "incident_a",
                "title": "API Returning 500 Errors — OAuth Users",
                "difficulty": "medium",
                "duration": "47 min",
                "impact": "$38K",
                "severity": "P1",
            },
            {
                "id": "incident_b",
                "title": "Database Crash at 2AM",
                "difficulty": "hard",
                "duration": "31 min",
                "impact": "$21K",
                "severity": "P1",
            },
            {
                "id": "incident_c",
                "title": "Silent 504 Errors — No Obvious Cause",
                "difficulty": "expert",
                "duration": "68 min",
                "impact": "$94K",
                "severity": "P1",
            },
        ]
    }


@app.get("/incidents/random")
async def random_incident():
    """Return a random incident ID (used by the Load Random button)."""
    choices = ["incident_a", "incident_b", "incident_c"]
    return {"id": random.choice(choices)}


@app.get("/investigate")
async def investigate(incident_id: str):
    """
    Stream investigation reasoning via SSE (Server-Sent Events).

    Each event is a JSON object with a ``type`` field.
    The stream ends with ``{"type": "done"}``.
    """
    if incident_id not in INCIDENTS:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")

    async def event_stream():
        try:
            agent = PostMortemAgent(incident_id)
            async for event in agent.run_streaming():
                # Yield SSE-formatted line
                yield f"data: {json.dumps(event)}\n\n"
                # Flush — important for streaming proxies
                await asyncio.sleep(0)
        except Exception as exc:
            error_event = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(error_event)}\n\n"
        finally:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "incidents_loaded": len(INCIDENTS)}


# ---------------------------------------------------------------------------
# Dev entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
