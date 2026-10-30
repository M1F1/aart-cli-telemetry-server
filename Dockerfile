# The server as a container image: SQLite on the /data volume by default, PostgreSQL when
# AART_CLI_TELEMETRY_DATABASE_URL names one. It runs as an unprivileged user and needs no writable
# path but /data.
#
# The image listens on 0.0.0.0:8000 only when its arguments include --behind-authenticating-proxy;
# without them it refuses to start, as `serve` does. Version 1 of the API carries no client
# credential, so the deployment must put an authenticating proxy or ingress in front.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true
RUN pip install poetry==2.4.1
WORKDIR /opt/aart-cli-telemetry-server
COPY pyproject.toml poetry.lock README.md LICENSE ./
RUN poetry install --only main --all-extras --no-root
COPY aart_cli_telemetry_server ./aart_cli_telemetry_server
RUN poetry build --format wheel \
    && .venv/bin/python -m pip install --no-deps dist/*.whl

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/aart-cli-telemetry-server/.venv/bin:$PATH \
    AART_CLI_TELEMETRY_DATA_DIR=/data
RUN useradd --system --uid 10001 --user-group --no-create-home --home-dir /nonexistent telemetry \
    && mkdir -p /data \
    && chown telemetry:telemetry /data \
    && chmod 0700 /data
COPY --from=build /opt/aart-cli-telemetry-server/.venv /opt/aart-cli-telemetry-server/.venv
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
ENTRYPOINT ["aart-cli-telemetry-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
