"""No tracked file carries a literal in the shape of a credential, and the check says where one is
without repeating it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.secret_shape_check import main, scan

from .credential_fixtures import (
    access_token,
    assignment,
    credential_url,
    held,
    secret_field,
)

_part = st.from_regex(r"[A-Za-z0-9._~-]{1,16}", fullmatch=True)


def _codes(line: str) -> list[str]:
    return [finding.code for finding in scan("example.py", line)]


@given(user=_part, value=_part, scheme=st.sampled_from(["https", "postgresql+psycopg", "git"]))
def test_a_url_carrying_a_user_and_a_secret_is_refused(user: str, value: str, scheme: str) -> None:
    line = f'url = "{credential_url("db.example", "/x", scheme=scheme, user=user, held=value)}"'
    assert "credential-url" in _codes(line)


@given(st.text(alphabet=st.characters(blacklist_characters="@"), max_size=80))
def test_a_line_without_an_at_sign_carries_no_credential_url(line: str) -> None:
    assert "credential-url" not in _codes(line)


@pytest.mark.parametrize(
    "line",
    [
        'url = "https://user@index.example/simple"',
        'url = "http://127.0.0.1:8000/healthz"',
        'url = f"{scheme}://telemetry@{host}:5432/telemetry"',
        "a mail address, someone@example.com, is not a URL",
    ],
)
def test_a_url_without_a_secret_is_allowed(line: str) -> None:
    assert _codes(line) == []


def test_a_credential_word_with_a_quoted_value_is_refused() -> None:
    word, quoted = "token", '"' + held() + '"'
    assert _codes(f"{word} = {quoted}") == ["credential-literal"]
    assert _codes(secret_field("password", held())) == ["credential-literal"]


def test_a_credential_word_bound_to_code_is_allowed() -> None:
    word = "password"  # written apart: inside a string, `password = ...` is the refused shape
    assert _codes(f'{word} = os.environ["AART_CLI_TELEMETRY_PULL_PASSWORD"]') == []
    assert _codes("password: ${{ secrets[vars.PASSWORD_SECRET_NAME] }}") == []


def test_a_token_shaped_literal_is_refused() -> None:
    assert _codes(f"value = {access_token()}") == ["token-literal"]


def test_a_credential_assigned_inside_a_string_is_refused_unless_it_is_a_placeholder() -> None:
    assert _codes('said = "' + assignment("password", held()) + '"') == ["credential-assignment"]
    assert _codes('said = "' + assignment("password", "{held}") + '"') == []
    assert _codes('said = "' + assignment("password", "<yours>") + '"') == []


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="this image has no git")


def _git(*arguments: str) -> None:
    subprocess.run(["git", *arguments], check=True)  # noqa: S603, S607 -- fixed arguments, git on PATH


def _repository(tmp_path: Path, files: dict[str, str], tracked: set[str]) -> Path:
    _git("init", "-q", str(tmp_path))
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    _git("-C", str(tmp_path), "add", *sorted(tracked))
    return tmp_path


@needs_git
def test_a_tracked_file_with_a_credential_fails_and_is_named_without_its_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    value = held()
    root = _repository(
        tmp_path,
        {
            "clean.py": "x = 1\n",
            "leaky.py": f'x = 1\nurl = "{credential_url("h.example", held=value)}"\n',
        },
        {"clean.py", "leaky.py"},
    )
    assert main(root) == 2
    said = capsys.readouterr()
    assert "leaky.py:2: credential-url" in said.err
    assert "clean.py" not in said.err
    assert value not in said.out + said.err


@needs_git
def test_an_untracked_file_is_not_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repository(
        tmp_path,
        {"clean.py": "x = 1\n", "local.py": f'url = "{credential_url("h.example")}"\n'},
        {"clean.py"},
    )
    assert main(root) == 0
    assert "secret shape check OK" in capsys.readouterr().out


def test_without_git_every_file_but_git_s_own_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CI image may have no git; the checkout copied into it is then read whole.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(credential_url("h.example"), encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "leaky.py").write_text(credential_url("h.example"), encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    assert main(tmp_path) == 2
    (tmp_path / "scripts" / "leaky.py").write_text("x = 1\n", encoding="utf-8")
    assert main(tmp_path) == 0


@needs_git
def test_a_directory_that_is_no_repository_is_read_whole(tmp_path: Path) -> None:
    # The copy CI checks in an image has no `.git`, so git lists nothing there.
    (tmp_path / "leaky.py").write_text(credential_url("h.example"), encoding="utf-8")
    assert main(tmp_path) == 2


def test_a_line_is_one_finding_of_a_kind_however_many_strings_carry_it() -> None:
    one = '"' + assignment("password", held()) + '"'
    assert _codes(f"said = {one} + {one}") == ["credential-assignment"]
