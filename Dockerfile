# syntax=docker/dockerfile:1.7
# =============================================================================
#  TransForge - multi-stage container image
# =============================================================================
#  Build:   docker build -t transforge:dev .
#  Run:     docker compose -f deploy/compose/compose.yml run --rm app status --all
#
#  TransForge is a batch CLI (no HTTP surface): the image is a toolbox that is
#  run against a mounted site tree, talking to an LLM server on the Docker host.
#
#  Load-bearing notes (same pattern as the DisPatch Chat image):
#   * BUILDER and RUNTIME ship the same interpreter at the same path - the uv
#     venv holds an absolute python symlink. ghcr.io/astral-sh/uv:python3.13-
#     trixie-slim is built FROM python:3.13-slim-trixie.
#   * `--no-editable`: transforge.py and review.py are installed INTO the venv,
#     so the runtime stage needs only /app/.venv, no source tree.
#   * uv is NOT in the runtime image; tini comes from compose (`init: true`).
# =============================================================================


FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-editable

# README.md because pyproject.toml names it as the package readme.
# --reinstall-package transforge: never serve the project itself from the uv
# cache mount (belt and braces next to [tool.uv] cache-keys in pyproject.toml;
# a source-only change used to produce an image with the previous code).
COPY README.md LICENSE transforge.py review.py ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --reinstall-package transforge

RUN find /app/.venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /app/.venv -name '*.pyc' -delete


FROM python:3.13-slim-trixie AS runtime

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED

LABEL org.opencontainers.image.title="TransForge" \
      org.opencontainers.image.description="Config-driven website translator that runs against your own local LLM server" \
      org.opencontainers.image.source="https://github.com/LaserLloyd/TransForge" \
      org.opencontainers.image.documentation="https://github.com/LaserLloyd/TransForge/blob/main/docs/deploy/docker.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# tzdata so TZ= resolves; ca-certificates for the opt-in `review` stage (HTTPS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# UID/GID 1000: a bind-mounted site tree owned by the person running
# `docker compose` is then writable without further ceremony.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chmod=0755 deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --chown=app:app deploy/compose/config.example.toml /usr/local/share/transforge/config.example.toml

# TransForge hard-codes ~/.config/transforge/config.toml and
# ~/.local/state/transforge (transforge.py CONFIG_PATH / STATE_DIR), so HOME is
# /data and those two directories are the mount points:
#   /data/.config/transforge        config.toml      (bind mount: you edit it)
#   /data/.local/state/transforge   manifests+backups (named volume: never lose it)
#   /sites                          the site trees to translate (bind mount)
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data \
    IMAGE_VERSION=${VERSION}

# No `VOLUME`: an unmounted anonymous volume would hide data loss.
RUN install -d -o app -g app -m 0750 /data /data/.config /data/.config/transforge \
        /data/.local /data/.local/state /data/.local/state/transforge /data/tmp /sites

WORKDIR /sites
USER app

# No HEALTHCHECK here: this is a batch CLI with no HTTP surface. The optional
# `schedule` compose service carries its own heartbeat check.
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
