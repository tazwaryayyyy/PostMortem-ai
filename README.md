# PostMortem.ai

> "When your systems go down, PostMortem.ai investigates the root cause, admits when it's wrong, and delivers a complete post-mortem — before your engineer finishes their first Slack message."

Built for **AI Agent Olympics 2026 — Milan AI Week**.

---

## Architecture

```
postmortem-ai/
├── main.py            # FastAPI app + SSE streaming endpoint
├── agent.py           # PostMortemAgent state machine
├── prompts.py         # LLM prompt templates
├── mock_apis.py       # Mock data tool implementations
├── incidents/
│   ├── incident_a.json  # Deploy regression (primary demo)
│   ├── incident_b.json  # Cache cascade (backup)
│   └── incident_c.json  # Silent dependency (expert)
├── ui/
│   └── index.html     # 4-panel dashboard (vanilla JS, no build step)
└── requirements.txt
```

---

## Setup

### 1. Clone and install

```bash
git clone <your-repo>
cd postmortem-ai
pip install -r requirements.txt
```

### 2. Set your Groq API key

```bash
cp .env.example .env
# then edit .env and add:
# GROQ_API_KEY=your_key_here
```

Or export directly:

```bash
export GROQ_API_KEY=your_key_here
```

### 3. Run locally

```bash
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

---

## Vultr Deployment

```bash
# On Vultr VM (Ubuntu 22.04+)
git clone <your-repo>
cd postmortem-ai
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key" > .env

# Run on port 80 (show Vultr dashboard in demo closing)
uvicorn main:app --host 0.0.0.0 --port 80
```

---

## Demo Flow

1. Select **Incident A** (OAuth Deploy Regression) — primary demo
2. Click **Investigate** — watch all 4 panels animate live
3. Agent forms 3 hypotheses, rejects H1 and H2 with specific evidence
4. Governance gate appears — click Approve
5. Post-mortem populates with root cause, impact, action items
6. Click **⟳ Random** to prove it's not scripted

### The Three Incidents

| ID | Title | Difficulty | Impact |
|----|-------|-----------|--------|
| `incident_a` | API 500 Errors — OAuth Users | Medium | $38K |
| `incident_b` | Database Crash at 2AM | Hard | $21K |
| `incident_c` | Silent 504 Errors | Expert | $94K |

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the dashboard UI |
| `GET` | `/incidents` | List all incident metadata |
| `GET` | `/incidents/random` | Pick a random incident ID |
| `GET` | `/investigate?incident_id=X` | Stream investigation via SSE |
| `GET` | `/health` | Health check |

---

## ROI Numbers

| Metric | Manual | PostMortem.ai |
|--------|--------|---------------|
| Time to root cause | 2–4 hours | ~93 seconds |
| Engineer-hours per incident | 4–6 hours | 0.1 hours |
| Cost per incident @ $150/hr | $600–$900 | $15 |
| Monthly cost (4 incidents) | $2,400–$3,600 | $60 |
