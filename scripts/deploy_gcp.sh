#!/usr/bin/env bash
# Deploy SimpleCover (frontend + FastAPI + Bedrock client) to Cloud Run.
# Reads Datadog keys from ../.env and AWS keys from ~/.aws/credentials (never prints them).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-australia-southeast1}"
SERVICE="${GCP_SERVICE:-simple-cover}"
SECRET_PREFIX="${SECRET_PREFIX:-simple-cover}"

if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "Set a GCP project: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

ENV_FILE="${ROOT}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

python3 - "${ROOT}" "${PROJECT}" "${SECRET_PREFIX}" <<'PY'
import configparser, subprocess, sys
from pathlib import Path

root, project, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
env = {}
for line in (Path(root) / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

aws = configparser.ConfigParser()
aws.read(Path.home() / ".aws/credentials")
if "default" not in aws:
    sys.exit("No [default] profile in ~/.aws/credentials")
env["AWS_ACCESS_KEY_ID"] = aws["default"]["aws_access_key_id"]
env["AWS_SECRET_ACCESS_KEY"] = aws["default"]["aws_secret_access_key"]

needed = {
    f"{prefix}-dd-api-key": env.get("DD_API_KEY"),
    f"{prefix}-dd-app-key": env.get("DD_APPLICATION_KEY") or env.get("DD_APP_KEY"),
    f"{prefix}-aws-access-key-id": env["AWS_ACCESS_KEY_ID"],
    f"{prefix}-aws-secret-access-key": env["AWS_SECRET_ACCESS_KEY"],
}
missing = [k for k, v in needed.items() if not v]
if missing:
    sys.exit("Missing secret values for: " + ", ".join(missing))

def secret_exists(name: str) -> bool:
    r = subprocess.run(
        ["gcloud", "secrets", "describe", name, "--project", project],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0

for name, value in needed.items():
    if not secret_exists(name):
        subprocess.run(
            ["gcloud", "secrets", "create", name, "--replication-policy=automatic",
             "--project", project],
            check=True,
        )
        print(f"created secret {name}", flush=True)
    subprocess.run(
        ["gcloud", "secrets", "versions", "add", name, "--project", project, "--data-file=-"],
        input=value.encode(),
        check=True,
    )
    print(f"added secret version {name}", flush=True)
PY

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for secret in \
  "${SECRET_PREFIX}-dd-api-key" \
  "${SECRET_PREFIX}-dd-app-key" \
  "${SECRET_PREFIX}-aws-access-key-id" \
  "${SECRET_PREFIX}-aws-secret-access-key"
do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --project="${PROJECT}" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
done
echo "granted secretAccessor to ${SA}"

gcloud run deploy "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --source="${ROOT}" \
  --allow-unauthenticated \
  --memory=1Gi \
  --cpu=1 \
  --timeout=120 \
  --concurrency=8 \
  --min-instances=0 \
  --max-instances=3 \
  --cpu-boost \
  --set-env-vars="AWS_REGION=ap-southeast-2,AWS_DEFAULT_REGION=ap-southeast-2,DD_SITE=datadoghq.com,DD_SERVICE=simple_cover_backend,DD_ENV=gcp,DD_VERSION=1.0,DD_LLMOBS_ENABLED=true,DD_LLMOBS_AGENTLESS_ENABLED=true,DD_LLMOBS_ML_APP=simplecover_assistant,DD_AI_GUARD_ENABLED=true,DD_TRACE_ENABLED=true,DD_LOGS_ENABLED=true,DD_SOURCE=python,OTEL_SDK_DISABLED=true" \
  --set-secrets="DD_API_KEY=${SECRET_PREFIX}-dd-api-key:latest,DD_APPLICATION_KEY=${SECRET_PREFIX}-dd-app-key:latest,DD_APP_KEY=${SECRET_PREFIX}-dd-app-key:latest,AWS_ACCESS_KEY_ID=${SECRET_PREFIX}-aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=${SECRET_PREFIX}-aws-secret-access-key:latest"

echo
gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --format='value(status.url)'
