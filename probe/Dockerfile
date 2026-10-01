FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS dependencies
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /mnt/agents && chown 1000:1000 /mnt/agents
COPY --from=dependencies /install /usr/local
WORKDIR /app
COPY . /app/probe
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PROBE_ROOT=/mnt/agents
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "probe.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
