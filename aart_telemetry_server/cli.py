"""`aart-telemetry-server serve | backup`: the composition root.

This is where the shell is assembled: which database, which clock, which address. `serve` listens on
loopback unless told it sits behind an authenticating proxy: version 1 of the API carries no client
credential, so a server reachable from elsewhere without one in front would accept anybody's events
and show anybody the dashboard.

The database is `$AART_TELEMETRY_DATABASE_URL` when set -- any URL SQLAlchemy reads, such as a
PostgreSQL one -- and otherwise SQLite in the data directory. A URL can carry a password, so it is
never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

import uvicorn
from returns.result import Failure, Success

from .adapters.sql import open_repository, sqlite_url
from .effects import perform
from .web.app import create_app

__all__ = [
    "DATABASE_URL_VARIABLE",
    "DATA_DIR_VARIABLE",
    "database_url",
    "default_data_dir",
    "is_loopback",
    "main",
]

DATA_DIR_VARIABLE = "AART_TELEMETRY_DATA_DIR"
DATABASE_URL_VARIABLE = "AART_TELEMETRY_DATABASE_URL"


def default_data_dir() -> str:
    configured = os.environ.get(DATA_DIR_VARIABLE, "").strip()
    if configured:
        return configured
    base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    return str(Path(base) / "aart-telemetry-server")


def database_url(data_dir: str | None = None) -> str:
    """SQLite in `data_dir` when given, else the configured URL, else SQLite in the default."""

    configured = os.environ.get(DATABASE_URL_VARIABLE, "").strip()
    if configured and data_dir is None:
        return configured
    return sqlite_url(os.path.abspath(data_dir or default_data_dir()))


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aart-telemetry-server")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--data-dir",
        default=None,
        help=f"SQLite here; default: ${DATABASE_URL_VARIABLE}, else ${DATA_DIR_VARIABLE}",
    )
    serve.add_argument(
        "--behind-authenticating-proxy",
        action="store_true",
        help="listen beyond loopback; only when a proxy or ingress authenticates every request",
    )

    backup = commands.add_parser("backup", help="copy a SQLite database consistently while it runs")
    backup.add_argument("target")
    backup.add_argument("--data-dir", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if (
        arguments.command == "serve"
        and not is_loopback(arguments.host)
        and not arguments.behind_authenticating_proxy
    ):
        print(
            f"refusing to listen on {arguments.host}: version 1 of the API carries no client "
            "credential. Listen on 127.0.0.1, or put an authenticating proxy in front and pass "
            "--behind-authenticating-proxy.",
            file=sys.stderr,
        )
        return 2

    match perform(open_repository(database_url(arguments.data_dir))):
        case Failure(error):
            print(f"error: the database cannot be used: {error.reason}", file=sys.stderr)
            return 1
        case Success(repository):
            pass

    if arguments.command == "backup":
        match perform(repository.backup(arguments.target)):
            case Success(path):
                print(path)
                return 0
            case Failure(error):
                print(f"error: {error.reason}", file=sys.stderr)
                return 1

    uvicorn.run(
        create_app(repository),
        host=arguments.host,
        port=arguments.port,
        proxy_headers=arguments.behind_authenticating_proxy,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
