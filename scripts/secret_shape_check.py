"""Refuse credential-shaped literals anywhere in the tracked tree, as aart-cli does.

A secret scanner reads bytes, not intent. Push protection at an organisation that turns it on
rejects a push carrying anything in the shape of a credential, a redaction test's made-up value
included, and a value already in history can only be let through by an exception. So the shape
is assembled at run time by `tests/credential_fixtures.py`, and this check keeps any tracked file
from writing it down. It says where a finding is and what shape it has, never the text.

The patterns are built from fragments for the same reason: a check that trips itself is a check
nobody can run. Where git cannot list the files, because it is not installed or the directory is
no repository, as in the copy CI checks in an image, the directory is read whole but for git's own
directory: never fewer files than git would list.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_AT = "@"
_COLON = ":"
_SLASHES = "/" + "/"

# A scheme, then a userinfo of two colon-separated halves, then the authority. Neither half holds
# a quote or whitespace, so a line that mentions a scheme and, apart, an address does not match.
_URI_RE = re.compile(
    r"[a-z][a-z0-9+.\-]*" + _COLON + _SLASHES + r"[^\s/@:\"']+" + _COLON + r"[^\s/@\"']+" + _AT
)

# A credential-naming key next to a quoted value: `"password":"…"`, `token = '…'`.
_KEY_WORDS = (
    "password|passwd|pwd|secret|token|credential"
    "|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key"
)
# A key word starts a word or ends a name, as in `API_TOKEN` or `client_secret`; one that goes on,
# as in `PASSWORD_SECRET_NAME`, names where a secret is kept rather than holding one.
_KEY_START = r"(?:\b|(?<=_))"
_KV_RE = re.compile(
    r"(?i)"
    + _KEY_START
    + r"("
    + _KEY_WORDS
    + r")\b[\"']?\s*["
    + _COLON
    + r"=]\s*[\"'][^\"']{2,}[\"']"
)

# The prefixes GitHub issues tokens under, matched as a scanner matches them: by prefix and length.
_TOKEN_PREFIXES = ("ghp", "gho", "ghs", "ghu", "ghr", "github" + "_pat")
_TOKEN_RE = re.compile(r"\b(" + "|".join(_TOKEN_PREFIXES) + r")_[A-Za-z0-9_]{20,}")

# The same key words with no quotes around the value, searched only inside a string or backtick
# span: outside one, the same text is an ordinary keyword argument or assignment in code.
_SPAN_RE = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`")
_UNQUOTED_RE = re.compile(
    r"(?i)" + _KEY_START + r"(" + _KEY_WORDS + r")\b\s*" + r"=" + r"\s*([^\s\"'`,;)}\]&$\\]{4,})"
)
# A value opening with one of these is a placeholder or an expansion, not a credential.
_PLACEHOLDER_STARTS = ("[", "{", "<", "%")

_SKIP_SUFFIXES = (".lock", ".png", ".ico", ".whl")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.text}"


def scan(path: str, text: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _URI_RE.search(line):
            findings.append(
                Finding(path, number, "credential-url", "a URL carrying user and secret")
            )
        if _KV_RE.search(line):
            findings.append(
                Finding(
                    path,
                    number,
                    "credential-literal",
                    "a credential-naming key with a quoted value",
                )
            )
        if _TOKEN_RE.search(line):
            findings.append(
                Finding(path, number, "token-literal", "a literal in the shape of an issued token")
            )
        for span in _SPAN_RE.findall(line):
            match = _UNQUOTED_RE.search(span)
            if match and not match.group(2).startswith(_PLACEHOLDER_STARTS):
                findings.append(
                    Finding(
                        path,
                        number,
                        "credential-assignment",
                        "a credential-naming key assigned a value inside a string",
                    )
                )
                break
    return tuple(findings)


def _files(root: Path) -> tuple[str, ...]:
    try:
        listing = subprocess.run(
            ("git", "ls-files"), cwd=root, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        listing = None
    if listing is not None and listing.returncode == 0:
        return tuple(line for line in listing.stdout.splitlines() if line)
    found: list[str] = []
    for folder, folders, names in os.walk(root):
        folders[:] = [name for name in folders if name != ".git"]
        found += [str(Path(folder, name).relative_to(root)) for name in names]
    return tuple(sorted(found))


def main(root: Path = ROOT) -> int:
    findings: list[Finding] = []
    for path in _files(root):
        if path.endswith(_SKIP_SUFFIXES):
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan(path, text))
    if not findings:
        print("secret shape check OK")
        return 0
    for finding in sorted(findings):
        print(finding.render(), file=sys.stderr)
    print(
        f"\n{len(findings)} credential-shaped literal(s). Assemble them through "
        "tests/credential_fixtures.py instead; see that module's docstring for why.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
