# SimpleCover — demo frontend for the health-insurance AI agent

A **local, throwaway demo UI** for the Bedrock health-insurance assistant built in
[`bedrock-health-agent/`](bedrock-health-agent/). It's a single web page styled
loosely after an Australian health-insurance comparison site (purple hero, coral
CTA, rounded cards) with an **original, fictional brand ("SimpleCover")** — no
real logos, mascots, brand names, or copy from any real site.

The page talks to a **thin local backend** that forwards every message to the
existing `agent_client.invoke_agent(customer_id, session_id, message)`. That
backend endpoint is the seam that later becomes a Lambda + gets OpenTelemetry
tracing — its request/response shape is kept deliberately stable.

```
 browser (frontend/)  ──POST /api/chat──▶  backend/server.py  ──▶  agent_client.invoke_agent()
   {customer_id, session_id, message}         (thin wrapper)          → Bedrock Agent
   ◀── {response, trace} ──────────────                               ← {response, trace}
```

> The frontend/backend never touch AWS directly — browser JS can't hold AWS
> credentials, and the backend is the single choke point that swaps for a Lambda.

---

## Layout

| Folder | What |
|---|---|
| `frontend/` | Single-page UI (`index.html` + `styles.css` + `app.js`), no build step |
| `backend/`  | FastAPI server exposing `POST /api/chat` → `invoke_agent` |
| `bedrock-health-agent/` | The agent prototype from the previous step (KB, tools, IaC, `agent_client.py`) |

---

## Prerequisites

- Python 3.10+.
- For **real** agent calls: the Bedrock Agent deployed
  (`cd bedrock-health-agent && python infra/provision.py`) and AWS credentials
  set in your shell (same account/region as the deployment). See that folder's
  README.
- For **UI-only** demoing without AWS: use the backend's mock mode (below).

---

## 1. Start the backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# real agent (needs the deployed Bedrock Agent + AWS creds):
uvicorn server:app --reload --port 8000

# OR offline mock (no AWS, canned responses + traces, for UI demos):
SIMPLE_COVER_BACKEND_MOCK=1 uvicorn server:app --reload --port 8000
```

Backend runs at **http://localhost:8000** — health check: `GET /api/health`.

> The backend imports `agent_client` from `../bedrock-health-agent`, so keep the
> two folders side by side. `boto3` comes in transitively; if you deploy/run the
> real agent you'll already have the agent prototype's deps installed.

## 2. Start the frontend

Any static file server works. Simplest:

```bash
cd frontend
python -m http.server 5173
```

Then open **http://localhost:5173** in your browser.

> If you serve the frontend on a different origin/port, that's fine — the backend
> has permissive CORS for local dev. To point the page at a non-default backend,
> append `?api=http://localhost:8000` to the URL.

---

## Using it in a demo

- On load you get a random **Customer ID** and **session**. Edit the Customer ID
  field and click **New session** to simulate a *returning* customer (same
  `customer_id`, new session) — this is how you show cross-session memory.
- Type naturally, or click **📄 Paste document** to paste a policy/PHIS (or a
  grocery receipt) to exercise the document tool.
- Each agent reply has an expandable **Trace** line showing the tool calls, KB
  retrievals, and guardrail actions that fired — this is what makes RAG and tool
  use visible during the demo.

**Example messages:** reuse the 8 scenarios in
[`bedrock-health-agent/scenarios.md`](bedrock-health-agent/scenarios.md) — paste
them straight into the chat (use the Paste-document button for the two
"uploaded document" scenarios).

---

## Scope / notes

- Local only — no AWS deployment of the frontend or backend.
- No auth, no persistence beyond the in-memory browser session.
- Pasted text stands in for a real file upload (no file/image upload).
- Backend traces are exported with OpenTelemetry (OTLP/HTTP) to the local
  Datadog Agent collector at `http://127.0.0.1:4318`. Service name `simple_cover_backend`,
  env `dev`. The Agent must have `otlp_config.receiver` enabled.
- Mock mode (`SIMPLE_COVER_BACKEND_MOCK=1`) returns lightly keyword-driven canned
  responses so the UI can be shown without a live agent; turn it off for the real
  thing.
