#!/usr/bin/env python3
"""
Provision the whole SimpleCover health-insurance demo on AWS with boto3.

Creates: S3 bucket + `kb_docs/` uploads, IAM roles, tools Lambda, an S3 Vectors
vector store (falls back to OpenSearch Serverless if S3 Vectors is unavailable),
Bedrock Knowledge Base + ingestion, the Bedrock Agent (+ action group + KB
association + alias), and a DynamoDB memory table. Records everything to
config.STATE_FILE so teardown.py can remove it.

Run:  python infra/provision.py
Idempotency is best-effort: most creates tolerate "already exists". If a run
fails partway, re-run after fixing, or run teardown.py and start clean.
"""
import io
import json
import os
import time
import zipfile

import botocore

import common
import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DOCS_DIR = os.path.join(REPO, "kb_docs")
HANDLER_PATH = os.path.join(REPO, "lambdas", "insurance_tools", "handler.py")
INSTRUCTIONS_PATH = os.path.join(REPO, "agent", "instructions.txt")
FUNCTIONS_PATH = os.path.join(REPO, "agent", "action_group_functions.json")

AGENT_ROLE_NAME = "AmazonBedrockExecutionRoleForAgents_" + config.PREFIX  # required prefix
KB_SELECTION = (
    "Use this knowledge base for questions about specific policy inclusions, "
    "tiers, waiting periods, rebates, MLS/LHC, and how SimpleCover operates"
)


def main():
    c = common.clients()
    acct = common.account_id(c)
    state = common.load_state()
    state.update({"region": config.REGION, "account": acct, "prefix": config.PREFIX})
    common.save_state(state)

    try:
        step_s3(c, state)
        step_iam(c, acct, state)
        step_lambda(c, acct, state)
        if _s3_vectors_available(c):
            step_s3_vectors(c, acct, state)
        else:
            common.log("S3 Vectors unavailable in this account/region — using OpenSearch Serverless")
            step_opensearch(c, acct, state)
        step_kb(c, acct, state)
        step_agent(c, acct, state)
        step_memory_table(c, state)
    finally:
        common.save_state(state)

    common.log("DONE. Agent is ready.")
    common.log(f"  agentId      = {state.get('agent_id')}")
    common.log(f"  agentAliasId = {state.get('agent_alias_id')}")
    common.log("Try it:  python chat.py")


# ---------------------------------------------------------------------------
def step_s3(c, state):
    common.log("S3: bucket + KB docs")
    b = config.S3_BUCKET
    try:
        if config.REGION == "us-east-1":
            c["s3"].create_bucket(Bucket=b)
        else:
            c["s3"].create_bucket(
                Bucket=b,
                CreateBucketConfiguration={"LocationConstraint": config.REGION},
            )
        common.log(f"  created bucket {b}")
    except c["s3"].exceptions.BucketAlreadyOwnedByYou:
        common.log(f"  bucket {b} already exists")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "BucketAlreadyOwnedByYou":
            common.log(f"  bucket {b} already exists")
        elif code == "BucketAlreadyExists":
            # Global name taken by another account — suffix with this account id.
            acct = common.account_id(c)
            b = f"{config.PREFIX}-kb-docs-{acct}"
            config.S3_BUCKET = b
            common.log(f"  default bucket name taken; using {b}")
            try:
                if config.REGION == "us-east-1":
                    c["s3"].create_bucket(Bucket=b)
                else:
                    c["s3"].create_bucket(
                        Bucket=b,
                        CreateBucketConfiguration={"LocationConstraint": config.REGION},
                    )
                common.log(f"  created bucket {b}")
            except c["s3"].exceptions.BucketAlreadyOwnedByYou:
                common.log(f"  bucket {b} already exists")
        else:
            raise
    for fn in sorted(os.listdir(KB_DOCS_DIR)):
        if fn.endswith(".md"):
            c["s3"].upload_file(os.path.join(KB_DOCS_DIR, fn), b, fn)
    common.log(f"  uploaded {len(os.listdir(KB_DOCS_DIR))} docs")
    state["s3_bucket"] = b


