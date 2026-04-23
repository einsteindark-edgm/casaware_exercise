FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --no-dev
# `tests/fakes/` contains FakeTemporalBackend used when TEMPORAL_MODE=fake and
# the dev JWT minter used by scripts/gen_test_jwt.py. Copied for dev images.
COPY tests ./tests
COPY scripts ./scripts

ENV PYTHONPATH=/app/src:/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "gunicorn", "nexus_backend.main:app", \
     "-w", "4", "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", "--access-logfile", "-"]
