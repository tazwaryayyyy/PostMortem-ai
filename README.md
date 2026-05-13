# PostMortem.ai v2.0

> "When your systems go down, PostMortem.ai investigates the root cause, admits when it's wrong, and delivers a complete post-mortem — before your engineer finishes their first Slack message."

Built for **AI Agent Olympics 2026 — Milan AI Week**.

> **Live Demo:** See deployment instructions in DEPLOYMENT.md
> **Demo Video:** See `/docs` for screenshots and walk-through
> **Judges:** Use the **⚡ Judge Mode** button for a fully-automated 6-step guided demo — or manually select *incident_e* (★ Recommended) and click "Simulate Webhook" for a $312K autonomous investigation.

## For Judges — Quick Start (60 seconds)

1. `cp .env.example .env` — fill in your Groq + Google API keys (free tier works)
2. `pip install -r requirements.txt`
3. `python main.py`
4. Open `http://localhost:8000` — click **⚡ Judge Mode** and watch the 5-agent pipeline reason live

### What to watch for
- **Stats strip** — real incident financial data loaded from JSON files on startup
- **Reasoning Trace panel** — EvidenceAgent explains WHY it picks each tool (🧠 cards), then shows the HTTP dispatch (🔧 cards with endpoint + params + latency)
- **Agent Debate** — CriticAgent (qwen3-32b) challenges the RootCauseAgent conclusion (⚔️ card, amber border)
- **Hypothesis rejection** — red-bordered cards show which leads the agent ruled out and why
- **Export Markdown** — generates a complete structured post-mortem report

---

## Architecture

```
postmortem-ai/
├── main.py               # FastAPI app — SSE streaming + all endpoints
├── agent.py              # Multi-agent coordinator (5 specialist agents)
├── vision_agent.py       # Gemini 2.5 Flash — screenshot visual analysis
├── incident_generator.py # AI-generated incident scenarios on demand
├── investigation_store.py# SQLite WAL-mode persistence layer
├── prompts.py            # LLM prompt templates
├── mock_apis.py          # Tool implementations (dynamic incident loader)
├── incidents/
│   ├── incident_a.json   # API 500 Errors — OAuth deploy regression
│   ├── incident_b.json   # Database Crash at 2AM
│   ├── incident_c.json   # Silent 504 Errors — service mesh
│   ├── incident_d.json   # ML Memory Leak — CUDA OOM ($156K) ★ Expert
│   └── incident_e.json   # Silent Payment Cascade — Stripe TLS ($312K) ★ Expert
├── ui/
│   └── index.html        # 5-panel dashboard (vanilla JS, no build step)
├── Dockerfile            # Multi-stage, non-root image
├── docker-compose.yml    # App + nginx reverse proxy
├── nginx.conf            # SSE-tuned nginx configuration
├── vultr-deploy.sh       # One-command Vultr VM deployment
└── requirements.txt
```

---

## Agent Stack

| Agent | Model | Role |
|-------|-------|------|
| `HypothesisAgent` | llama-3.3-70b-versatile | Generate 3 investigation hypotheses |
| `EvidenceAgent` | llama-3.1-8b-instant | Evaluate evidence for each hypothesis |
| `RootCauseAgent` | llama-3.3-70b-versatile | Synthesize confirmed root cause |
| `CriticAgent` | qwen-qwen3-32b | Challenge the root cause (red-team) |
| `ReportAgent` | compound-beta | Generate full post-mortem markdown |
| `VisionAgent` | gemini-2.5-flash | Analyze dashboard screenshots |

All Groq models use a silent fallback chain: `llama-3.3-70b-versatile → llama-4-scout-17b → llama-3.1-8b-instant`.

---

## Setup

### 1. Install

```bash
git clone <your-repo>
cd postmortem-ai
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add:
# GROQ_API_KEY=your_groq_key     (required — all text agents)
# GOOGLE_API_KEY=your_gemini_key (optional — visual analysis only)
```