# ---------------------------------------------------------------------------
def _create_role(c, name, trust, desc):
    try:
        r = c["iam"].create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=desc,
        )
        common.log(f"  created role {name}")
        return r["Role"]["Arn"]
    except c["iam"].exceptions.EntityAlreadyExistsException:
        common.log(f"  role {name} already exists")
        return c["iam"].get_role(RoleName=name)["Role"]["Arn"]


def _put_inline(c, role, policy_name, doc):
    c["iam"].put_role_policy(
        RoleName=role, PolicyName=policy_name, PolicyDocument=json.dumps(doc)
    )


def step_iam(c, acct, state):
    common.log("IAM: roles")
    region = config.REGION

    # --- Lambda execution role (only needs CloudWatch logs) ---
    lambda_trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
         "Action": "sts:AssumeRole"}]}
    lambda_role_arn = _create_role(c, config.LAMBDA_ROLE, lambda_trust, "SimpleCover demo tools lambda")
    c["iam"].attach_role_policy(
        RoleName=config.LAMBDA_ROLE,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    state["lambda_role"] = config.LAMBDA_ROLE
    state["lambda_role_arn"] = lambda_role_arn

    # --- Knowledge Base role ---
    kb_trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"},
         "Action": "sts:AssumeRole"}]}
    kb_role_arn = _create_role(c, config.KB_ROLE, kb_trust, "SimpleCover demo KB role")
    embed_arn = f"arn:aws:bedrock:{region}::foundation-model/{config.EMBED_MODEL_ID}"
    _put_inline(c, config.KB_ROLE, "kb-perms", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": [embed_arn]},
        {"Effect": "Allow", "Action": ["aoss:APIAccessAll"],
         "Resource": [f"arn:aws:aoss:{region}:{acct}:collection/*"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{config.S3_BUCKET}",
                      f"arn:aws:s3:::{config.S3_BUCKET}/*"]},
        {"Effect": "Allow",
         "Action": [
             "s3vectors:GetIndex", "s3vectors:QueryVectors", "s3vectors:GetVectors",
             "s3vectors:PutVectors", "s3vectors:DeleteVectors", "s3vectors:ListVectors",
             "s3vectors:GetVectorBucket", "s3vectors:ListIndexes",
         ],
         "Resource": [
             f"arn:aws:s3vectors:{region}:{acct}:bucket/{config.S3_VECTOR_BUCKET}",
             f"arn:aws:s3vectors:{region}:{acct}:bucket/{config.S3_VECTOR_BUCKET}/*",
         ]},
    ]})
    state["kb_role"] = config.KB_ROLE
    state["kb_role_arn"] = kb_role_arn

    # --- Agent role (name MUST start with AmazonBedrockExecutionRoleForAgents_) ---
    agent_trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"},
         "Action": "sts:AssumeRole",
         "Condition": {"StringEquals": {"aws:SourceAccount": acct}}}]}
    agent_role_arn = _create_role(c, AGENT_ROLE_NAME, agent_trust, "SimpleCover demo agent role")
    # Inference profiles (au.*/us.*/apac.*) invoke FMs in more than one region.
    _put_inline(c, AGENT_ROLE_NAME, "agent-perms", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile"],
         "Resource": [f"arn:aws:bedrock:*::foundation-model/*",
                      f"arn:aws:bedrock:*:{acct}:inference-profile/*",
                      f"arn:aws:bedrock:*:{acct}:application-inference-profile/*"]},
        {"Effect": "Allow", "Action": ["bedrock:Retrieve"],
         "Resource": [f"arn:aws:bedrock:{region}:{acct}:knowledge-base/*"]},
    ]})
    state["agent_role"] = AGENT_ROLE_NAME
    state["agent_role_arn"] = agent_role_arn

    common.log("  waiting 12s for IAM propagation …")
    time.sleep(12)


# ---------------------------------------------------------------------------
def _zip_handler():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HANDLER_PATH, "handler.py")
    buf.seek(0)
    return buf.read()


