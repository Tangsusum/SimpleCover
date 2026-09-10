# SimpleCover: FastAPI + static frontend + Bedrock agent client.
# Cloud Run serves both on one origin (scale-to-zero).
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
COPY bedrock-health-agent/requirements.txt /app/bedrock-health-agent/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/backend/requirements.txt \
    -r /app/bedrock-health-agent/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY bedrock-health-agent /app/bedrock-health-agent

# Datadog in-container Agent (APM). LLM Obs is sent agentless.
# Pin >= 1.9.13 for LLM Obs / EVP-proxy fixes.
COPY --from=datadog/serverless-init:1.9.13 /datadog-init /app/datadog-init

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV DD_SERVICE=simple_cover_backend
ENV DD_SOURCE=python
WORKDIR /app/backend

ENTRYPOINT ["/app/datadog-init"]
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