### 3. Run locally

```bash
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

---

## Deploy on Vultr

### Option A — Docker Compose (recommended)

```bash
# 1. Provision a Vultr Cloud Compute instance (Ubuntu 22.04, 2 vCPU / 4 GB RAM)

# 2. SSH into the instance
ssh root@YOUR_VULTR_IP

# 3. Clone the repo
git clone <your-repo> /opt/postmortem-ai
cd /opt/postmortem-ai

# 4. Create .env
cp .env.example .env
nano .env   # fill in GROQ_API_KEY and GOOGLE_API_KEY

# 5. Deploy
bash vultr-deploy.sh

# App is now live at http://YOUR_VULTR_IP
# Health check: curl http://YOUR_VULTR_IP/health
```

### Option B — Manual

```bash
# Install Docker
apt-get update && apt-get install -y docker.io docker-compose-plugin

# Build and start
docker compose up --build -d

# View logs
docker compose logs -f postmortem-ai
```

### Environment variables for production

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | *required* | Groq API key for all text agents |
| `GOOGLE_API_KEY` | *optional* | Gemini API key for VisionAgent |
| `MAX_CONCURRENT_INVESTIGATIONS` | `10` | Semaphore limit on concurrent streams |
| `DB_PATH` | `investigations.db` | SQLite database file path |
| `VULTR_DEPLOYMENT` | `false` | Set `true` on Vultr to enable instance metadata in `/health` |
| `ENVIRONMENT` | `development` | `development` or `production` |

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard UI |
| `GET` | `/incidents` | List all incident metadata |
| `GET` | `/incidents/random` | Random incident ID |
| `GET` | `/investigate?incident_id=X` | Stream investigation via SSE |
| `POST` | `/upload-screenshot` | Upload PNG/JPG for Gemini visual analysis |
| `POST` | `/generate-incident` | AI-generate a new incident scenario |
| `GET` | `/history` | Last 20 investigation outcomes (SQLite) |
| `GET` | `/metrics` | Aggregate investigation statistics |
| `POST` | `/webhook/pagerduty` | PagerDuty webhook integration |
| `GET` | `/health` | Health check (Vultr-aware) |

---

## Demo Flow

1. Select **Incident E** (Payment Cascade — most dramatic) or press **5**
2. Drop a dashboard screenshot onto the Signal Feed for **Gemini visual analysis**
3. Click **Investigate** — watch all 5 panels animate live
4. Agent dispatches stream in real-time in the **Agent Activity** panel
5. 3 hypotheses appear: 2 are rejected with specific evidence 🔴, 1 confirmed 🟢
6. **Confidence meter** climbs to final root cause confidence %
7. **Governance gate** appears — click Approve to generate the report
8. Export the report as Markdown with one click
9. Click **+ Generate** to demonstrate infinite scenario coverage with AI-generated incidents
10. Press **Space** for a random incident to prove it's not scripted

### All Five Incidents

| ID | Title | Difficulty | Impact |
|----|-------|-----------|--------|
| `incident_a` | API 500 Errors — OAuth Deploy | Medium | $38K |
| `incident_b` | Database Crash at 2AM | Hard | $21K |
| `incident_c` | Silent 504 Errors — Service Mesh | Expert | $94K |
| `incident_d` | ML Memory Leak — CUDA OOM | Expert | $156K |
| `incident_e` | Silent Payment Cascade — Stripe TLS | Expert | $312K |

---

## ROI Numbers

| Metric | Manual | PostMortem.ai |
|--------|--------|---------------|
| Time to root cause | 2–4 hours | ~93 seconds |
| Engineer-hours per incident | 4–6 hours | 0.1 hours |
| Cost per incident @ $150/hr | $600–$900 | $15 |
| Monthly cost (4 incidents) | $2,400–$3,600 | $60 |
| Annual savings (50 incidents/yr) | baseline | ~$43,000 |