def step_lambda(c, acct, state):
    common.log("Lambda: tools function")
    code = _zip_handler()
    try:
        r = c["lambda"].create_function(
            FunctionName=config.LAMBDA_NAME,
            Runtime="python3.12",
            Role=state["lambda_role_arn"],
            Handler="handler.lambda_handler",
            Code={"ZipFile": code},
            Timeout=30,
            MemorySize=256,
            Description="SimpleCover demo mock insurance tools",
        )
        arn = r["FunctionArn"]
        common.log(f"  created {config.LAMBDA_NAME}")
    except c["lambda"].exceptions.ResourceConflictException:
        c["lambda"].update_function_code(FunctionName=config.LAMBDA_NAME, ZipFile=code)
        arn = c["lambda"].get_function(FunctionName=config.LAMBDA_NAME)["Configuration"]["FunctionArn"]
        common.log(f"  updated existing {config.LAMBDA_NAME}")
    state["lambda_name"] = config.LAMBDA_NAME
    state["lambda_arn"] = arn


# ---------------------------------------------------------------------------
def _normalize_caller_arn(c, acct):
    arn = c["sts"].get_caller_identity()["Arn"]
    # Convert an assumed-role STS arn into the underlying IAM role arn so it is a
    # valid principal for an OpenSearch Serverless data-access policy.
    if ":assumed-role/" in arn:
        role_name = arn.split(":assumed-role/")[1].split("/")[0]
        return f"arn:aws:iam::{acct}:role/{role_name}"
    return arn


def _s3_vectors_available(c) -> bool:
    s3v = c.get("s3vectors")
    if s3v is None:
        return False
    try:
        s3v.list_vector_buckets(maxResults=1)
        return True
    except Exception as e:  # noqa: BLE001
        common.log(f"  S3 Vectors probe failed ({e})")
        return False


def _s3v_index_arn(s3v, bucket, index, acct):
    try:
        info = s3v.get_index(vectorBucketName=bucket, indexName=index).get("index") or {}
        if info.get("indexArn"):
            return info["indexArn"]
    except Exception:  # noqa: BLE001
        pass
    try:
        for idx in s3v.list_indexes(vectorBucketName=bucket).get("indexes", []):
            if idx.get("indexName") == index and idx.get("indexArn"):
                return idx["indexArn"]
    except Exception:  # noqa: BLE001
        pass
    return (
        f"arn:aws:s3vectors:{config.REGION}:{acct}:bucket/{bucket}/index/{index}"
    )


def step_s3_vectors(c, acct, state):
    """Create the S3 Vectors bucket + index used by the Knowledge Base."""
    common.log("S3 Vectors: bucket + index")
    s3v = c["s3vectors"]
    bucket = config.S3_VECTOR_BUCKET
    index = config.S3_VECTOR_INDEX
    try:
        s3v.create_vector_bucket(vectorBucketName=bucket)
        common.log(f"  created vector bucket {bucket}")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "BucketAlreadyExists", "AlreadyExists"):
            common.log(f"  vector bucket {bucket} already exists")
        else:
            raise
    info = s3v.get_vector_bucket(vectorBucketName=bucket).get("vectorBucket") or {}
    bucket_arn = info.get("vectorBucketArn") or (
        f"arn:aws:s3vectors:{config.REGION}:{acct}:bucket/{bucket}"
    )
    try:
        s3v.create_index(
            vectorBucketName=bucket,
            indexName=index,
            dimension=config.EMBED_DIMENSIONS,
            dataType="float32",
            distanceMetric="cosine",
            metadataConfiguration={
                "nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT"],
            },
        )
        common.log(f"  created vector index {index}")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "AlreadyExists"):
            common.log(f"  vector index {index} already exists")
        else:
            raise
    index_arn = _s3v_index_arn(s3v, bucket, index, acct)
    state["vector_store"] = "s3_vectors"
    state["s3_vector_bucket"] = bucket
    state["s3_vector_index"] = index
    state["s3_vector_bucket_arn"] = bucket_arn
    state["s3_vector_index_arn"] = index_arn
    common.log(f"  index arn {index_arn}")
    common.log("  waiting 8s for S3 Vectors IAM/index to settle …")
    time.sleep(8)


