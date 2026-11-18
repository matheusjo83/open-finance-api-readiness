# Contract validator image. Dependencies from uv.lock only (no dev group).
# Build context: services/validator, plus the pinned specs as the named context `specs`
# (make validator-image: --build-context specs=specs/upstream). The specs are baked into the
# local image only; they are not redistributed (the upstream repository declares no license).
# --frozen, not --locked: the lock also names the proxy and the transmitter (dev-only path
# dependencies, outside this context); make validator-test checks it.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
COPY --from=build /app/.venv /app/.venv
COPY --from=specs consents-3.3.1.yml accounts-2.4.2.yml /app/specs/
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 SPECS_DIR=/app/specs
USER 65532
CMD ["python", "-m", "contract_validator"]
