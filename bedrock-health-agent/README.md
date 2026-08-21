# CTM Health-Insurance AI Assistant — Bedrock Agent prototype

A **bare-minimum, throwaway demo** of an AI health-insurance sales assistant on
**Amazon Bedrock Agents**. It shows the core call-centre-replacement slice
end-to-end: natural-language intake → RAG knowledge lookup → mocked backend tools
(quotes, rebate, callback, document parsing) → human handoff → guardrails →
cross-session memory.

> ⚠️ Everything is **mock/fictional** — fake funds, illustrative prices, no real
> APIs, not compliant, not production. It exists to demonstrate the concept.

This is **stage 1**. Later it will be wrapped by a Lambda + frontend and
instrumented with OpenTelemetry (frontend → Lambda → Bedrock Agent → tools/KB).
The code is structured so that step is easy — see [Design for the future](#design-for-the-future-lambda--otel).

---

## What's in the box

| Piece | Where | What it is |
|---|---|---|
| KB documents | `kb_docs/*.md` | 5 mock policies (Basic→Gold across 3 fake funds) + 2 general-knowledge docs (tiers/rebate/MLS/LHC, waiting periods) |
| Tools Lambda | `lambdas/insurance_tools/handler.py` | `get_quotes`, `estimate_rebate_and_lhc`, `schedule_callback`, `process_uploaded_document` — all mocked, self-contained |
| Agent prompt | `agent/instructions.txt` | System instructions (clarify-before-quote, never invent prices, handoff rules, doc handling, scope/injection resistance) |
| Action schema | `agent/action_group_functions.json` | Bedrock function schema for the 4 tools |
| Guardrail | created by `provision.py` | PROMPT_ATTACK filter + off-topic denied-topic |
| Memory | DynamoDB table | Cross-session context keyed by `customer_id` |
| IaC | `infra/provision.py`, `infra/teardown.py` | boto3 stand-up / tear-down of *everything* |
| Client seam | `agent_client.py` | The single `invoke_agent(...)` entry point (future Lambda handler) |
| CLI | `chat.py` | Local test harness only |
| Scenarios | `scenarios.md` | Copy-paste inputs for all 8 required use cases |

---

## Architecture

```
 chat.py ─┐
          ├─▶ agent_client.invoke_agent(customer_id, session_id, message)
          │        │  1. load memory (DynamoDB, by customer_id)
          │        │  2. InvokeAgent (bedrock-agent-runtime) + session state
          │        │  3. parse response + STRUCTURED trace
          │        └▶ 4. save memory
          ▼
   ┌──────────────── Bedrock Agent ────────────────┐
   │  instructions.txt  +  Guardrail (prompt-attack)│
   │      │                         │               │
   │      ▼                         ▼               │
   │  Knowledge Base           Action group         │
   │  (OpenSearch Serverless)  (Lambda: 4 tools)     │
   │      ▲                                          │
   │  S3 (kb_docs/*.md) ──ingest──┘                  │
   └────────────────────────────────────────────────┘
```

Memory note: Bedrock's native session state is per-session and ephemeral, so it
**can't** by itself give a returning `customer_id` continuity across sessions.
We therefore keep a tiny DynamoDB table keyed by `customer_id` and inject its
contents as Bedrock session state on each invoke. Simple and demonstrable.

---

## Prerequisites

- An AWS account + credentials configured for the CLI/boto3 (`aws configure` or
  env vars / SSO). The identity needs permissions for Bedrock, Bedrock Agent,
  OpenSearch Serverless, S3, Lambda, IAM, DynamoDB.
- **Bedrock model access enabled** in your region for BOTH:
  - the agent model (default `au.anthropic.claude-sonnet-4-5-20250929-v1:0` in `ap-southeast-2`; Claude 3.5 Sonnet on-demand is EOL)
  - the embeddings model (default `amazon.titan-embed-text-v2:0`)
  Enable these in the Bedrock console → *Model access* before provisioning.
- Python 3.10+.

```bash
cd bedrock-health-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional config overrides (all have sensible defaults in `config.py`):

```bash
export AWS_REGION=ap-southeast-2
export CTM_PREFIX=ctm-health-demo          # name prefix for every resource
export CTM_AGENT_MODEL_ID=au.anthropic.claude-sonnet-4-5-20250929-v1:0
```

---

## Deploy

```bash
python infra/provision.py
```

This stands up **everything** (S3 + docs, IAM roles, Lambda, OpenSearch
Serverless collection + index, Knowledge Base + ingestion, Guardrail, Agent +
action group + KB association + alias, DynamoDB table) and writes a
`.deployment.json` state file that the client and teardown read.

Takes ~5–10 minutes, mostly waiting on the OpenSearch Serverless collection and
KB ingestion. It's chatty about what it's doing. If a step fails midway, fix the
cause and re-run (most steps are idempotent), or run teardown and start clean.

---

## Run the demo

```bash
python chat.py
```

Enter a customer id (this simulates a logged-in cookie), then type messages. See
`scenarios.md` for copy-paste inputs covering all 8 use cases. Useful commands
inside the CLI:

- `:paste … :end` — multi-line paste (for the "uploaded document" scenarios)
- `:new` — new session id, same customer (used to demo memory)
- `:customer <id>` — switch customer
- `:trace on|off` — show/hide the structured trace
- `:quit`

Each reply prints a `--- trace ---` block so you can *see* the tool calls, KB
retrievals, and guardrail actions that fired.

---

## Tear down (it's throwaway)

```bash
python infra/teardown.py
```

Removes every resource recorded in `.deployment.json` (best-effort, safe to
re-run) and deletes the state file. Run this when you're done to avoid ongoing
OpenSearch Serverless / DynamoDB charges.

---

## Design for the future (Lambda + OTel)

Deliberate choices so the next stage is clean:

- **One seam.** `agent_client.invoke_agent(customer_id, session_id, message) ->
  {response, trace, ...}` is the *only* caller of `InvokeAgent`. Wrap this
  function body in a Lambda handler; nothing else needs to change.
- **Structured trace, not strings.** `invoke_agent` returns `trace` as a list of
  typed dicts (`{"kind": "tool_call"|"kb_lookup"|"guardrail"|"rationale", ...}`)
  via `trace_entry()`. Each entry maps directly to a future OTel span — iterate
  the list and emit spans; no re-parsing of printed text. `chat.py` is the only
  place that renders them to strings, and it's throwaway.
- **No OTel yet.** No SDK, spans, or exporters are added — just a shape that
  won't need reworking when they are.

---

## Notes / limitations (intentional)

- Prices, funds, rebate/LHC math, and document extraction are all **mock**.
  `estimate_rebate_and_lhc` and `get_quotes` label themselves as illustrative.
- `process_uploaded_document` uses keyword + regex heuristics (no OCR, no LLM
  call inside the Lambda) so the tool Lambda stays dependency-free.
- The guardrail policy is intentionally minimal — enough to visibly block an
  obvious prompt-injection and off-topic asks.
- **Not yet deployed/verified against a live AWS account by the author** — the
  IaC is written to run but you should expect to enable model access first and
  watch the first `provision.py` run. Common first-run snags:
  - *Model access not enabled* → enable in Bedrock console and re-run.
  - *OpenSearch index 403 right after creation* → `provision.py` already retries;
    if it still fails, re-run (data-access policy sometimes needs a minute).
  - *Agent role name* must start with `AmazonBedrockExecutionRoleForAgents_`
    (handled automatically).
```