# ---------------------------------------------------------------------------
def step_opensearch(c, acct, state):
    common.log("OpenSearch Serverless: policies + collection + index")
    state["vector_store"] = "opensearch_serverless"
    coll = config.OSS_COLLECTION
    aoss = c["aoss"]

    enc = {"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{coll}"]}],
           "AWSOwnedKey": True}
    net = [{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{coll}"]},
                      {"ResourceType": "dashboard", "Resource": [f"collection/{coll}"]}],
            "AllowFromPublic": True}]
    caller = _normalize_caller_arn(c, acct)
    dap = [{"Rules": [
        {"ResourceType": "index", "Resource": [f"index/{coll}/*"], "Permission": ["aoss:*"]},
        {"ResourceType": "collection", "Resource": [f"collection/{coll}"], "Permission": ["aoss:*"]}],
        "Principal": [state["kb_role_arn"], caller]}]

    for kind, name, doc in [
        ("encryption", f"{config.PREFIX}-enc", json.dumps(enc)),
        ("network", f"{config.PREFIX}-net", json.dumps(net)),
    ]:
        try:
            aoss.create_security_policy(name=name, type=kind, policy=doc)
            common.log(f"  created {kind} policy {name}")
        except aoss.exceptions.ConflictException:
            common.log(f"  {kind} policy {name} already exists")
    # Data-access policies use a separate API (not CreateSecurityPolicy).
    try:
        aoss.create_access_policy(
            name=f"{config.PREFIX}-dap", type="data", policy=json.dumps(dap)
        )
        common.log(f"  created data policy {config.PREFIX}-dap")
    except aoss.exceptions.ConflictException:
        common.log(f"  data policy {config.PREFIX}-dap already exists")
    state["oss_policies"] = {
        "encryption": f"{config.PREFIX}-enc",
        "network": f"{config.PREFIX}-net",
        "data": f"{config.PREFIX}-dap",
    }

    # collection
    try:
        aoss.create_collection(name=coll, type="VECTORSEARCH",
                               description="SimpleCover demo vector store")
        common.log(f"  creating collection {coll}")
    except aoss.exceptions.ConflictException:
        common.log(f"  collection {coll} already exists")

    def _coll_ready():
        r = aoss.batch_get_collection(names=[coll])["collectionDetails"]
        if not r:
            return False, "pending"
        d = r[0]
        return (d["status"] == "ACTIVE", d["status"])

    common.wait_until(_coll_ready, f"collection {coll}", timeout=600, interval=15)
    det = aoss.batch_get_collection(names=[coll])["collectionDetails"][0]
    state["oss_collection"] = coll
    state["oss_collection_arn"] = det["arn"]
    state["oss_collection_id"] = det["id"]
    endpoint = det["collectionEndpoint"]

    _create_index(endpoint, acct)
    state["oss_index"] = config.OSS_INDEX
    common.log("  waiting 45s for index to settle before KB creation …")
    time.sleep(45)


