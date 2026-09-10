"""
Thin local backend for the SimpleCover demo frontend.

The ONLY job of this server is to expose `POST /api/chat` and forward the call to
the existing `agent_client.invoke_agent(customer_id, session_id, message)` from
the Bedrock Agent prototype. Keep the request/response shape stable:

    in :  {customer_id, session_id, message}
    out:  {response, trace}

That is the exact seam that gets swapped for a real Lambda handler later — the
handler body becomes essentially this endpoint body, so the swap is close to a
no-op. OTel traces go to the local Datadog Agent (OTLP :4318).

Run:
    cd backend
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8000

Optional offline UI mode (no AWS / no deployed agent needed):
    SIMPLE_COVER_BACKEND_MOCK=1 uvicorn server:app --reload --port 8000
"""
import os
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
# AI Guard requires these before ddtrace starts (see
# https://docs.datadoghq.com/security/ai_guard/setup/).
os.environ.setdefault("DD_AI_GUARD_ENABLED", "true")
os.environ.setdefault("DD_SERVICE", "simple_cover_backend")
os.environ.setdefault("DD_ENV", "dev")
os.environ.setdefault("DD_TRACE_ENABLED", "true")
if not os.environ.get("DD_APP_KEY") and os.environ.get("DD_APPLICATION_KEY"):
    os.environ["DD_APP_KEY"] = os.environ["DD_APPLICATION_KEY"]
import ddtrace.auto  # noqa: E402

import sys

from ddtrace.llmobs import LLMObs
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
from pydantic import BaseModel

from telemetry import setup_telemetry, tracer


def _patch_ddtrace_bedrock_kb_observation() -> None:
    """ddtrace treats retrievedReferences as a dict; Bedrock returns a list, which
    aborts LLM-Obs span translation for the whole InvokeAgent call."""
    try:
        from ddtrace.llmobs._integrations import bedrock_agents as ba
    except ImportError:
        return
    orig = ba._observation_span

    def _observation_span(observation, root_span, current_active_span):
        obs = observation
        if observation.get("type") == "KNOWLEDGE_BASE":
            out = observation.get("knowledgeBaseLookupOutput") or {}
            refs = out.get("retrievedReferences")
            if isinstance(refs, list):
                obs = dict(observation)
                kb_out = dict(out)
                texts = []
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    text = (ref.get("content") or {}).get("text") or ref.get("text") or ""
                    if text:
                        texts.append(text)
                kb_out["retrievedReferences"] = {"text": "\n\n".join(texts)}
                obs["knowledgeBaseLookupOutput"] = kb_out
        return orig(obs, root_span, current_active_span)

    ba._observation_span = _observation_span


_patch_ddtrace_bedrock_kb_observation()

# Make the agent prototype importable (agent_client.py lives next door).
AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "bedrock-health-agent")
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

MOCK = os.environ.get("SIMPLE_COVER_BACKEND_MOCK", "").lower() in ("1", "true", "yes")

# Importing agent_client is safe without AWS (all AWS clients are lazy); it only
# touches AWS when invoke_agent() is actually called.
import agent_client  # noqa: E402

app = FastAPI(title="SimpleCover demo backend", version="0.1.0")

# CORS: wide open for local frontend, including RUM trace-propagation headers.
TRACE_HEADERS = [
    "Content-Type",
    "traceparent",
    "tracestate",
    "baggage",
    "x-datadog-trace-id",
    "x-datadog-parent-id",
    "x-datadog-origin",
    "x-datadog-sampling-priority",
    "x-datadog-tags",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"] + TRACE_HEADERS,
    expose_headers=TRACE_HEADERS,
)
setup_telemetry(app)


class ChatRequest(BaseModel):
    customer_id: str
    session_id: str
    message: str


