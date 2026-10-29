"""`uvicorn aart_telemetry_server.asgi:app`: the application on the configured database."""

from __future__ import annotations

from returns.result import Failure, Success

from .adapters.sql import open_repository
from .cli import database_url
from .effects import perform
from .web.app import create_app

match perform(open_repository(database_url())):
    case Success(repository):
        app = create_app(repository)
    case Failure(error):
        raise RuntimeError(f"the database cannot be used: {error.reason}")
