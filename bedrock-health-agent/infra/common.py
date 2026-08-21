"""Shared helpers for provision.py / teardown.py."""
import json
import os
import sys
import time

import boto3

# Make sibling modules (config.py at repo root) importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def clients():
    """Return the set of boto3 clients we need, all pinned to config.REGION."""
    s = boto3.Session(region_name=config.REGION)
    return {
        "s3": s.client("s3"),
        "iam": s.client("iam"),
        "sts": s.client("sts"),
        "aoss": s.client("opensearchserverless"),
        "bedrock": s.client("bedrock"),                      # guardrails, models
        "bedrock_agent": s.client("bedrock-agent"),          # agents + KB
        "lambda": s.client("lambda"),
        "dynamodb": s.client("dynamodb"),
        "logs": s.client("logs"),
    }


def account_id(c):
    return c["sts"].get_caller_identity()["Account"]


def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  state -> {config.STATE_FILE}")


def log(msg):
    print(f"[ctm] {msg}", flush=True)


def wait_until(fn, desc, timeout=600, interval=10):
    """Poll fn() until it returns truthy or timeout. fn returns (done, detail)."""
    start = time.time()
    while True:
        done, detail = fn()
        if done:
            log(f"  {desc}: ready ({detail})")
            return
        if time.time() - start > timeout:
            raise TimeoutError(f"Timed out waiting for {desc} (last: {detail})")
        log(f"  {desc}: {detail} …")
        time.sleep(interval)
