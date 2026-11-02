"""`python -m deploy.smoke URL`: what a deployed server answers from here, storing nothing.

Run it from where aart-cli runs, so it goes through the same network and proxies:

- `GET /healthz` is 200 `{"status": "ok"}`: the server is up and reads its database.
- `POST /api/v1/events/batch` with `{}` is 422: the ingest endpoint is reachable as aart-cli
  reaches it, with no sign-in, and a request the contract refuses stores nothing.
- `GET /dashboard` is 200, or a sign-in in front of it -- a redirect, 401 or 403 -- said as such.

0 when the first two hold, 1 otherwise. The address may not carry a user name or password.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

__all__ = ["check", "main"]

_SIGN_IN = (301, 302, 303, 307, 308, 401, 403)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _request(url: str, data: bytes | None = None) -> tuple[int | None, bytes]:
    # check() lets only http:// and https:// addresses through.
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "aart-cli-telemetry-smoke"},
    )
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            return int(response.status), bytes(response.read(4096))
    except urllib.error.HTTPError as error:
        return int(error.code), bytes(error.read(4096))
    except (urllib.error.URLError, OSError, ValueError):
        return None, b""


Fetch = Callable[[str, "bytes | None"], "tuple[int | None, bytes]"]


def check(base: str, fetch: Fetch = _request, say: Callable[[str], None] = print) -> int:
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        say("refused: the address is http:// or https:// and names a host")
        return 2
    if parts.username is not None or parts.password is not None:
        say("refused: the address may not carry a user name or password")
        return 2
    base = base.rstrip("/")

    health, body = fetch(f"{base}/healthz", None)
    healthy = health == 200 and b'"ok"' in body
    say(f"{'ok' if healthy else 'FAIL'}: GET /healthz answered {health}")

    ingest, _ = fetch(f"{base}/api/v1/events/batch", b"{}")
    reachable = ingest == 422
    say(
        f"{'ok' if reachable else 'FAIL'}: POST /api/v1/events/batch with {{}} answered {ingest}"
        + ("" if reachable else "; aart-cli sends no sign-in, so it must reach this unchallenged")
    )

    dashboard, _ = fetch(f"{base}/dashboard", None)
    if dashboard == 200:
        say("ok: GET /dashboard answered 200, with no sign-in in front of it")
    elif dashboard in _SIGN_IN:
        say(f"ok: GET /dashboard answered {dashboard}, a sign-in in front of it")
    else:
        say(f"note: GET /dashboard answered {dashboard}")
    return 0 if healthy and reachable else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m deploy.smoke https://HOST[:PORT][/PATH]", file=sys.stderr)
        return 2
    return check(arguments[0])


if __name__ == "__main__":
    raise SystemExit(main())
