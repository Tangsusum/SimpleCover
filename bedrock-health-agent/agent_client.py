"""
agent_client.py — the single seam for talking to the Bedrock Agent.

This is deliberately the ONLY place that calls bedrock-agent-runtime.InvokeAgent.

  * invoke_agent(customer_id, session_id, message) -> dict is the one entry point.
  * Conversation continuity is Bedrock's sessionId (same session_id = same
    chat). Prior turns are not prepended onto the user message.
  * Datadog Agent Observability tracks the same session_id on the workflow span.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import boto3
from ddtrace.llmobs import LLMObs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_guard  # noqa: E402
import config  # noqa: E402


# ---------------------------------------------------------------------------
# Lazy singletons (cheap to keep warm in a future Lambda).
# ---------------------------------------------------------------------------
_STATE: Optional[Dict[str, Any]] = None
_RUNTIME = None


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


# ---------------------------------------------------------------------------
# Trace helpers — the shape below is the future OTel-span seam.
# ---------------------------------------------------------------------------
def trace_entry(kind: str, **attrs) -> Dict[str, Any]:
    """One structured trace record. kind ∈ {tool_call, kb_lookup, guardrail,
    rationale}. attrs become span attributes later."""
    return {"kind": kind, **attrs}


def _retrieved_chunk(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a Bedrock retrievedReference into text + source document + score."""
    loc = ref.get("location") or {}
    s3 = loc.get("s3Location") or {}
    uri = s3.get("uri") or loc.get("uri") or ""
    md = ref.get("metadata") or {}
    if not uri:
        uri = (
            md.get("x-amz-bedrock-kb-source-uri")
            or md.get("sourceUri")
            or md.get("source")
            or ""
        )
    document = str(uri).rsplit("/", 1)[-1] if uri else ""
    score = ref.get("score")
    if score is None:
        score = md.get("score")
    return {
        "text": (ref.get("content") or {}).get("text", "")[:600],
        "document": document,
        "uri": uri,
        "score": score,
        "source": loc,
    }


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
        chunks = [_retrieved_chunk(ref) for ref in kb_out.get("retrievedReferences", [])]
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
def invoke_agent(
    customer_id: str,
    session_id: str,
    message: str,
    agent_id: Optional[str] = None,
    alias_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Invoke the Bedrock Agent for one turn.

    agent_id/alias_id default to the deployed state (.deployment.json) but can
    be overridden to target a different agent/alias, e.g. a variant agent
    provisioned for a model-comparison experiment.

    Returns:
        {
          "response": str,             # the agent's natural-language reply
          "trace": [ {kind: ...}, ...] # structured, OTel-ready
          "customer_id": str,
          "session_id": str,
        }
    """
    st = _state()
    agent_id = agent_id or st["agent_id"]
    alias_id = alias_id or st["agent_alias_id"]

    session_state = {
        "sessionAttributes": {
            "customer_id": customer_id,
            "session_id": session_id,
        },
        "promptSessionAttributes": {
            "customer_id": customer_id,
            "session_id": session_id,
        },
    }

    with LLMObs.workflow(name="invoke_agent", session_id=session_id):
        LLMObs.annotate(
            input_data=message,
            tags={"user_id": customer_id, "user_handle": customer_id},
        )
        blocked = ai_guard.evaluate_prompt(message)
        if blocked:
            LLMObs.annotate(output_data=blocked["response"])
            return {
                "response": blocked["response"],
                "trace": blocked.get("trace") or [],
                "customer_id": customer_id,
                "session_id": session_id,
            }
        resp = _runtime().invoke_agent(
            agentId=agent_id,
            agentAliasId=alias_id,
            sessionId=session_id,
            inputText=message,
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

        reply = response_text.strip()
        LLMObs.annotate(output_data=reply)
        return {
            "response": reply,
            "trace": trace,
            "customer_id": customer_id,
            "session_id": session_id,
        }
