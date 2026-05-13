# PostMortem.ai ⚡

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Powered by Groq](https://img.shields.io/badge/Powered%20by-Groq-f55036?logo=thunderbird&logoColor=white)](https://groq.com)
[![Vision: Gemini](https://img.shields.io/badge/Vision-Gemini%202.5%20Flash-4285f4?logo=google&logoColor=white)](https://ai.google.dev)
[![Models: 5 Agents](https://img.shields.io/badge/Agents-5%20Specialist%20LLMs-8b5cf6)](agent.py)

> The average incident sits uninvestigated for 2–4 hours.
> Half that time is engineers asking "what does this break?"
> PostMortem.ai answers in 93 seconds.

PostMortem.ai runs a production incident through 5 specialist LLMs in 93 seconds and produces a structured post-mortem, including a live argument between two agents using different model architectures.

The system is designed to be wrong first. The first hypothesis is always a deliberate red herring -- something plausible that the evidence chain must disprove before moving on. The CriticAgent runs on qwen3-32b, a different architecture from the rest of the pipeline, so it has no shared reasoning context to agree with. It only sees the conclusion and the evidence, and it is prompted to find holes.

## Quick Start

1. Copy `.env.example` to `.env` and add your Groq and Google API keys (free tier works)
2. Run `pip install -r requirements.txt`
3. Run `python main.py`
4. Open `http://localhost:8000`

Click Judge Mode. Everything runs automatically.

### What to watch for

- `Reasoning Trace panel` -- the agent explains which tool it picked and why the others are wrong, before each call
- `Dispatch cards` -- each tool call shows the actual HTTP endpoint, parameters, and response latency in milliseconds
- `Agent Debate card` -- CriticAgent challenges the root cause conclusion; watch whether it agrees or raises a counterargument
- `Hypothesis rejection cards` -- red-bordered cards show each dead end the agent ruled out, with the specific evidence that killed it

## How It Works

```
postmortem-ai/
+-- main.py               # FastAPI app -- SSE streaming + all endpoints
+-- agent.py              # Multi-agent coordinator (5 specialist agents)
+-- live_tools.py         # Real HTTP tool endpoints (/tools/*)
+-- vision_agent.py       # Gemini 2.5 Flash -- screenshot visual analysis
+-- incident_generator.py # AI-generated incident scenarios on demand
+-- investigation_store.py# SQLite WAL-mode persistence layer
+-- prompts.py            # LLM prompt templates
+-- mock_apis.py          # Tool implementations (httpx + fallback layer)
+-- incidents/
�   +-- incident_a.json   # API 500 Errors -- OAuth deploy regression
�   +-- incident_b.json   # Database Crash at 2AM
�   +-- incident_c.json   # Silent 504 Errors -- service mesh
�   +-- incident_d.json   # ML Memory Leak -- CUDA OOM ($156K)
�   +-- incident_e.json   # Silent Payment Cascade -- Stripe TLS ($312K)
+-- ui/
�   +-- index.html        # 5-panel dashboard (vanilla JS, no build step)
+-- Dockerfile            # Multi-stage, non-root image
+-- docker-compose.yml    # App + nginx reverse proxy
+-- nginx.conf            # SSE-tuned nginx configuration
+-- vultr-deploy.sh       # One-command Vultr VM deployment
+-- requirements.txt
```

EvidenceAgent runs on `llama-3.1-8b-instant` because it executes in a tight loop -- one call per tool result per hypothesis -- so speed matters more than reasoning depth at that stage. HypothesisAgent and RootCauseAgent use `llama-3.3-70b-versatile` because hypothesis generation and synthesis are the two steps where reasoning quality directly affects whether the agent reaches the correct conclusion. ReportAgent uses `compound-beta` because it specializes in structured output, which produces cleaner markdown sections than a general-purpose model. CriticAgent uses `qwen-qwen3-32b` specifically because it is a different model family: using the same model to critique its own output would produce agreement by default.

| Agent           | Model                    | Role                                              |
|-----------------|--------------------------|---------------------------------------------------|
| HypothesisAgent | llama-3.3-70b-versatile  | Generate and rank hypotheses from signals         |
| EvidenceAgent   | llama-3.1-8b-instant     | Evaluate each tool result against a hypothesis    |
| RootCauseAgent  | llama-3.3-70b-versatile  | Synthesize confirmed evidence into root cause     |
| CriticAgent     | qwen-qwen3-32b           | Challenge the root cause conclusion independently |
| ReportAgent     | compound-beta            | Write the final structured post-mortem            |
| VisionAgent     | gemini-2.5-flash         | Analyze screenshots or infer visual patterns      |

All Groq models use a silent fallback chain: `llama-3.3-70b-versatile` then `llama-4-scout-17b` then `llama-3.1-8b-instant`.

## The Numbers

| Metric                        | Manual        | PostMortem.ai |
|-------------------------------|---------------|---------------|
| Time to root cause            | 2-4 hours     | ~93 seconds   |
| Engineer-hours per incident   | 4-6 hours     | 0.1 hours     |
| Cost per incident @ $150/hr   | $600-$900     | $15           |
| Monthly cost (4 incidents)    | $2,400-$3,600 | $60           |
| Annual savings (50/yr)        | baseline      | ~$43,000      |

These figures use the Atlassian 2024 State of Incidents report baseline of 4.5 hr mean MTTR.

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
# GROQ_API_KEY=your_groq_key     (required -- all text agents)
# GOOGLE_API_KEY=your_gemini_key (optional -- visual analysis only)
```

### 3. Run locally

```bash
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`

## Deploy on Vultr

### Option A -- Docker Compose (recommended)

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

### Option B -- Manual

```bash
# Install Docker
apt-get update && apt-get install -y docker.io docker-compose-plugin

# Build and start
docker compose up --build -d

# View logs
docker compose logs -f postmortem-ai
```

### Environment variables

| Variable                        | Default                  | Description                                    |
|---------------------------------|--------------------------|------------------------------------------------|
| `GROQ_API_KEY`                  | required                 | Groq API key for all text agents               |
| `GOOGLE_API_KEY`                | optional                 | Gemini API key for VisionAgent                 |
| `MAX_CONCURRENT_INVESTIGATIONS` | `10`                     | Semaphore limit on concurrent streams          |
| `DB_PATH`                       | `investigations.db`      | SQLite database file path                      |
| `TOOL_API_BASE_URL`             | `http://localhost:8000`  | Base URL for live tool HTTP endpoints          |
| `ENVIRONMENT`                   | `development`            | Set to `production` on deployed instances      |

## Endpoints

| Method | Path                          | Description                                    |
|--------|-------------------------------|------------------------------------------------|
| GET    | `/`                           | Dashboard UI                                   |
| GET    | `/incidents`                  | List all incident metadata                     |
| GET    | `/incidents/random`           | Random incident ID                             |
| GET    | `/investigate?incident_id=X`  | Stream investigation via SSE                   |
| POST   | `/tools/logs`                 | Live log query endpoint                        |
| POST   | `/tools/metrics`              | Live metrics query endpoint                    |
| POST   | `/tools/github`               | Live GitHub commits endpoint                   |
| POST   | `/tools/slack`                | Live Slack messages endpoint                   |
| POST   | `/upload-screenshot`          | Upload PNG/JPG for Gemini visual analysis      |
| POST   | `/generate-incident`          | AI-generate a new incident scenario            |
| GET    | `/history`                    | Last 20 investigation outcomes from SQLite     |
| GET    | `/metrics`                    | Aggregate stats including financial impact     |
| POST   | `/webhook/pagerduty`          | PagerDuty webhook integration                  |
| GET    | `/health`                     | Health check                                   |

## Incidents

| ID           | Title                                          | Impact |
|--------------|------------------------------------------------|--------|
| `incident_a` | API 500 Errors -- OAuth Deploy Regression      | $38K   |
| `incident_b` | Database Crash at 2AM                          | $21K   |
| `incident_c` | Silent 504 Errors -- Service Mesh              | $94K   |
| `incident_d` | ML Memory Leak -- CUDA OOM                     | $156K  |
| `incident_e` | Silent Payment Cascade -- Stripe TLS Rotation  | $312K  |

`incident_e` is the recommended demo. The $312K Stripe TLS cascade triggers the CriticAgent debate most reliably.

## License

MIT License. Copyright (c) 2026 tazwaryayyyy. See [LICENSE](LICENSE)
