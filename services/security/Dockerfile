# Security tools image: the conformance suite's runner (scripts/run-test-plan.py and the two
# modules it imports, pinned by commit and SHA-256 in the Makefile and passed as the named
# context `oidf`: make security-image) and this package. Dependencies from uv.lock only.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
COPY --from=build /app/.venv /app/.venv
COPY --from=oidf run-test-plan.py conformance.py test_plan_parser.py /app/oidf/
# run-test-plan.py reads certs-keys/ beside it (placeholders of other plans' configurations).
RUN mkdir /app/oidf/certs-keys
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 OIDF_SCRIPTS=/app/oidf
USER 65532
CMD ["python", "-m", "security_tools"]
