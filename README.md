# aart-cli-telemetry-server

Receives artifact telemetry from [aart-cli](https://github.com/M1F1/aart-cli), stores it in a
database -- SQLite by default, PostgreSQL by URL -- and shows it on a dashboard in the browser: which artifacts are used, how often, by how many
installations, and how installs, updates and removals change over time. Uses of skills and MCPs
aart-cli did not install (`unmanaged.used`) are counted apart, by the name the harness gave them.
Failures aart-cli met (`aart_cli.failed`: a crash, a command that ended in an error, an
interruption) are grouped by fingerprint -- the defect, not its line numbers -- with how often, on
how many installations, on which versions and platforms. A failure carries an exception's type
name, aart-cli's own frames and diagnostic codes; never a message, an argument or a path. The
server recomputes each fingerprint and refuses one that is not the failure's own.

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
or not created by this server, is refused and left as it is. A version-1 database from before
failures were received gains its `failure` table on the first start of this version, and nothing
else in it changes. Servers starting together on one database do this one at a time, so every
replica of a first deployment or an upgrade starts. A connection the database dropped -- a
restart, a failover, an idle timeout -- is replaced before it is used, so the first request after
one is answered rather than refused with 503.

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

## Who may reach it

Version 1 of the API carries no client credential, so `serve` refuses any address other than
loopback unless `--behind-authenticating-proxy`, or
`AART_CLI_TELEMETRY_BEHIND_AUTHENTICATING_PROXY=true` in its environment, says that what is in front
of it decides who reaches it. Any other value of that variable refuses to start.

aart-cli sends no credential and cannot answer a sign-in, so what limits who may send events is
where a request can come from: the organisation's network, a VPN, a private endpoint, a zero-trust
access service -- whichever the organisation uses. The dashboard and `/api/v1/stats/*` may also sit
behind the organisation's sign-in, with `/api/v1/events/batch` and `/healthz` left out of it.
Never expose the server to the Internet unrestricted.

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
| `GET /api/v1/stats/summary?days=30` | Uses, installs, updates, removals, active installations, uses of tools aart-cli did not install, failures aart-cli met. |
| `GET /api/v1/stats/top-artifacts?days=30&limit=20` | Artifacts by uses. |
| `GET /api/v1/stats/top-unmanaged?days=30&limit=20` | Skills and MCPs aart-cli did not install, by uses (`unmanaged.used`). |
| `GET /api/v1/stats/failures?days=30&limit=20` | Failures aart-cli met, by defect (`aart_cli.failed`): most frequent, then most recent, first. |
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

CI installs without Poetry: `scripts/locked_requirements.py` prints what `poetry.lock` pins, every
hash with it, and pip installs that, as aart-cli's CI does. Poetry takes an index only from a
source in `pyproject.toml`, which would change the hash the lock is checked against, while pip
reads `PIP_INDEX_URL`, so a copy that installs from its own index sets a variable and edits no file.

## Container image

The `Dockerfile` builds an image for any container platform:

```bash
docker build -t aart-cli-telemetry-server .
```

- **What it installs is what `poetry.lock` pins**, by pip with every hash checked. `PYTHON_IMAGE` is
  the base, any Debian-based image with Python 3.11 or later. The packages come from pip's index, or
  from the one the build secret `pip-index` holds (a URL, with credentials where it needs them),
  which is present only while pip runs and never in a layer:

  ```bash
  docker build --build-arg PYTHON_IMAGE=python:3.13-slim-bookworm --secret id=pip-index,env=PIP_INDEX_URL -t aart-cli-telemetry-server .
  ```
- **The database is SQLite on the `/data` volume** unless `AART_CLI_TELEMETRY_DATABASE_URL` names
  another. The `postgresql` extra is installed, so a PostgreSQL URL works without a rebuild. Pass the
  URL as a secret, since it can carry a password.
- **It listens on `0.0.0.0:8000` only with `--behind-authenticating-proxy`** as the container's
  argument, or `AART_CLI_TELEMETRY_BEHIND_AUTHENTICATING_PROXY=true` in its environment where a
  platform sets variables more easily than arguments. Without either the container refuses to
  start, as `serve` does elsewhere, because a container's port is reachable from beyond its host.
- **It runs as uid 10001** with nothing writable but `/data`, so a read-only root filesystem works. A
  bind-mounted `/data` must be writable by that uid; a named volume is prepared by the image.
- **Health** is `GET /healthz` on port 8000; point the platform's liveness and readiness probes at
  it. The image's own `HEALTHCHECK` does the same for Docker.

To try it on this machine, publish the port to loopback only:

```bash
docker run --rm -p 127.0.0.1:8000:8000 -v aart-cli-telemetry:/data aart-cli-telemetry-server --behind-authenticating-proxy
```

SQLite needs a disk of its own: a block device behind one replica. A network file share, which
many container platforms and managed application hosts mount as persistent storage, does not give
SQLite the locking WAL mode relies on. There, use PostgreSQL, managed or your own. Run one replica
on SQLite; several replicas need PostgreSQL.

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

## Deploying

`deploy/` turns an environment file into the commands that deploy the image to a target, which is a
parameter:

```bash
make deploy TARGET=docker ENVIRONMENT=deploy/environments/example-docker.env
make deploy TARGET=docker ENVIRONMENT=deploy/environments/example-docker.env APPLY=1
make smoke URL=https://telemetry.example
```

- **A target is a module in `deploy/targets/`**: the settings it reads and `plan(settings)`, the
  commands with why each runs. It is found by its module alone. The server imports nothing from
  `deploy/` and `deploy/` nothing from the server (`tach.toml`, `tests/test_architecture.py`), so a
  platform never becomes part of the server.
- **Without `APPLY=1` the plan is printed** and nothing runs. With it each step runs in turn, and the
  first that fails stops the rest.
- **An environment file is meant to be committed**, so it holds names, addresses and references,
  never a secret: a key that names one (`*_PASSWORD`, `*_TOKEN`, `*_SECRET` and the like) or a URL
  carrying a password is refused, and no refusal repeats a value. A secret reaches the server by
  reference to where the platform keeps secrets, or from a file only the host reads.
- **`docker`** runs the image on this machine, or on another through `DOCKER_CONTEXT`: it pulls the
  image (unless `PULL=false`), replaces the container of the same name on the same volume, sets
  `AART_CLI_TELEMETRY_BEHIND_AUTHENTICATING_PROXY=true`, publishes port 8000 on `PUBLISH`
  (`127.0.0.1:8000` unless set) for whatever the host puts in front, and waits until the image's
  health check reports healthy. A PostgreSQL URL comes from `ENV_FILE`, which Docker reads and
  this never does. See `deploy/environments/example-docker.env`.
- **`make smoke URL=...`** (`python -m deploy.smoke`) checks a deployed server from where aart-cli
  runs, through the same network, and stores nothing: `GET /healthz` is 200; `POST
  /api/v1/events/batch` with `{}` is 422, so the ingest endpoint is reachable with no sign-in; and
  whether `GET /dashboard` is served or has a sign-in in front of it.

## Your organisation's copy

A copy of this repository on your organisation's own Git host differs from it only in GitHub
Actions variables (Settings, Secrets and variables, Actions). Each has this repository's value as
its fallback, and a secret is named by a variable, never read by a fixed name, as in aart-cli:

| Variable | What it sets | Unset |
|---|---|---|
| `AART_CLI_TELEMETRY_RUNNER` | the runner labels, as JSON | `["ubuntu-latest"]` |
| `AART_CLI_TELEMETRY_CI_PYTHON_IMAGE` | the image the checks run in, tagged with each Python version | `python` |
| `AART_CLI_TELEMETRY_PYTHON_VERSIONS` | the Python versions `quality` runs under, as JSON | `["3.11", "3.13"]` |
| `AART_CLI_TELEMETRY_PYTHON_VERSION` | the Python version of the PostgreSQL check | `3.13` |
| `AART_CLI_TELEMETRY_PIP_INDEX_URL` | the package index | `https://pypi.org/simple` |
| `AART_CLI_TELEMETRY_PIP_INDEX_CREDENTIALS_SECRET_NAME` | the secret holding the index's `user:password` | none |
| `AART_CLI_TELEMETRY_PYTHON_IMAGE` | the image's base | `python:3.13-slim-bookworm` |
| `AART_CLI_TELEMETRY_POSTGRES_IMAGE` | the PostgreSQL image the checks run against | `postgres:16`, `postgres:16-alpine` |
| `AART_CLI_TELEMETRY_IMAGE_REGISTRY` | the registry the image is published to | not published |
| `AART_CLI_TELEMETRY_IMAGE_REPOSITORY` | the image's repository there | `aart-cli-telemetry-server` |
| `AART_CLI_TELEMETRY_IMAGE_USERNAME_SECRET_NAME` | the secret holding the registry user name | the runner's own sign-in |
| `AART_CLI_TELEMETRY_IMAGE_PASSWORD_SECRET_NAME` | the secret holding the registry password or token | the runner's own sign-in |

With a registry named, every change on `main` that passes every check is built and checked again,
then pushed as `REGISTRY/REPOSITORY:sha-<first 12 of the commit>` and `:main`. A deployment names
the `sha-` tag, so it changes only when it is told to. Give the registry credential the right to
push to that one repository and nothing else.

A copy that deploys with a target of its own adds it as `deploy/targets/NAME.py`, its tests as
`tests/test_deploy_NAME.py`, and its environments as `deploy/environments/NAME.env`. This
repository keeps only `docker.py` and `example-*.env` there, so taking its changes, with this
repository as the remote `upstream`, never conflicts with them:

```bash
git fetch upstream
git merge upstream/main
```

## Not yet

- Pulling a CI image from a registry that needs credentials.
- Scheduled backups and a tested restore.

## Licence

MIT. Chart.js is MIT, © Chart.js Contributors; see
`aart_cli_telemetry_server/web/static/chart.js.LICENSE.md`.
