#!/usr/bin/env python3
"""
Tear down everything provision.py created, driven by config.STATE_FILE.

Run:  python infra/teardown.py
Best-effort: each delete is wrapped so a missing/partially-created resource
doesn't stop the rest. Safe to run more than once.
"""
import os
import time

import botocore

import common
import config


def _try(desc, fn):
    try:
        fn()
        common.log(f"  deleted {desc}")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket",
                    "ResourceNotFound", "NotFoundException"):
            common.log(f"  {desc}: already gone")
        else:
            common.log(f"  {desc}: {code} (skipping)")
    except Exception as e:  # noqa: BLE001
        common.log(f"  {desc}: {e} (skipping)")


def main():
    c = common.clients()
    s = common.load_state()
    if not s:
        common.log("No state file; nothing to tear down.")
        return
    ba = c["bedrock_agent"]

    common.log("Deleting Bedrock Agent + alias")
    if s.get("agent_id") and s.get("agent_alias_id"):
        _try("agent alias", lambda: ba.delete_agent_alias(
            agentId=s["agent_id"], agentAliasId=s["agent_alias_id"]))
    if s.get("agent_id"):
        _try("agent", lambda: ba.delete_agent(
            agentId=s["agent_id"], skipResourceInUseCheck=True))

    common.log("Deleting Knowledge Base")
    if s.get("kb_id") and s.get("data_source_id"):
        _try("data source", lambda: ba.delete_data_source(
            knowledgeBaseId=s["kb_id"], dataSourceId=s["data_source_id"]))
    if s.get("kb_id"):
        _try("knowledge base", lambda: ba.delete_knowledge_base(
            knowledgeBaseId=s["kb_id"]))
    if s.get("legacy_kb_id") and s.get("legacy_data_source_id"):
        _try("legacy data source", lambda: ba.delete_data_source(
            knowledgeBaseId=s["legacy_kb_id"], dataSourceId=s["legacy_data_source_id"]))
    if s.get("legacy_kb_id"):
        _try("legacy knowledge base", lambda: ba.delete_knowledge_base(
            knowledgeBaseId=s["legacy_kb_id"]))

    common.log("Deleting Guardrail")
    if s.get("guardrail_id"):
        _try("guardrail", lambda: c["bedrock"].delete_guardrail(
            guardrailIdentifier=s["guardrail_id"]))

    common.log("Deleting OpenSearch Serverless collection + policies")
    if s.get("oss_collection_id"):
        _try("collection", lambda: c["aoss"].delete_collection(id=s["oss_collection_id"]))
        time.sleep(5)
    for kind, name in (s.get("oss_policies") or {}).items():
        if kind == "data":
            _try(f"{kind} policy", lambda n=name:
                 c["aoss"].delete_access_policy(name=n, type="data"))
        else:
            _try(f"{kind} policy", lambda k=kind, n=name:
                 c["aoss"].delete_security_policy(name=n, type=k))

    common.log("Deleting S3 Vectors index + bucket")
    s3v = c.get("s3vectors")
    vec_bucket = s.get("s3_vector_bucket") or config.S3_VECTOR_BUCKET
    vec_index = s.get("s3_vector_index") or config.S3_VECTOR_INDEX
    if s3v and (s.get("s3_vector_bucket") or s.get("vector_store") == "s3_vectors"):
        _try("s3 vector index", lambda: s3v.delete_index(
            vectorBucketName=vec_bucket, indexName=vec_index))
        _try("s3 vector bucket", lambda: s3v.delete_vector_bucket(
            vectorBucketName=vec_bucket))

    common.log("Deleting Lambda")
    if s.get("lambda_name"):
        _try("lambda", lambda: c["lambda"].delete_function(FunctionName=s["lambda_name"]))

    common.log("Deleting DynamoDB memory table")
    if s.get("memory_table"):
        _try("memory table", lambda: c["dynamodb"].delete_table(TableName=s["memory_table"]))

    common.log("Deleting IAM roles")
    for role_key in ("lambda_role", "kb_role", "agent_role"):
        role = s.get(role_key)
        if role:
            _delete_role(c, role)

    common.log("Emptying + deleting S3 bucket")
    if s.get("s3_bucket"):
        _empty_bucket(c, s["s3_bucket"])
        _try("bucket", lambda: c["s3"].delete_bucket(Bucket=s["s3_bucket"]))

    # Remove state file
    if os.path.exists(config.STATE_FILE):
        os.remove(config.STATE_FILE)
        common.log(f"  removed {config.STATE_FILE}")
    common.log("Teardown complete.")


def _delete_role(c, role):
    iam = c["iam"]
    try:
        for p in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
        common.log(f"  deleted role {role}")
    except iam.exceptions.NoSuchEntityException:
        common.log(f"  role {role}: already gone")
    except Exception as e:  # noqa: BLE001
        common.log(f"  role {role}: {e} (skipping)")


def _empty_bucket(c, bucket):
    try:
        paginator = c["s3"].get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if objs:
                c["s3"].delete_objects(Bucket=bucket, Delete={"Objects": objs})
    except botocore.exceptions.ClientError:
        # Non-versioned bucket
        try:
            objs = c["s3"].list_objects_v2(Bucket=bucket).get("Contents", [])
            if objs:
                c["s3"].delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": o["Key"]} for o in objs]})
        except botocore.exceptions.ClientError:
            pass


if __name__ == "__main__":
    main()
