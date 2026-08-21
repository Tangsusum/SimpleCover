"""
agent_client.py — the single seam for talking to the Bedrock Agent.

This is deliberately the ONLY place that calls bedrock-agent-runtime.InvokeAgent.
Later it gets wrapped by a Lambda handler and instrumented with OpenTelemetry, so:

  * invoke_agent(customer_id, session_id, message) -> dict is the one entry point.
  * The returned "trace" is a list of STRUCTURED dicts (tool calls with input/
    output, KB retrievals, guardrail actions, model rationale) — not printed
    strings — so each entry maps cleanly onto a future OTel span.
  * Cross-session memory (keyed by customer_id) lives in a small DynamoDB table
    and is injected as Bedrock session state, so a returning customer_id resumes
    with prior context.

No OTel SDK is added here yet — see the trace_entry() shape for the seam.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402


# ---------------------------------------------------------------------------
# Lazy singletons (cheap to keep warm in a future Lambda).
# ---------------------------------------------------------------------------
_STATE: Optional[Dict[str, Any]] = None
_RUNTIME = None
_DDB = None


def _state() -> Dict[str, Any]:
    global _STATE
    if _STATE is None:
        if not os.path.exists(config.STATE_FILE):
            raise RuntimeError(
                f"Deployment state not found at {config.STATE_FILE}. "
                "Run `python infra/provision.py` first."
            )
        with open(config.STATE_FILE) as f:
            _STATE = json.load(f)
    return _STATE


def _runtime():
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = boto3.client("bedrock-agent-runtime", region_name=config.REGION)
    return _RUNTIME


def _ddb():
    global _DDB
    if _DDB is None:
        _DDB = boto3.client("dynamodb", region_name=config.REGION)
    return _DDB


# ---------------------------------------------------------------------------
# Memory (DynamoDB, keyed by customer_id)
# ---------------------------------------------------------------------------
def _memory_table() -> str:
    return _state().get("memory_table", config.MEMORY_TABLE)


def load_memory(customer_id: str) -> Dict[str, Any]:
    """Return {'context': str, 'turns': [str, ...]} for a customer (or empty)."""
    try:
        r = _ddb().get_item(
            TableName=_memory_table(),
            Key={"customer_id": {"S": customer_id}},
        )
    except Exception:  # noqa: BLE001 - memory is best-effort for a demo
        return {"context": "", "turns": []}
    item = r.get("Item")
    if not item:
        return {"context": "", "turns": []}
    return {
        "context": item.get("context", {}).get("S", ""),
        "turns": [t.get("S", "") for t in item.get("turns", {}).get("L", [])],
    }


def save_memory(customer_id: str, user_message: str) -> str:
    """Append this turn to the customer's memory and return the updated context."""
    mem = load_memory(customer_id)
    turns = (mem["turns"] + [user_message])[-10:]  # keep last 10 user turns
    context = "Earlier the customer said: " + " | ".join(turns)
    try:
        _ddb().put_item(
            TableName=_memory_table(),
            Item={
                "customer_id": {"S": customer_id},
                "context": {"S": context[:1900]},
                "turns": {"L": [{"S": t[:400]} for t in turns]},
                "updated": {"S": datetime.datetime.utcnow().isoformat() + "Z"},
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return context


# ---------------------------------------------------------------------------
# Trace helpers — the shape below is the future OTel-span seam.
# ---------------------------------------------------------------------------
def trace_entry(kind: str, **attrs) -> Dict[str, Any]:
    """One structured trace record. kind ∈ {tool_call, kb_lookup, guardrail,
    rationale}. attrs become span attributes later."""
    return {"kind": kind, **attrs}


def _parse_orchestration(trace_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn one Bedrock 'trace' event into zero or more structured entries."""
    out: List[Dict[str, Any]] = []
    orch = trace_obj.get("orchestrationTrace", {})

    # Tool call / KB query inputs
    inv = orch.get("invocationInput", {})
    ag_in = inv.get("actionGroupInvocationInput")
    if ag_in:
        params = {p.get("name"): p.get("value") for p in ag_in.get("parameters", [])}
        out.append(trace_entry(
            "tool_call",
            phase="input",
            action_group=ag_in.get("actionGroupName"),
            tool=ag_in.get("function") or ag_in.get("apiPath"),
            input=params,
        ))
    kb_in = inv.get("knowledgeBaseLookupInput")
    if kb_in:
        out.append(trace_entry(
            "kb_lookup",
            phase="input",
            knowledge_base_id=kb_in.get("knowledgeBaseId"),
            query=kb_in.get("text"),
        ))

    # Observations (outputs)
    obs = orch.get("observation", {})
    ag_out = obs.get("actionGroupInvocationOutput")
    if ag_out:
        out.append(trace_entry("tool_call", phase="output",
                               output=_maybe_json(ag_out.get("text"))))
    kb_out = obs.get("knowledgeBaseLookupOutput")
    if kb_out:
        chunks = []
        for ref in kb_out.get("retrievedReferences", []):
            chunks.append({
                "text": ref.get("content", {}).get("text", "")[:600],
                "source": ref.get("location", {}),
                "score": ref.get("metadata", {}).get("score"),
            })
        out.append(trace_entry("kb_lookup", phase="output", chunks=chunks))

    # Model rationale (the agent's reasoning step), useful for the demo/trace.
    rationale = orch.get("rationale", {}).get("text")
    if rationale:
        out.append(trace_entry("rationale", text=rationale))

    return out


def _parse_guardrail(trace_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    g = trace_obj.get("guardrailTrace")
    if not g:
        return []
    action = g.get("action")
    return [trace_entry(
        "guardrail",
        action=action,  # "INTERVENED" or "NONE"
        blocked=(action == "INTERVENED"),
        input_assessments=g.get("inputAssessments"),
        output_assessments=g.get("outputAssessments"),
    )]


def _maybe_json(text: Optional[str]) -> Any:
    if not text:
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


# ---------------------------------------------------------------------------
# THE entry point
# ---------------------------------------------------------------------------
def invoke_agent(customer_id: str, session_id: str, message: str) -> Dict[str, Any]:
    """
    Invoke the Bedrock Agent for one turn.

    Returns:
        {
          "response": str,             # the agent's natural-language reply
          "trace": [ {kind: ...}, ...] # structured, OTel-ready
          "customer_id": str,
          "session_id": str,
        }
    """
    st = _state()
    agent_id = st["agent_id"]
    alias_id = st["agent_alias_id"]

    # Pull prior context for this returning customer and inject it as session
    # state. We also prepend a compact hint so continuity is robust regardless of
    # the agent's prompt template.
    mem = load_memory(customer_id)
    context = mem.get("context", "")
    input_text = message
    if context:
        input_text = f"[known customer context: {context}]\n\n{message}"

    session_state = {
        "sessionAttributes": {"customer_id": customer_id},
        "promptSessionAttributes": {
            "customer_id": customer_id,
            "customer_context": context or "none",
        },
    }

    resp = _runtime().invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=input_text,
        enableTrace=True,
        sessionState=session_state,
    )

    response_text = ""
    trace: List[Dict[str, Any]] = []
    for event in resp["completion"]:
        if "chunk" in event:
            response_text += event["chunk"]["bytes"].decode("utf-8")
        elif "trace" in event:
            trace_obj = event["trace"].get("trace", {})
            trace.extend(_parse_orchestration(trace_obj))
            trace.extend(_parse_guardrail(trace_obj))

    # Persist this turn so the next call with the same customer_id has continuity.
    save_memory(customer_id, message)

    return {
        "response": response_text.strip(),
        "trace": trace,
        "customer_id": customer_id,
        "session_id": session_id,
    }
