# Helvetra Backend Dockerfile
# Multi-stage build for smaller production image.

FROM python:3.11-slim as base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install dependencies
FROM base as deps

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Production image
FROM base as production

# Run as a non-root user — limits container-escape blast radius if a code-
# exec bug ever lands inside uvicorn or a dep (helvetra/infra#10).
RUN useradd --system --no-create-home --shell /usr/sbin/nologin app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY --chown=app:app app ./app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini .

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
