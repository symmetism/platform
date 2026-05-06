# syntax=docker/dockerfile:1.7
#
# attestation-service container (battle plan H2).
# Build context: REPO ROOT (Platform/) so we can copy
# `apps/attestation-service/` cleanly. Invoked by build-and-publish.yml
# (which we'll add to Platform's .github/workflows/ at H6 time).

FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY apps/attestation-service/pyproject.toml /build/pyproject.toml
# We don't need full install (no source yet); just dependency resolve.
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "httpx>=0.27" \
    "pydantic>=2.6"

FROM python:3.12-slim-bookworm AS runtime

ARG IMAGE_GIT_SHA="unknown"

LABEL org.opencontainers.image.title="attestation-service" \
      org.opencontainers.image.description="Symmetism public attestation service" \
      org.opencontainers.image.source="https://github.com/Symmetism/Platform" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${IMAGE_GIT_SHA}"

RUN groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --home /home/app --shell /sbin/nologin app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IMAGE_GIT_SHA="${IMAGE_GIT_SHA}"

WORKDIR /app
COPY --chown=app:app apps/attestation-service/src/ /app/src/
COPY --chown=app:app apps/attestation-service/static/ /app/static/

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "attestation.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--app-dir", "/app/src", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
