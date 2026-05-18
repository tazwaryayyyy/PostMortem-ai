# PostMortem.ai

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Powered by Vultr](https://img.shields.io/badge/Inference-Vultr%20Serverless-009BDE?logo=vultr&logoColor=white)](https://www.vultr.com/products/cloud-inference/)
[![Powered by Groq](https://img.shields.io/badge/Fallback-Groq-f55036?logo=thunderbird&logoColor=white)](https://groq.com)
[![Vision: Gemini](https://img.shields.io/badge/Vision-Gemini-4285f4?logo=google&logoColor=white)](https://ai.google.dev)
[![Models: 5 Agents](https://img.shields.io/badge/Agents-5%20Specialist%20LLMs-8b5cf6)](agent.py)

> The average incident sits uninvestigated for 2–4 hours.
> Half that time is engineers asking "what does this break?"
> PostMortem.ai answers in 93 seconds.

PostMortem.ai runs a production incident through six specialist agents in about 93 seconds and produces a structured postmortem, including a live argument between two agents using different model architectures.

The system is designed to be wrong first. The first hypothesis is always a deliberate red herring -- something plausible that the evidence chain must disprove before moving on. The CriticAgent runs on **Gemini** (Google), a completely different provider from the rest of the pipeline (Vultr Serverless Inference / Groq), so it has no shared reasoning context to agree with. It only sees the conclusion and the evidence, and it is prompted to find holes.

## Grand Prize Story

> Most incident tools tell teams what is broken.
> PostMortem.ai tells them what is misleading.

PostMortem.ai is an autonomous incident commander for SRE teams. It investigates a production incident across logs, metrics, deploy history, Slack, PagerDuty, and Gemini visual evidence; rejects plausible-but-wrong root causes; challenges its own conclusion with an independent CriticAgent; and produces an auditable postmortem with owners, recurrence risk, action items, and estimated review savings.

For SRE managers, PostMortem.ai reduces incident review time from hours to minutes by autonomously testing root-cause hypotheses across logs, metrics, deploy history, chat, alerts, and visual dashboard evidence.

PostMortem.ai targets the **Agentic Workflows**, **Collaborative Systems**, **Enterprise Utility**, and **Multimodal Intelligence** tracks:

- **Agentic workflow:** plans an investigation, selects tools, calls APIs, evaluates evidence, and changes course when confidence is low.
- **Collaborative system:** HypothesisAgent, EvidenceAgent, RootCauseAgent, CriticAgent, ReportAgent, and VisionAgent coordinate through streamed state.
- **Enterprise utility:** replaces repetitive incident-review labor and produces buyer-ready artifacts for SRE managers.
- **Multimodal intelligence:** Gemini 2.5 Flash serves dual roles — it turns screenshots or inferred dashboard patterns into contradiction checks against text evidence, **and** acts as the adversarial CriticAgent challenging the Vultr/Groq-based root cause conclusion from a completely independent inference stack.
- **Vultr production deployment:** FastAPI + Docker Compose on a Vultr VM, nginx reverse proxy, health endpoint, and persistent SQLite history.

## Quick Start

1. Copy `.env.example` to `.env` and add your Groq and Google API keys (free tier works)
2. Run `pip install -r requirements.txt`
3. Run `python main.py`
4. Open `http://localhost:8000`

Click Judge Mode. Everything runs automatically.

### What to watch for

- `Reasoning Trace panel` -- the agent explains which tool it picked and why the others are wrong, before each call
- `Autonomy Ledger` -- live proof of the current plan, selected tool, rejected alternatives, and confidence movement
- `Gemini changed the evidence plan` -- visual or inferred dashboard evidence is promoted into the investigation before root-cause synthesis
- `Dispatch cards` -- each tool call shows the actual HTTP endpoint, parameters, and response latency in milliseconds
- `Agent Debate card` -- CriticAgent challenges the root cause conclusion; watch whether it agrees or raises a counterargument
- `Hypothesis rejection cards` -- red-bordered cards show each dead end the agent ruled out, with the specific evidence that killed it
- `Postmortem report` -- includes evidence table, owner suggestions, recurrence risk, action items, detection gap, and defensible savings assumptions

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

EvidenceAgent runs on `llama-3.1-8b-instant` because it executes in a tight loop -- one call per tool result per hypothesis -- so speed matters more than reasoning depth at that stage. HypothesisAgent and RootCauseAgent use `llama-3.3-70b-versatile` because hypothesis generation and synthesis are the two steps where reasoning quality directly affects whether the agent reaches the correct conclusion. ReportAgent uses `compound-beta` because it specializes in structured output, which produces cleaner markdown sections than a general-purpose model. CriticAgent uses **Google Gemini** (tries `gemini-2.0-flash` first, then `gemini-2.5-flash`, falls back to Groq `qwen-qwen3-32b`) specifically because it is a different provider and model family from the rest of the pipeline: an adversarial critic from a completely independent inference stack cannot share implicit reasoning biases with the chain it is challenging. All Llama-based agents use **Vultr Serverless Inference** as primary (when `VULTR_API_KEY` is set), with Groq as fallback.

| Agent           | Model                                              | Role                                              |
|-----------------|-----------------------------------------------------|---------------------------------------------------|
| HypothesisAgent | llama-3.1-70b-instruct-fp8 (Vultr) / llama-3.3-70b-versatile (Groq) | Generate and rank hypotheses from signals |
| EvidenceAgent   | llama-3.1-8b-instruct-fp8 (Vultr) / llama-3.1-8b-instant (Groq) | Evaluate each tool result against a hypothesis |
| RootCauseAgent  | llama-3.1-70b-instruct-fp8 (Vultr) / llama-3.3-70b-versatile (Groq) | Synthesize confirmed evidence into root cause |
| CriticAgent     | gemini-2.0-flash → gemini-2.5-flash → qwen-qwen3-32b (Groq fallback) | Challenge the root cause conclusion independently |
| ReportAgent     | llama-3.1-70b-instruct-fp8 (Vultr) / compound-beta (Groq) | Write the final structured post-mortem |
| VisionAgent     | gemini-2.5-flash                                    | Analyze screenshots or infer visual patterns      |

Vultr Serverless Inference is the primary provider for all Llama-based agents when `VULTR_API_KEY` is set. Groq is the automatic fallback. The Groq fallback chain: `llama-3.3-70b-versatile` → `llama-4-scout-17b` → `llama-3.1-8b-instant`.

## The Numbers

| Metric                        | Manual        | PostMortem.ai |
|-------------------------------|---------------|---------------|
| Time to root cause            | 2-4 hours     | ~93 seconds   |
| Engineer-hours per incident   | 4-6 hours     | 0.1 hours     |
| Cost per incident @ $150/hr   | $600-$900     | $15           |
| Monthly cost (4 incidents)    | $2,400-$3,600 | $60           |
| Annual savings (50/yr)        | baseline      | ~$43,000      |

The dashboard labels incident dollars as **impact analyzed**, not recovered revenue. The savings estimate is limited to avoided review labor: 4.5 hr manual review baseline, 0.1 hr assisted review, and $150/hr blended engineering cost.

## Setup

### 1. Install

```bash
git clone https://github.com/tazwaryayyyy/PostMortem-ai
cd PostMortem-ai
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add:
# GROQ_API_KEY=your_groq_key       (required -- Llama fallback + CriticAgent fallback)
# GOOGLE_API_KEY=your_gemini_key   (required -- VisionAgent + CriticAgent)
# VULTR_API_KEY=your_vultr_key     (optional -- primary Llama inference, recommended)
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
git clone https://github.com/tazwaryayyyy/PostMortem-ai /opt/PostMortem-ai
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

If an older server has the legacy Python `docker-compose` command installed, use
Compose v2 (`docker compose`, with a space). The old v1 binary can fail during
recreate with `KeyError: 'ContainerConfig'` on newer Docker images.

```bash
docker compose down --remove-orphans
docker compose up --build -d --force-recreate --remove-orphans
```

### Environment variables

| Variable                        | Default                  | Description                                    |
|---------------------------------|--------------------------|------------------------------------------------|
| `GROQ_API_KEY`                  | required                 | Groq API key — fallback for all Llama agents   |
| `VULTR_API_KEY`                 | optional                 | Vultr Serverless Inference — primary provider for all Llama agents (recommended) |
| `GOOGLE_API_KEY`                | required                 | Gemini: VisionAgent (screenshots + inference) + CriticAgent (adversarial reasoning) |
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

## Submission Narrative

**Short description:** Autonomous incident commander that proves misleading root-cause hypotheses wrong before generating an auditable postmortem.

**Long description:** PostMortem.ai coordinates six specialist agents to investigate production incidents end-to-end. It ingests PagerDuty, Slack, logs, metrics, deploy history, and Gemini visual evidence; plans evidence-gathering steps; calls live HTTP tool endpoints; rejects false leads; escalates low-confidence conclusions; asks an independent CriticAgent to challenge the result; and produces an enterprise postmortem with owners, recurrence risk, detection gaps, and estimated review-cost savings.

**Demo path:** Click `Judge Mode`. The app simulates a PagerDuty webhook, runs the autonomous investigation, shows false-lead rejection, promotes Gemini visual evidence into the evidence plan, displays a CriticAgent challenge, and generates the final report.

**Prize fit:**

- Vultr: production web app on Vultr VM with Docker Compose, nginx, health checks, and persistent SQLite history.
- Google Gemini: Gemini 2.5 Flash runs in two structurally distinct roles — VisionAgent (screenshot analysis) and CriticAgent (adversarial cross-provider challenge of the Groq-built root cause conclusion). The investigation pipeline spans two independent AI providers by design.
- Overall: clear enterprise buyer, measurable labor savings, working public URL, and visible autonomous decision-making.

## License

MIT License. Copyright (c) 2026 tazwaryayyyy. See [LICENSE](LICENSE)
