FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
ARG AGENT_SOURCE=agents/hermes
COPY ${AGENT_SOURCE} /app/adapter
RUN sh /app/adapter/install.sh

FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap libseccomp2 git curl ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 1000 --create-home app
COPY --from=builder /opt /opt
COPY --from=builder /app/adapter /app/adapter
# The supervisor never uses the agent's dependency environment. This environment
# is outside /opt and is not mounted into the sandbox.
COPY runtime/requirements.txt /tmp/harness-requirements.txt
RUN python -m venv /harness-venv \
    && /harness-venv/bin/pip install --no-cache-dir -r /tmp/harness-requirements.txt
WORKDIR /app
COPY common /app/common
COPY runtime /app/runtime
ENV PATH=/harness-venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    AGENT_ADAPTER_MANIFEST=/app/adapter/manifest.json
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "runtime.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
