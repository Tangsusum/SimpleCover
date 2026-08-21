#!/usr/bin/env python3
"""
chat.py — thin local CLI for testing the agent. NOT part of the deployed system;
it just exercises agent_client.invoke_agent().

Usage:
    python chat.py                 # prompts for a customer id
    python chat.py --customer C123 # start directly as customer C123

In the prompt:
    <type a message>   send it to the agent
    :paste             enter multi-line mode (for pasting a "document"),
                       finish by typing  :end  on its own line
    :new               start a fresh session id (same customer, new conversation)
    :customer <id>     switch to a different customer id
    :trace on|off      toggle printing of the structured trace (default on)
    :quit / Ctrl-D     exit
"""
import os
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
import ddtrace.auto  # noqa: E402

import argparse
import json
import uuid

import agent_client


def new_session() -> str:
    return "sess-" + uuid.uuid4().hex[:12]


def print_trace(trace):
    if not trace:
        print("    (no trace events)")
        return
    for e in trace:
        kind = e.get("kind")
        if kind == "tool_call" and e.get("phase") == "input":
            print(f"    → tool call  {e.get('tool')}  input={json.dumps(e.get('input'))}")
        elif kind == "tool_call" and e.get("phase") == "output":
            out = e.get("output")
            out = json.dumps(out) if not isinstance(out, str) else out
            print(f"    ← tool result {out[:300]}")
        elif kind == "kb_lookup" and e.get("phase") == "input":
            print(f"    → KB query   {e.get('query')!r}")
        elif kind == "kb_lookup" and e.get("phase") == "output":
            print(f"    ← KB chunks  {len(e.get('chunks', []))} retrieved")
            for ch in e.get("chunks", [])[:3]:
                print(f"        · {ch.get('text','')[:120]!r}")
        elif kind == "guardrail":
            flag = "BLOCKED" if e.get("blocked") else "passed"
            print(f"    ⚑ guardrail  {flag} ({e.get('action')})")
        elif kind == "rationale":
            print(f"    · rationale  {e.get('text','')[:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", help="customer id (simulates cookie/login)")
    args = ap.parse_args()

    customer_id = args.customer or input("Customer id (simulates login/cookie): ").strip() or "guest"
    session_id = new_session()
    show_trace = True

    print(f"\nCTM Assist — customer={customer_id} session={session_id}")
    print("Type a message, or :paste / :new / :customer <id> / :trace on|off / :quit\n")

    while True:
        try:
            line = input(f"[{customer_id}] you> ").strip()
        except EOFError:
            print()
            break

        if not line:
            continue
        if line in (":quit", ":q", ":exit"):
            break
        if line == ":new":
            session_id = new_session()
            print(f"  (new session {session_id})")
            continue
        if line.startswith(":customer"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                customer_id = parts[1].strip()
                session_id = new_session()
                print(f"  (now customer {customer_id}, session {session_id})")
            continue
        if line.startswith(":trace"):
            show_trace = not line.endswith("off")
            print(f"  (trace {'on' if show_trace else 'off'})")
            continue
        if line == ":paste":
            print("  paste text; finish with a line containing only  :end")
            buf = []
            while True:
                try:
                    l = input()
                except EOFError:
                    break
                if l.strip() == ":end":
                    break
                buf.append(l)
            message = "\n".join(buf)
            if not message.strip():
                print("  (nothing pasted)")
                continue
        else:
            message = line

        try:
            result = agent_client.invoke_agent(customer_id, session_id, message)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR invoking agent: {exc}")
            continue

        print(f"\n  assistant> {result['response']}\n")
        if show_trace:
            print("  --- trace ---")
            print_trace(result["trace"])
            print()


if __name__ == "__main__":
    main()
