FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.53.0 boto3==1.43.97 httpx==0.28.1 'PyJWT[crypto]==2.14.0' \
    && useradd --uid 1000 --create-home app
WORKDIR /app
COPY common /app/common
COPY backend /app/backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
