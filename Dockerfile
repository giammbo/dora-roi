# Default 3.11: the floor, not the newest. pyproject declares
# requires-python >=3.11, so building on the floor means 3.12-only syntax fails
# here, in the container everybody runs, rather than surfacing later.
# CI overrides this to run the whole matrix through the same image definition —
# one way to build the project, not one for humans and another for the runner.
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS python-uv

RUN pip install --no-cache-dir uv

# The virtualenv lives OUTSIDE /app on purpose. compose bind-mounts the repo
# over /app so an edit is live without a rebuild, and a venv at /app/.venv
# would be shadowed by the host's macOS-built one — every command in the
# container would then run against the wrong interpreter, or none at all.
# /venv survives the mount.
ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/venv/bin:$PATH"
WORKDIR /app


# ---------------------------------------------------------------------------
# base — what the tool ships as: runtime dependencies only.
# ---------------------------------------------------------------------------
FROM python-uv AS base

# Two syncs, not one. The first installs ONLY the dependencies and stays
# cached until uv.lock changes; --no-install-project is required because the
# package cannot be built yet, src/ not being copied. The second, after the
# COPY, installs the package and with it the `dora-roi` console script.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# README.md and LICENSE are both declared in pyproject.toml (`readme` and
# `license-files`), so the second sync — the one that actually builds the
# package — fails without them. They sit below the dependency layer so that
# editing the README does not reinstall every dependency.
COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen

ENTRYPOINT ["dora-roi"]
CMD ["--help"]


# ---------------------------------------------------------------------------
# dev — the image every check runs in: pytest, ruff, mypy.
# Built from python-uv rather than from base so its dependency layer, which
# includes the dev extra, caches independently of the runtime one.
# ---------------------------------------------------------------------------
FROM python-uv AS dev

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra dev --extra aws --extra k8s
COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --extra dev --extra aws --extra k8s
COPY tests/ ./tests/

# No ENTRYPOINT: `docker compose run --rm dev <anything>` has to work for
# ruff and mypy too, not just the default below.
CMD ["pytest"]