def _create_index(endpoint, acct):
    from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
    import boto3

    host = endpoint.replace("https://", "")
    creds = boto3.Session(region_name=config.REGION).get_credentials()
    auth = AWSV4SignerAuth(creds, config.REGION, "aoss")
    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth, use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection, timeout=60,
    )
    body = {
        "settings": {"index": {"knn": True}},
        "mappings": {"properties": {
            config.VECTOR_FIELD: {
                "type": "knn_vector", "dimension": config.EMBED_DIMENSIONS,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"}},
            config.TEXT_FIELD: {"type": "text"},
            config.METADATA_FIELD: {"type": "text", "index": False},
        }},
    }
    # Index create/exists can 403 briefly after data-access policy creation; retry.
    for attempt in range(8):
        try:
            if client.indices.exists(index=config.OSS_INDEX):
                common.log(f"  index {config.OSS_INDEX} already exists")
                return
            client.indices.create(index=config.OSS_INDEX, body=body)
            common.log(f"  created index {config.OSS_INDEX}")
            return
        except Exception as e:  # noqa: BLE001
            common.log(f"  index create retry {attempt+1}/8 ({e})")
            time.sleep(15)
    raise RuntimeError("Failed to create OpenSearch index after retries")


# ---------------------------------------------------------------------------
def step_kb(c, acct, state):
    common.log("Bedrock Knowledge Base: create + ingest kb_docs/")
    ba = c["bedrock_agent"]
    embed_arn = f"arn:aws:bedrock:{config.REGION}::foundation-model/{config.EMBED_MODEL_ID}"
    use_s3v = state.get("vector_store") == "s3_vectors" or bool(state.get("s3_vector_index_arn"))

    # Existing OpenSearch KB can't be converted in place — create a new S3 Vectors KB.
    if use_s3v and state.get("kb_id") and state.get("kb_vector_store") != "s3_vectors":
        state["legacy_kb_id"] = state["kb_id"]
        state["legacy_data_source_id"] = state.get("data_source_id")
        common.log(f"  migrating off OSS KB {state['legacy_kb_id']} → S3 Vectors")
        state.pop("kb_id", None)
        state.pop("data_source_id", None)
        state["kb_associated"] = False

    if state.get("kb_id"):
        common.log(f"  KB {state['kb_id']} already recorded, skipping create")
    elif use_s3v:
        kb_name = config.KB_NAME if not state.get("legacy_kb_id") else f"{config.PREFIX}-s3v-kb"
        r = ba.create_knowledge_base(
            name=kb_name,
            description=KB_SELECTION,
            roleArn=state["kb_role_arn"],
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {
                    "embeddingModelArn": embed_arn,
                    "embeddingModelConfiguration": {
                        "bedrockEmbeddingModelConfiguration": {
                            "dimensions": config.EMBED_DIMENSIONS,
                            "embeddingDataType": "FLOAT32",
                        }
                    },
                },
            },
            storageConfiguration={
                "type": "S3_VECTORS",
                "s3VectorsConfiguration": {
                    "indexArn": state["s3_vector_index_arn"],
                },
            },
        )
        state["kb_id"] = r["knowledgeBase"]["knowledgeBaseId"]
        state["kb_vector_store"] = "s3_vectors"
        common.save_state(state)
        common.log(f"  created S3 Vectors KB {state['kb_id']}")
    else:
        state["vector_store"] = "opensearch_serverless"
        r = ba.create_knowledge_base(
            name=config.KB_NAME,
            description=KB_SELECTION,
            roleArn=state["kb_role_arn"],
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": embed_arn},
            },
            storageConfiguration={
                "type": "OPENSEARCH_SERVERLESS",
                "opensearchServerlessConfiguration": {
                    "collectionArn": state["oss_collection_arn"],
                    "vectorIndexName": config.OSS_INDEX,
                    "fieldMapping": {
                        "vectorField": config.VECTOR_FIELD,
                        "textField": config.TEXT_FIELD,
                        "metadataField": config.METADATA_FIELD,
                    },
                },
            },
        )
        state["kb_id"] = r["knowledgeBase"]["knowledgeBaseId"]
        state["kb_vector_store"] = "opensearch_serverless"
        common.save_state(state)
        common.log(f"  created OpenSearch KB {state['kb_id']}")

    def _kb_ready():
        s = ba.get_knowledge_base(knowledgeBaseId=state["kb_id"]
                                  )["knowledgeBase"]["status"]
        return (s in ("ACTIVE", "FAILED"), s)

    common.wait_until(_kb_ready, "KB status", timeout=300, interval=10)
    kb_status = ba.get_knowledge_base(knowledgeBaseId=state["kb_id"])["knowledgeBase"]["status"]
    if kb_status != "ACTIVE":
        raise RuntimeError(f"Knowledge Base ended in status {kb_status}")

    if not state.get("data_source_id"):
        ds = ba.create_data_source(
            knowledgeBaseId=state["kb_id"],
            name=f"{config.PREFIX}-s3",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {"bucketArn": f"arn:aws:s3:::{state.get('s3_bucket') or config.S3_BUCKET}"},
            },
        )
        state["data_source_id"] = ds["dataSource"]["dataSourceId"]
        common.save_state(state)
        common.log(f"  created data source {state['data_source_id']}")

    def _ds_ready():
        s = ba.get_data_source(knowledgeBaseId=state["kb_id"],
                               dataSourceId=state["data_source_id"]
                               )["dataSource"]["status"]
        return (s in ("AVAILABLE", "FAILED"), s)

    common.wait_until(_ds_ready, "data source", timeout=180, interval=8)
    ds_status = ba.get_data_source(knowledgeBaseId=state["kb_id"],
                                   dataSourceId=state["data_source_id"]
                                   )["dataSource"]["status"]
    if ds_status != "AVAILABLE":
        raise RuntimeError(f"Data source ended in status {ds_status}")

    job = ba.start_ingestion_job(
        knowledgeBaseId=state["kb_id"], dataSourceId=state["data_source_id"])
    job_id = job["ingestionJob"]["ingestionJobId"]

    def _ing_ready():
        j = ba.get_ingestion_job(knowledgeBaseId=state["kb_id"],
                                 dataSourceId=state["data_source_id"],
                                 ingestionJobId=job_id)["ingestionJob"]
        s = j["status"]
        return (s in ("COMPLETE", "FAILED"), s)

    common.wait_until(_ing_ready, "KB ingestion", timeout=600, interval=15)
    ing = ba.get_ingestion_job(knowledgeBaseId=state["kb_id"],
                               dataSourceId=state["data_source_id"],
                               ingestionJobId=job_id)["ingestionJob"]
    if ing["status"] != "COMPLETE":
        raise RuntimeError(f"KB ingestion failed: {ing.get('failureReasons', ing['status'])}")
    common.log(f"  ingested kb_docs into KB {state['kb_id']}")


