FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY tle ./tle
RUN pip install . && playwright install --with-deps chromium

# Settings live in the database and secrets in Secret Manager, so nothing company-specific is in the image.

CMD exec uvicorn tle.api:app --host 0.0.0.0 --port ${PORT:-8080}