@app.get("/api/health")
def health():
    return {"ok": True, "mock": MOCK}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """The one endpoint. Thin wrapper over invoke_agent()."""
    with LLMObs.workflow(name="chat", session_id=req.session_id):
        LLMObs.annotate(
            input_data=req.message,
            tags={"user_id": req.customer_id, "user_handle": req.customer_id},
        )
        with tracer().start_as_current_span("chat.invoke_agent") as span:
            span.set_attribute("enduser.id", req.customer_id)
            span.set_attribute("session.id", req.session_id)
            span.set_attribute("simple_cover.mock", MOCK)
            span.set_attribute("simple_cover.message_length", len(req.message or ""))
            ctx = span.get_span_context()
            print(
                f"[otel] chat trace_id={ctx.trace_id:032x} span_id={ctx.span_id:016x}",
                flush=True,
            )

            if MOCK:
                result = _mock_invoke(req.customer_id, req.session_id, req.message)
            else:
                try:
                    result = agent_client.invoke_agent(
                        req.customer_id, req.session_id, req.message
                    )
                except Exception as exc:  # noqa: BLE001 - surface errors to the UI
                    span.record_exception(exc)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                    LLMObs.annotate(output_data=str(exc))
                    return {
                        "response": (
                            "⚠️ The agent call failed. Is the Bedrock Agent deployed "
                            "(run `python infra/provision.py`) and are your AWS "
                            f"credentials set?\n\nDetails: {exc}"
                        ),
                        "trace": [{"kind": "error", "message": str(exc)}],
                    }

            events = result.get("trace") or []
            tools = [
                e.get("tool")
                for e in events
                if e.get("kind") == "tool_call" and e.get("phase") == "input" and e.get("tool")
            ]
            if tools:
                span.set_attribute("simple_cover.tools", ",".join(tools))
            kb_hits = sum(
                1 for e in events if e.get("kind") == "kb_lookup" and e.get("phase") == "output"
            )
            if kb_hits:
                span.set_attribute("simple_cover.kb_lookups", kb_hits)

            reply = result.get("response", "")
            LLMObs.annotate(output_data=reply)
            return {"response": reply, "trace": events}


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Offline mock so the UI can be built/demoed without a deployed agent.
# Lightly keyword-driven just so the trace panel shows something relevant.
# ---------------------------------------------------------------------------
def _mock_invoke(customer_id, session_id, message):
    low = message.lower()

    if any(w in low for w in ("ignore all previous", "system prompt", "no rules")):
        return {
            "response": ("I can only help with private health insurance, so I "
                         "can't do that. What cover are you looking for?"),
            "trace": [{"kind": "guardrail", "action": "INTERVENED", "blocked": True}],
        }
    if any(w in low for w in ("human", "person", "ridiculous", "frustrat", "speak to")):
        return {
            "response": ("I'm sorry this has been frustrating — I'm arranging for "
                         "a human consultant to call you back. Your reference is "
                         "CB-482013, tomorrow between 10:00–11:00."),
            "trace": [
                {"kind": "tool_call", "phase": "input", "tool": "schedule_callback",
                 "input": {"reason": "customer requested a human / frustrated",
                           "customer_id": customer_id}},
                {"kind": "tool_call", "phase": "output",
                 "output": {"confirmation_id": "CB-482013",
                            "scheduled_for": "tomorrow 10:00–11:00"}},
            ],
        }
    if any(w in low for w in ("woolworths", "receipt", "eftpos", "invoice")):
        return {
            "response": ("That doesn't look like a health-insurance document — I "
                         "couldn't find any cover, premium or tier details. Could "
                         "you paste your policy or PHIS instead?"),
            "trace": [
                {"kind": "tool_call", "phase": "input",
                 "tool": "process_uploaded_document",
                 "input": {"document_text": message[:80] + "…"}},
                {"kind": "tool_call", "phase": "output",
                 "output": {"is_health_insurance_document": False,
                            "rejection_reason": "not enough health-insurance signals"}},
            ],
        }
    if any(w in low for w in ("phis", "premium", "tier", "hospital cover", "fund:")):
        return {
            "response": ("Thanks — I read that as a MediSure Gold Complete policy, "
                         "Hospital + Extras, about $310/month, $250 excess, NSW. "
                         "Want me to compare it against similar options?"),
            "trace": [
                {"kind": "tool_call", "phase": "input",
                 "tool": "process_uploaded_document",
                 "input": {"document_text": message[:80] + "…"}},
                {"kind": "tool_call", "phase": "output",
                 "output": {"is_health_insurance_document": True,
                            "extracted_fields": {"fund": "MediSure", "tier": "Gold",
                                                 "monthly_premium_aud": 310}}},
            ],
        }
    # default: a quote-style answer that exercises KB + get_quotes in the trace
    return {
        "response": ("[MOCK] Based on what you've told me, here are a couple of "
                     "options:\n\n• HealthFirst Basic Hospital Saver — $95/mo — "
                     "cheapest way to avoid MLS, but hospital-only.\n• MediSure "
                     "Bronze Secure — $120/mo — covers common procedures, still no "
                     "extras.\n\nWant me to factor in extras like dental/physio?"),
        "trace": [
            {"kind": "kb_lookup", "phase": "input", "query": "entry level hospital cover tiers"},
            {"kind": "kb_lookup", "phase": "output",
             "chunks": [{"text": "Basic tier covers mostly restricted categories …"},
                        {"text": "Holding hospital cover lets you avoid the MLS …"}]},
            {"kind": "tool_call", "phase": "input", "tool": "get_quotes",
             "input": {"state": "NSW", "cover_type": "hospital", "priorities": "cheap entry-level"}},
            {"kind": "tool_call", "phase": "output",
             "output": {"quotes": [{"fund": "HealthFirst", "product": "Basic Hospital Saver",
                                    "monthly_premium_aud": 95},
                                   {"fund": "MediSure", "product": "Bronze Secure Hospital",
                                    "monthly_premium_aud": 120}]}},
        ],
    }