# ---------------------------------------------------------------------------
def step_agent(c, acct, state):
    common.log("Bedrock Agent: create + action group + KB + alias")
    ba = c["bedrock_agent"]
    with open(INSTRUCTIONS_PATH) as f:
        instructions = f.read()
    with open(FUNCTIONS_PATH) as f:
        functions = json.load(f)["functions"]

    if not state.get("agent_id"):
        r = ba.create_agent(
            agentName=config.AGENT_NAME,
            agentResourceRoleArn=state["agent_role_arn"],
            foundationModel=config.AGENT_MODEL_ID,
            instruction=instructions,
            idleSessionTTLInSeconds=1800,
        )
        state["agent_id"] = r["agent"]["agentId"]
        common.save_state(state)
        common.log(f"  created agent {state['agent_id']}")

    agent_id = state["agent_id"]
    agent_arn = f"arn:aws:bedrock:{config.REGION}:{acct}:agent/{agent_id}"
    state["agent_arn"] = agent_arn

    def _agent_status(want):
        def _f():
            s = ba.get_agent(agentId=agent_id)["agent"]["agentStatus"]
            return (s in want, s)
        return _f

    common.wait_until(_agent_status({"NOT_PREPARED", "PREPARED"}),
                      "agent ready for edits", timeout=300, interval=10)

    ba.update_agent(
        agentId=agent_id,
        agentName=config.AGENT_NAME,
        agentResourceRoleArn=state["agent_role_arn"],
        foundationModel=config.AGENT_MODEL_ID,
        instruction=instructions,
        idleSessionTTLInSeconds=1800,
    )
    common.log("  updated agent instructions / model")
    common.wait_until(_agent_status({"NOT_PREPARED", "PREPARED"}),
                      "agent ready after update", timeout=300, interval=10)

    # Lambda resource policy so Bedrock can invoke it (needs agent arn).
    try:
        c["lambda"].add_permission(
            FunctionName=state["lambda_name"],
            StatementId="bedrock-agent-invoke",
            Action="lambda:InvokeFunction",
            Principal="bedrock.amazonaws.com",
            SourceArn=agent_arn,
        )
        common.log("  added Lambda invoke permission for Bedrock")
    except c["lambda"].exceptions.ResourceConflictException:
        common.log("  Lambda invoke permission already present")

    # Action group
    if not state.get("action_group_id"):
        ag = ba.create_agent_action_group(
            agentId=agent_id, agentVersion="DRAFT",
            actionGroupName=config.ACTION_GROUP,
            actionGroupExecutor={"lambda": state["lambda_arn"]},
            functionSchema={"functions": functions},
            actionGroupState="ENABLED",
        )
        state["action_group_id"] = ag["agentActionGroup"]["actionGroupId"]
        common.save_state(state)
        common.log(f"  created action group {state['action_group_id']}")
    else:
        ba.update_agent_action_group(
            agentId=agent_id, agentVersion="DRAFT",
            actionGroupId=state["action_group_id"],
            actionGroupName=config.ACTION_GROUP,
            actionGroupExecutor={"lambda": state["lambda_arn"]},
            functionSchema={"functions": functions},
            actionGroupState="ENABLED",
        )
        common.log("  updated action group")

    # Associate KB (disassociate a leftover OpenSearch KB after S3 Vectors migrate)
    legacy = state.get("legacy_kb_id")
    if legacy and legacy != state.get("kb_id"):
        try:
            ba.disassociate_agent_knowledge_base(
                agentId=agent_id, agentVersion="DRAFT", knowledgeBaseId=legacy)
            common.log(f"  disassociated legacy KB {legacy}")
        except botocore.exceptions.ClientError as e:
            common.log(f"  disassociate legacy KB: {e.response['Error']['Code']}")
        state["kb_associated"] = False

    if not state.get("kb_associated"):
        try:
            ba.associate_agent_knowledge_base(
                agentId=agent_id, agentVersion="DRAFT",
                knowledgeBaseId=state["kb_id"],
                description=KB_SELECTION,
                knowledgeBaseState="ENABLED",
            )
            state["kb_associated"] = True
            common.save_state(state)
            common.log("  associated knowledge base")
        except botocore.exceptions.ClientError as e:
            if "already" not in str(e).lower():
                raise
            state["kb_associated"] = True
    else:
        try:
            ba.update_agent_knowledge_base(
                agentId=agent_id, agentVersion="DRAFT",
                knowledgeBaseId=state["kb_id"],
                description=KB_SELECTION,
                knowledgeBaseState="ENABLED",
            )
            common.log("  updated knowledge base association")
        except botocore.exceptions.ClientError as e:
            common.log(f"  update KB association: {e.response['Error']['Code']}")

    # Prepare
    ba.prepare_agent(agentId=agent_id)
    common.wait_until(_agent_status({"PREPARED"}), "agent prepared",
                      timeout=300, interval=10)

    # Alias — create once, then retarget at the newly prepared DRAFT
    if not state.get("agent_alias_id"):
        al = ba.create_agent_alias(agentId=agent_id, agentAliasName=config.AGENT_ALIAS)
        state["agent_alias_id"] = al["agentAlias"]["agentAliasId"]
        common.save_state(state)
        common.log(f"  created alias {state['agent_alias_id']}")
    else:
        ba.update_agent_alias(
            agentId=agent_id,
            agentAliasId=state["agent_alias_id"],
            agentAliasName=config.AGENT_ALIAS,
        )
        common.log(f"  updated alias {state['agent_alias_id']} to latest prepared version")

    def _alias_ready():
        s = ba.get_agent_alias(agentId=agent_id,
                               agentAliasId=state["agent_alias_id"]
                               )["agentAlias"]["agentAliasStatus"]
        return (s == "PREPARED", s)

    common.wait_until(_alias_ready, "alias prepared", timeout=300, interval=10)


# ---------------------------------------------------------------------------
def step_memory_table(c, state):
    common.log("DynamoDB: memory table")
    d = c["dynamodb"]
    try:
        d.create_table(
            TableName=config.MEMORY_TABLE,
            KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        common.log(f"  creating {config.MEMORY_TABLE}")
    except d.exceptions.ResourceInUseException:
        common.log(f"  table {config.MEMORY_TABLE} already exists")
    d.get_waiter("table_exists").wait(TableName=config.MEMORY_TABLE)
    state["memory_table"] = config.MEMORY_TABLE


if __name__ == "__main__":
    main()
