"""
Central configuration for the Bedrock health-insurance assistant prototype.

Everything is overridable via environment variables so the same code can be
reused unchanged when this is later wrapped in a Lambda. Defaults are chosen to
be the fastest-to-stand-up, widely-available options.
"""
import os

# --- AWS / region -----------------------------------------------------------
# Honor both AWS_REGION and the CLI default (AWS_DEFAULT_REGION / ~/.aws/config).
REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "ap-southeast-2"
)

# --- Naming (single prefix so teardown can find everything) ------------------
# AWS resource names use hyphens (S3 disallows underscores). Live deployments
# keep whatever prefix is already recorded in .deployment.json.
_STATE_FILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".deployment.json"
)


def _prefix_from_state():
    path = os.environ.get("SIMPLE_COVER_STATE_FILE", _STATE_FILE_DEFAULT)
    if os.path.exists(path):
        try:
            import json
            with open(path) as f:
                return json.load(f).get("prefix")
        except Exception:  # noqa: BLE001
            return None
    return None


PREFIX = (
    os.environ.get("SIMPLE_COVER_PREFIX")
    or _prefix_from_state()
    or "simple-cover-health-demo"
)

# --- Foundation models -------------------------------------------------------
# Agent reasoning model. Claude 3.5 Sonnet on-demand is EOL; Bedrock Agents need
# a current inference-profile id. Defaults follow the deploy region.
_AGENT_MODELS_BY_REGION = {
    "ap-southeast-2": "au.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us-east-1": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us-west-2": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
}
AGENT_MODEL_ID = os.environ.get(
    "SIMPLE_COVER_AGENT_MODEL_ID",
    _AGENT_MODELS_BY_REGION.get(REGION, "au.anthropic.claude-sonnet-4-5-20250929-v1:0"),
)

# Embeddings model for the knowledge base. Titan v2 = 1024 dimensions.
EMBED_MODEL_ID = os.environ.get("SIMPLE_COVER_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.environ.get("SIMPLE_COVER_EMBED_DIMENSIONS", "1024"))

# --- Derived resource names --------------------------------------------------
S3_BUCKET = os.environ.get("SIMPLE_COVER_S3_BUCKET", f"{PREFIX}-kb-docs")
S3_VECTOR_BUCKET = os.environ.get("SIMPLE_COVER_S3_VECTOR_BUCKET", f"{PREFIX}-vectors")
S3_VECTOR_INDEX = os.environ.get("SIMPLE_COVER_S3_VECTOR_INDEX", f"{PREFIX}-s3v-index")
OSS_COLLECTION = f"{PREFIX}-vs"          # OpenSearch Serverless fallback collection
OSS_INDEX = f"{PREFIX}-index"            # OSS vector index name
KB_NAME = f"{PREFIX}-kb"
AGENT_NAME = f"{PREFIX}-agent"
AGENT_ALIAS = os.environ.get("SIMPLE_COVER_AGENT_ALIAS", "demo")
ACTION_GROUP = "insurance_tools"
LAMBDA_NAME = f"{PREFIX}-tools"
MEMORY_TABLE = os.environ.get("SIMPLE_COVER_MEMORY_TABLE", f"{PREFIX}-memory")

# IAM role names
KB_ROLE = f"{PREFIX}-kb-role"
AGENT_ROLE = f"{PREFIX}-agent-role"
LAMBDA_ROLE = f"{PREFIX}-lambda-role"

# --- Metadata fields inside the OpenSearch vector index ----------------------
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"

# --- State file: where provision.py records what it created ------------------
# agent_client.py and teardown.py read this. Kept beside the repo root.
STATE_FILE = os.environ.get("SIMPLE_COVER_STATE_FILE", _STATE_FILE_DEFAULT)
