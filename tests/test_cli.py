"""The composition root: which database, which address, and a backup."""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aart_cli_telemetry_server.cli import (
    BEHIND_PROXY_VARIABLE,
    DATA_DIR_VARIABLE,
    DATABASE_URL_VARIABLE,
    database_url,
    default_data_dir,
    is_loopback,
    main,
)


def test_serve_refuses_beyond_loopback_without_a_proxy(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", "--host", "0.0.0.0", "--data-dir", "/nonexistent"]) == 2
    assert "authenticating proxy" in capsys.readouterr().err
    assert [
        is_loopback(h)
        for h in ("127.0.0.1", "::1", "[::1]", "localhost", "0.0.0.0", "10.0.0.1", "x")
    ] == [
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def _served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "aart_cli_telemetry_server.cli.uvicorn.run",
        lambda app, **options: calls.append(options),
    )
    return calls


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_the_proxy_in_front_can_be_said_by_a_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = _served(monkeypatch)
    monkeypatch.setenv(BEHIND_PROXY_VARIABLE, value)
    assert main(["serve", "--host", "0.0.0.0", "--data-dir", str(tmp_path)]) == 0
    assert [(call["host"], call["proxy_headers"]) for call in calls] == [("0.0.0.0", True)]


@pytest.mark.parametrize("value", ["", "false", " FALSE "])
def test_a_variable_that_says_no_proxy_is_refused_beyond_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = _served(monkeypatch)
    monkeypatch.setenv(BEHIND_PROXY_VARIABLE, value)
    assert main(["serve", "--host", "0.0.0.0", "--data-dir", str(tmp_path)]) == 2
    assert main(["serve", "--data-dir", str(tmp_path)]) == 0
    assert [call["proxy_headers"] for call in calls] == [False]


@given(
    value=st.text(
        alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=12
    ).filter(lambda v: v.strip().lower() not in {"", "true", "false"})
)
def test_any_other_value_of_the_variable_refuses_to_start_even_with_the_flag(value: str) -> None:
    # Not monkeypatch: Hypothesis runs this body many times inside one test.
    before = os.environ.get(BEHIND_PROXY_VARIABLE)
    os.environ[BEHIND_PROXY_VARIABLE] = value
    error = io.StringIO()
    try:
        with contextlib.redirect_stderr(error):
            refused = main(["serve", "--behind-authenticating-proxy", "--data-dir", "/nonexistent"])
    finally:
        if before is None:
            del os.environ[BEHIND_PROXY_VARIABLE]
        else:
            os.environ[BEHIND_PROXY_VARIABLE] = before
    assert refused == 2
    assert f"${BEHIND_PROXY_VARIABLE} is true or false" in error.getvalue()


def test_the_database_is_the_flag_then_the_url_then_the_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(DATABASE_URL_VARIABLE, raising=False)
    monkeypatch.delenv(DATA_DIR_VARIABLE, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_data_dir() == str(tmp_path / "xdg" / "aart-cli-telemetry-server")
    assert database_url() == f"sqlite:///{tmp_path}/xdg/aart-cli-telemetry-server/telemetry.sqlite3"
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path / "data"))
    assert database_url() == f"sqlite:///{tmp_path}/data/telemetry.sqlite3"
    monkeypatch.setenv(DATABASE_URL_VARIABLE, "postgresql://telemetry@db/telemetry")
    assert database_url() == "postgresql://telemetry@db/telemetry"
    assert database_url(str(tmp_path / "flag")) == f"sqlite:///{tmp_path}/flag/telemetry.sqlite3"


def test_backup_copies_and_a_refused_database_is_said_without_its_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    assert main(["backup", str(tmp_path / "copy.sqlite3"), "--data-dir", str(data)]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "copy.sqlite3")
    with sqlite3.connect(tmp_path / "copy.sqlite3") as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone() == (1,)

    monkeypatch.setenv(DATABASE_URL_VARIABLE, "postgresql+nodriver://u:hunter2@db/x")
    assert main(["backup", str(tmp_path / "other.sqlite3")]) == 1
    said = capsys.readouterr().err
    assert "the database cannot be used" in said
    assert "hunter2" not in said


def test_serve_runs_the_app_on_the_database_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "aart_cli_telemetry_server.cli.uvicorn.run",
        lambda app, **options: calls.append({"app": type(app).__name__, **options}),
    )
    assert main(["serve", "--data-dir", str(tmp_path), "--port", "8123"]) == 0
    assert calls == [
        {
            "app": "FastAPI",
            "host": "127.0.0.1",
            "port": 8123,
            "proxy_headers": False,
            "server_header": False,
        }
    ]
    assert (tmp_path / "telemetry.sqlite3").exists()


def test_the_asgi_entry_point_opens_the_configured_database_or_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATABASE_URL_VARIABLE, f"sqlite:///{tmp_path}/asgi/telemetry.sqlite3")
    sys.modules.pop("aart_cli_telemetry_server.asgi", None)
    module = importlib.import_module("aart_cli_telemetry_server.asgi")
    assert type(module.app).__name__ == "FastAPI"

    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "telemetry.sqlite3").write_bytes(b"x" * 64)
    monkeypatch.setenv(DATABASE_URL_VARIABLE, f"sqlite:///{tmp_path}/broken/telemetry.sqlite3")
    sys.modules.pop("aart_cli_telemetry_server.asgi", None)
    with pytest.raises(RuntimeError, match="cannot be used"):
        importlib.import_module("aart_cli_telemetry_server.asgi")
    sys.modules.pop("aart_cli_telemetry_server.asgi", None)
