FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY tle ./tle
# Playwright's installer needs libatomic before it can install the rest of what Chromium needs,
# and the slim image no longer has it.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/* \
    && pip install . && playwright install --with-deps chromium

# Settings live in the database and secrets in Secret Manager, so nothing company-specific is in the image.

CMD exec uvicorn tle.api:app --host 0.0.0.0 --port ${PORT:-8080}
