"""Datadog AI Guard evaluation for the Bedrock agent seam.

Bedrock Agents are not in the automatic AI Guard integrations (LangChain /
OpenAI / Anthropic), so prompts are evaluated explicitly via the ddtrace SDK.
https://docs.datadoghq.com/security/ai_guard/setup/
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_INSTRUCTIONS: Optional[str] = None
_CLIENT = None
_CLIENT_FAILED = False

_SYSTEM_FALLBACK = (
    "You are SimpleCover Assist, a private health-insurance comparison assistant. "
    "Authorized tools: get_quotes, estimate_rebate_and_lhc, schedule_callback, "
    "process_uploaded_document. Authorized data: fictional policy details, "
    "waiting periods, rebate/MLS/LHC. Do not access unrelated systems."
)


def apply_env() -> None:
    """Set AI Guard env vars. Call before `import ddtrace.auto`."""
    os.environ.setdefault("DD_AI_GUARD_ENABLED", "true")
    os.environ.setdefault("DD_SERVICE", os.environ.get("OTEL_SERVICE_NAME", "simple_cover_backend"))
    os.environ.setdefault("DD_ENV", os.environ.get("OTEL_ENV", "dev"))
    os.environ.setdefault("DD_TRACE_ENABLED", "true")
    if not os.environ.get("DD_APP_KEY") and os.environ.get("DD_APPLICATION_KEY"):
        os.environ["DD_APP_KEY"] = os.environ["DD_APPLICATION_KEY"]


def enabled() -> bool:
    return os.environ.get("DD_AI_GUARD_ENABLED", "").lower() in ("1", "true", "yes")


def _instructions() -> str:
    global _INSTRUCTIONS
    if _INSTRUCTIONS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent", "instructions.txt")
        try:
            with open(path) as f:
                _INSTRUCTIONS = f.read().strip() or _SYSTEM_FALLBACK
        except OSError:
            _INSTRUCTIONS = _SYSTEM_FALLBACK
    return _INSTRUCTIONS


def _client():
    global _CLIENT, _CLIENT_FAILED
    if _CLIENT_FAILED:
        return None
    if _CLIENT is None:
        from ddtrace.aiguard import new_ai_guard_client

        try:
            _CLIENT = new_ai_guard_client()
        except Exception as exc:  # noqa: BLE001
            _CLIENT_FAILED = True
            print(f"[ai_guard] client init failed: {exc}", flush=True)
            return None
    return _CLIENT


def evaluate_prompt(user_message: str) -> Optional[Dict[str, Any]]:
    """Block unsafe prompts. Returns a response+trace dict, or None to proceed.

    Fail-open if AI Guard is off or the evaluate API errors (e.g. Preview not
    enabled on the org, missing ai_guard_evaluate scope).
    """
    if not enabled() or not (user_message or "").strip():
        return None
    client = _client()
    if client is None:
        return None
    from ddtrace.aiguard import AIGuardAbortError, AIGuardClientError, Message

    try:
        result = client.evaluate(
            messages=[
                Message(role="system", content=_instructions()),
                Message(role="user", content=user_message),
            ],
        )
        print(
            f"[ai_guard] action={result.get('action')} reason={result.get('reason') or ''}",
            flush=True,
        )
    except AIGuardAbortError as abort:
        reason = abort.reason or "This request was blocked by AI Guard."
        return {
            "response": (
                "I can't process that request. "
                f"{reason}"
            ),
            "trace": [{
                "kind": "guardrail",
                "action": abort.action,
                "blocked": True,
                "reason": reason,
                "tags": list(abort.tags or []),
            }],
        }
    except AIGuardClientError as exc:
        print(f"[ai_guard] evaluate failed (fail-open): {exc}", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[ai_guard] evaluate error (fail-open): {exc}", flush=True)
        return None
    return None
