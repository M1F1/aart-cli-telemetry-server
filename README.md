# aart-cli-telemetry-server

Receives artifact telemetry from [aart-cli](https://github.com/M1F1/aart-cli), stores it in a
database -- SQLite by default, PostgreSQL by URL -- and shows it on a dashboard in the browser: which artifacts are used, how often, by how many
installations, and how installs, updates and removals change over time.

- **The contract** between aart-cli and this server is [`docs/telemetry-api-v1.md`](docs/telemetry-api-v1.md)
  with [`contract/telemetry-api-v1.examples.json`](contract/telemetry-api-v1.examples.json). Both
  repositories keep identical copies and test against them. This server never imports aart-cli.
- **Storage** is behind a port. A batch is stored in one transaction and acknowledged only after it
  committed; an `event_id` already stored is acknowledged and never counted again. The default is
  one SQLite database in WAL mode, owner-only (0600); `AART_CLI_TELEMETRY_DATABASE_URL` selects any
  database SQLAlchemy reads, and the same repository runs on PostgreSQL.
- **The dashboard** loads nothing from outside the server. Chart.js 4.4.1 (MIT) is vendored in
  `aart_cli_telemetry_server/web/static/`, with its licence beside it.

## How it is built

A functional core with an imperative shell:

| Layer | What it is | May import |
|---|---|---|
| `domain` | The contract (pydantic, strict), events, and the dashboard's counts as pure functions. | pydantic, returns, pure stdlib |
| `application` | Use cases against ports: `EventRepository` and a clock. Failures are `returns` values. | `domain`, returns |
| `adapters` | `SqlEventRepository` on SQLAlchemy Core (SQLite, PostgreSQL); `InMemoryEventRepository`. | anything |
| `web` | FastAPI routes: read the request, call one use case, map the result to a status. | `application`, `domain` |
| `effects` | The one place a result leaves IO (`returns.unsafe`). | returns |
| `cli`, `asgi` | Composition roots: which database, which clock, which address. | everything |

`tach.toml` holds the edges between layers, `tests/test_architecture.py` the third-party imports.
Every repository is held to the pure definitions in `domain/stats.py` by property tests over
generated histories (`tests/test_repository_contract.py`), so a database that counts differently
fails there rather than on the dashboard.

## Run it locally

Needs Python 3.11 or newer and [Poetry](https://python-poetry.org/).

```bash
poetry install
poetry run aart-cli-telemetry-server serve --port 8000
```

Then open <http://127.0.0.1:8000/dashboard>.

The database is SQLite outside the source tree: in `$AART_CLI_TELEMETRY_DATA_DIR` when it is set,
else `$XDG_DATA_HOME/aart-cli-telemetry-server`, else `~/.local/share/aart-cli-telemetry-server`.
`$AART_CLI_TELEMETRY_DATABASE_URL` replaces it with any database URL, such as
`postgresql+psycopg://telemetry@db/telemetry` (install the `postgresql` extra for the driver); a
URL can carry a password, so it is never printed. `--data-dir` overrides both with SQLite there.
The schema is created in an empty database on first start; a database at another schema version,
or not created by this server, is refused and left as it is.

The application can also be served by any ASGI server, reading the same variables:

```bash
AART_CLI_TELEMETRY_DATA_DIR=/tmp/aart-cli-telemetry poetry run uvicorn aart_cli_telemetry_server.asgi:app --host 127.0.0.1 --port 8000
```

## Point aart-cli at it

```bash
aart-cli consume telemetry enable --endpoint http://127.0.0.1:8000
aart-cli consume telemetry status
aart-cli consume telemetry sync
```

`http://` is accepted only for a loopback address; anything else must be `https://`. See aart-cli's
consumer guide for what is sent and when.

## Listening beyond loopback

Version 1 of the API carries no client credential, so `serve` refuses any address other than
loopback unless `--behind-authenticating-proxy` says that a proxy or ingress in front authenticates
every request, to the ingest endpoint and to the dashboard alike. Never expose the server to the
Internet without one.

## Backup

```bash
poetry run aart-cli-telemetry-server backup /path/to/copy.sqlite3
```

takes a consistent copy with SQLite's online backup while the server runs. For PostgreSQL use
`pg_dump`.

## Endpoints

| Endpoint | |
|---|---|
| `POST /api/v1/events/batch` | The contract's ingest endpoint. |
| `GET /healthz` | `{"status": "ok"}` when the database can be read. |
| `GET /dashboard` | The dashboard. |
| `GET /api/v1/stats/summary?days=30` | Uses, installs, updates, removals, active installations. |
| `GET /api/v1/stats/top-artifacts?days=30&limit=20` | Artifacts by uses. |
| `GET /api/v1/stats/daily?days=30` | Per UTC day. |
| `GET /api/v1/stats/by-source?days=30` | Uses per harness and kind. |

## Development

```bash
poetry install --with dev
make quality
```

`make quality` is what CI runs: ruff (lint and format), mypy strict with the pydantic and returns
plugins, tach (layers and declared dependencies), and the tests under coverage (95% branch minimum).
Universal claims are Hypothesis properties. `make mutants` runs mutmut over `domain` and
`application`; it is advisory, as in aart-cli, and survivors are findings to read. On 2026-10-29 it
killed 379 of 399; the 20 left are equivalent: the wording of errors no reason repeats, the
`include_url`/`include_input` flags of errors whose input no reason reads, and a defensive fallback
no contract error reaches.

`tests/test_repository_contract.py` also runs against PostgreSQL when
`AART_CLI_TELEMETRY_TEST_POSTGRES_URL` names a database it may empty. Create that database with a
linguistic collation (CI uses ICU `en-US`): under the byte-order `C` collation some images default
to, the test that holds artifact ids in code-point order passes even without the explicit `COLLATE
"C"`. `tests/test_e2e_aart_cli.py` runs a real aart-cli against a real server when `AART_CLI_PYTHON`
names an interpreter that imports `aart_cli`, and is skipped otherwise.

## Container image

The `Dockerfile` builds an image for a container platform such as AKS or Azure Container Apps:

```bash
docker build -t aart-cli-telemetry-server .
```

- **The database is SQLite on the `/data` volume** unless `AART_CLI_TELEMETRY_DATABASE_URL` names
  another. The `postgresql` extra is installed, so a PostgreSQL URL works without a rebuild. Pass the
  URL as a secret, since it can carry a password.
- **It listens on `0.0.0.0:8000` only with `--behind-authenticating-proxy`** as the container's
  argument. Without it the container refuses to start, as `serve` does elsewhere, because a
  container's port is reachable from beyond its host.
- **It runs as uid 10001** with nothing writable but `/data`, so a read-only root filesystem works. A
  bind-mounted `/data` must be writable by that uid; a named volume is prepared by the image.
- **Health** is `GET /healthz` on port 8000; point the platform's liveness and readiness probes at
  it. The image's own `HEALTHCHECK` does the same for Docker.

To try it on this machine, publish the port to loopback only:

```bash
docker run --rm -p 127.0.0.1:8000:8000 -v aart-cli-telemetry:/data aart-cli-telemetry-server --behind-authenticating-proxy
```

SQLite needs a disk of its own: a block device such as an Azure Disk behind one replica. A network
share such as Azure Files, the storage Azure Container Apps and App Service mount, does not give
SQLite the locking WAL mode relies on. There, use PostgreSQL, such as Azure Database for PostgreSQL.
Run one replica on SQLite; several replicas need PostgreSQL.

A consistent copy of the SQLite database, taken while the server runs:

```bash
docker exec CONTAINER aart-cli-telemetry-server backup /data/backup.sqlite3
```

`make image-smoke` builds the image and checks it with Docker. It checks:

- the refusal without the flag;
- a read-only container as uid 10001 storing a batch from the contract;
- the database at 0600;
- the backup;
- a second container serving what the first stored;
- the same image on PostgreSQL.

CI runs it on every change.

## Not yet

The rest of the deployment to AKS is the next stage and is not in this repository yet:

- publishing the image to a registry;
- manifests for one replica on a persistent disk;
- an authenticating ingress;
- scheduled backups and a tested restore.

## Licence

MIT. Chart.js is MIT, © Chart.js Contributors; see
`aart_cli_telemetry_server/web/static/chart.js.LICENSE.md`.
