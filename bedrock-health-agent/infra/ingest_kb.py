#!/usr/bin/env python3
"""
Re-upload kb_docs/*.md to the docs S3 bucket and start a Knowledge Base
ingestion job. Use this after editing mock documents without re-running full
provision.

Run:  python infra/ingest_kb.py
"""
import os

import common
import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DOCS_DIR = os.path.join(REPO, "kb_docs")


def main():
    c = common.clients()
    state = common.load_state()
    bucket = state.get("s3_bucket") or config.S3_BUCKET
    kb_id = state.get("kb_id")
    ds_id = state.get("data_source_id")
    if not kb_id or not ds_id:
        raise SystemExit("No kb_id/data_source_id in .deployment.json — run provision.py first.")

    n = 0
    for fn in sorted(os.listdir(KB_DOCS_DIR)):
        if not fn.endswith(".md"):
            continue
        c["s3"].upload_file(os.path.join(KB_DOCS_DIR, fn), bucket, fn)
        n += 1
    common.log(f"uploaded {n} docs to s3://{bucket}/")

    ba = c["bedrock_agent"]
    job = ba.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    job_id = job["ingestionJob"]["ingestionJobId"]

    def _ing_ready():
        j = ba.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        return (j["status"] in ("COMPLETE", "FAILED"), j["status"])

    common.wait_until(_ing_ready, "KB ingestion", timeout=600, interval=15)
    ing = ba.get_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
    )["ingestionJob"]
    if ing["status"] != "COMPLETE":
        raise RuntimeError(f"KB ingestion failed: {ing.get('failureReasons', ing['status'])}")
    common.log(f"ingested into KB {kb_id}")


if __name__ == "__main__":
    main()
