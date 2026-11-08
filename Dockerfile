# The image holds code and templates only. Secrets arrive at runtime as Secret Manager file mounts.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY studio ./studio
COPY templates ./templates
COPY web ./web
# Layout references for the model. The app has no route to this folder, so browsers can't reach it.
COPY references ./references

RUN useradd --system --no-create-home --uid 10001 studio
USER studio

ENV APP_ENV=production DATA_DIR=/tmp/studio
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn studio.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8080} --no-server-header --no-access-log --timeout-graceful-shutdown 20"]
