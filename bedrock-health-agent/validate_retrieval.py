#!/usr/bin/env python3
"""
Call Bedrock Knowledge Bases Retrieve API directly (not via the agent) to
sanity-check that kb_docs ingested correctly.

For each topic query, prints the top 3 chunks (score + source document + snippet).

Run:  python validate_retrieval.py
"""
from __future__ import annotations

import json
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

# (query, filename expected in the top 3)
CHECKS = [
    (
        "HealthFirst Basic Hospital Saver monthly premium and excess",
        "healthfirst_basic.md",
    ),
    (
        "Does HealthFirst Silver Plus cover joint reconstruction and extras dental?",
        "healthfirst_silver.md",
    ),
    (
        "MediSure Bronze Secure Hospital what is not covered pregnancy joint replacement",
        "medisure_bronze.md",
    ),
    (
        "MediSure Gold Complete extras optical limit and pregnancy cover",
        "medisure_gold.md",
    ),
    (
        "SunWell Silver Active Hospital extras physiotherapy and dental limits",
        "sunwell_silver.md",
    ),
    (
        "What is a waiting period for pre-existing conditions and switching funds?",
        "general_waiting_periods.md",
    ),
    (
        "Medicare Levy Surcharge MLS Lifetime Health Cover LHC rebate hospital tiers",
        "general_tiers_rebate_mls_lhc.md",
    ),
]


def _document(result: dict) -> str:
    loc = result.get("location") or {}
    uri = (loc.get("s3Location") or {}).get("uri") or ""
    md = result.get("metadata") or {}
    if not uri:
        uri = md.get("x-amz-bedrock-kb-source-uri") or ""
    return str(uri).rsplit("/", 1)[-1] if uri else "?"


def main() -> int:
    if not os.path.exists(config.STATE_FILE):
        print(f"No deployment state at {config.STATE_FILE}. Run infra/provision.py first.")
        return 1
    with open(config.STATE_FILE) as f:
        state = json.load(f)
    kb_id = state.get("kb_id")
    if not kb_id:
        print("No kb_id in .deployment.json")
        return 1

    runtime = boto3.client("bedrock-agent-runtime", region_name=config.REGION)
    print(f"Knowledge Base {kb_id}  vector_store={state.get('vector_store') or state.get('kb_vector_store')}")
    print()

    failures = 0
    for query, expected in CHECKS:
        print("=" * 72)
        print(f"Q: {query}")
        print(f"   expect {expected} in top 3")
        resp = runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 3},
            },
        )
        hits = resp.get("retrievalResults") or []
        docs = []
        for i, hit in enumerate(hits, 1):
            doc = _document(hit)
            docs.append(doc)
            score = hit.get("score")
            score_s = f"{score:.3f}" if isinstance(score, (int, float)) else str(score)
            text = ((hit.get("content") or {}).get("text") or "").replace("\n", " ")[:180]
            print(f"   {i}. [{doc}] score={score_s}")
            print(f"      {text}")
        ok = expected in docs
        print(f"   {'PASS' if ok else 'MISS'}  top docs={docs}")
        print()
        if not ok:
            failures += 1

    print("=" * 72)
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} queries had the expected source in the top 3")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
