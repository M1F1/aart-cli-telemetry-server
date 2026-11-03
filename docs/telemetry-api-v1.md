# aart-cli telemetry API, version 1

This document is the whole contract between aart-cli (the client) and aart-cli-telemetry-server (the
server). The two repositories keep **identical copies** of it, `docs/telemetry-api-v1.md`, together
with `contract/telemetry-api-v1.examples.json`; each repository's contract tests read both files,
so a change that is not made in both is a failing test in one of them. Neither side imports the
other's code.

## Endpoint

    POST <server>/api/v1/events/batch
    Content-Type: application/json

`<server>` is the server's base address, as a person configures it in aart-cli
(`consume telemetry enable --endpoint <server>`). aart-cli appends `/api/v1/events/batch`.

- **Transport.** `https://` for any host. `http://` only for a loopback host (`127.0.0.1`, `::1`,
  `localhost`), which is for local development. An address with a user name, password, query or
  fragment is refused by the client and is never sent.
- **Authentication.** Version 1 sends no credential from the client. A server that is reachable
  beyond loopback sits behind its organisation's authenticating proxy or ingress. A shared secret
  baked into a distributed aart-cli is never used.
- **Limits.** A request body is at most 1 MiB and holds 1 to 500 events. aart-cli sends at most 100.

## Request

```json
{
  "schema_version": 1,
  "client_id": "0b6f4d1e-8f8a-4a43-9a52-2f0f5d0f6a11",
  "client_version": "0.22.0",
  "events": [
    {
      "event_id": "7515ec68-1e98-46ca-bd90-9f6a70c84d11",
      "occurred_at": "2026-10-29T18:15:00Z",
      "event_type": "artifact.used",
      "source": "claude_hook",
      "artifact_id": "release-notes",
      "artifact_type": "skill",
      "artifact_version": "1.2.0"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `schema_version` | The integer `1`. |
| `client_id` | A lowercase UUID. Pseudonymous: aart-cli draws it at random when telemetry is turned on and draws a new one when its data is forgotten. It is not derived from the person, the machine or a path. |
| `client_version` | The aart-cli version, 1–64 characters of `[0-9A-Za-z.+-]`. |
| `automated` | Optional, `true` or `false`. `true` when aart-cli runs as automation -- a CI job, for one -- rather than for a person; aart-cli sends it only then, and a request without it is a person's. It says nothing more about the automation. |
| `environment` | Optional (revision 7). `{"os": ..., "arch": ..., "python": ...}`, all three: `os` is `darwin`, `linux`, `windows` or `other`; `arch` 1–16 characters of `[a-z0-9_]` (`arm64`, `x86_64`); `python` 1–32 characters of `[0-9A-Za-z.+-]`. Nothing else: no host, user or path. |
| `events` | 1–500 events. |

Every event:

| Field | Rule |
|---|---|
| `event_id` | A lowercase UUID, drawn by aart-cli when the event is recorded. The server's idempotency key. |
| `occurred_at` | UTC, `YYYY-MM-DDTHH:MM:SS` with an optional fraction of 1–6 digits, then `Z`. |
| `event_type` | `artifact.installed`, `artifact.updated`, `artifact.removed`, `artifact.used`, `unmanaged.used` or `aart_cli.failed`. |
| `source` | `aart_cli` for the first three and `aart_cli.failed`; `claude_hook` or `opencode_plugin` for `artifact.used` and `unmanaged.used`. |
| `artifact_id` | The artifact's name, 1–200 characters of `[0-9A-Za-z._@:+-]`. |
| `artifact_type` | The artifact's kind, 1–64 characters of `[a-z0-9-]` (`skill`, `mcp`, `agent`, ...). |
| `artifact_version` | Optional. 1–64 characters of `[0-9A-Za-z.+-]`. Never present in `unmanaged.used` or `aart_cli.failed`. |
| `failure` | Present in `aart_cli.failed` and in no other event; see below. |

An event may also say where and how it happened (below). No other field is accepted, in the
request or in an event. In particular an event never carries a prompt, what a tool was called with,
a file's content, a path, a project, a Registry address, a user name, a host name or any
credential.

### Where and how (revision 7)

Every one of these is optional, and each is accepted only in the event types its row names; in any
other it refuses the event (`this event_type does not hold that field`). Lifecycle is
`artifact.installed`, `artifact.updated` and `artifact.removed`; a use is `artifact.used` and
`unmanaged.used`. `aart_cli.failed` takes none of them.

| Field | Events | Rule |
|---|---|---|
| `harness` | lifecycle, a use | The harness the installation is for, or the use was in: a lowercase slug, 1–64 characters of `[a-z0-9-]` starting with a letter, words joined by single `-` (`claude`, `opencode`, `codex`). A use's is its source's: `claude` for `claude_hook`, `opencode` for `opencode_plugin`; a use that names none is read as its source's. |
| `scope` | lifecycle, `artifact.used` | `user` or `project`: whose configuration the installation is in. Never which project. |
| `registry` | lifecycle, `artifact.used` | The alias of the Registry the artifact was installed from, a slug as above (`company`). Never its address. |
| `interface` | lifecycle | `tui` or `cli`: where a person asked for it. |
| `previous_version` | `artifact.updated` | The version it was updated or downgraded from, as `artifact_version`. |
| `via` | lifecycle | `direct` (asked for itself), `collection` (as a member of a Collection) or `plugin` (a skill a plugin carries, §214). |
| `collection`, `collection_version` | lifecycle | The Collection it came in, as `artifact_id`, and its version, as `artifact_version`: only with `via` `collection`, `collection` then required and `collection_version` optional. |
| `package_digest` | `artifact.installed`, `artifact.updated` | The digest of the package installed: `sha256:` and 64 lowercase hex digits. |
| `duration_ms` | lifecycle | How long the action that changed it took, an integer 0–86400000. Every artifact of one action has the action's. |
| `configuration_inputs`, `credential_inputs` | `artifact.installed`, `artifact.updated` | How many configuration values and credentials the installation asked for, integers 0–1000. Never a value or a name. |
| `tool` | a use | Which tool of an MCP was called, 1–200 characters of `[0-9A-Za-z._@:+-]`; only when `artifact_type` is `mcp`. Never what it was called with. |
| `session` | a use | A lowercase UUID that stands for the harness's session the use was in, the same for every use of that session and drawn by aart-cli, so uses in one session can be counted together. Never the harness's own session id. |

`artifact.used` is sent only for an artifact aart-cli installed and could name with certainty, and
names that installation. `unmanaged.used` is a use of a skill or MCP that no artifact aart-cli
installed answers to: `artifact_id` is the name the harness used (a skill's name, an MCP server's
for Claude Code, the whole tool name for opencode) and `artifact_type` is `skill` or `mcp`. aart-cli
sends it only while the person, or without their choice the build, says such uses are sent. A use
aart-cli can attribute is never sent as `unmanaged.used`. Installation, use of an installed artifact
and use of anything else are separate event types and the server keeps them apart.

## Failures

`aart_cli.failed` is a failure aart-cli met. `artifact_type` is its kind: `crash` (an exception no
handler took), `error` (a command that ended with a nonzero exit code, or a terminal action that
stopped) or `interrupted` (Ctrl+C). `artifact_id` is where it happened, 1–128 characters of
`[a-z0-9._-]` starting with a letter or digit, in aart-cli's own words: a command's subcommands
joined by `.` (`consume.update`), `tui` or `tui.<action>`, `usage-hook`, `telemetry-sync`. It has
no `artifact_version`; the request's `client_version` is the version that failed. Its `failure`:

| Field | Rule |
|---|---|
| `fingerprint` | 16 lowercase hex digits: the first 16 of the SHA-256 of the UTF-8 JSON array `[artifact_type, artifact_id, exception or "", frames without their `:line`, diagnostics]`, written without spaces; when the failure has `causes`, the array has a sixth element, `[[exception, frames without their `:line`], ...]`, one per cause in their order. The server checks it. |
| `exception` | Required for `crash`, optional otherwise. An exception's type name, 1–128 characters of `[A-Za-z0-9_.]` starting with a letter or `_`. |
| `frames` | 0–30 strings `module:function:line`, innermost last: `module` `aart_cli` or a dotted name below it (`aart_cli.io.receipt_store`), at most 128 characters of `[A-Za-z0-9_.]`, `function` 1–128 characters of `[A-Za-z0-9_<>]`, `line` 1–6 digits. Only frames of aart-cli's own package. |
| `causes` | Optional, only with `exception`. 1–5 objects, each the exception the one before it was raised from or while handling, the first being `exception`'s: its `exception`, a type name as above, and its `frames`, as above. Nothing else. |
| `diagnostics` | 0–10 distinct diagnostic codes, each 1–64 characters of `[a-z0-9.-]` starting with a letter or digit. |
| `exit_code` | Optional. An integer 0–255. |
| `python` | Python's version, 1–32 characters of `[0-9A-Za-z.+-]`. |
| `platform` | `darwin`, `linux` or `other`. |

All of them but `exception`, `causes` and `exit_code` are required, and no other field is accepted. A
failure never carries an exception's message, an argument, a path, a project, an environment
variable, an input's value or a credential.

## Responses

| Status | Meaning | The client |
|---|---|---|
| `200` | Every event of the batch is stored. The body is `{"acknowledged_event_ids": [...]}`, every `event_id` of the request, including any stored by an earlier request. | Marks exactly the acknowledged ids as sent. |
| `400` | The body is not JSON. | Splits the batch, as for `422`. |
| `413` | The body is larger than 1 MiB. | Splits the batch. |
| `422` | The request does not follow this contract; nothing of it was stored. The body is `{"error": "...", "rejected_events": [{"index": 0, "event_id": "...", "reason": "..."}]}`, where `rejected_events` lists the events that are themselves invalid and is empty when the request's envelope is. | Marks the listed events rejected and sends the rest again; with none listed, splits the batch, and a single event the server refuses is marked rejected. |
| `401`, `403` | Not authorised. | Keeps every event and does not try again by itself for an hour. |
| `429`, `5xx`, a timeout or no connection | The server could not take it now. | Keeps every event and tries again later, with backoff. |

The server stores a batch in one transaction and answers `200` only after it committed. An
`event_id` it already holds is acknowledged and not stored or counted again, so a batch sent twice
-- because the answer to the first was lost -- counts once.

An error body never repeats a submitted value beyond the `event_id` and `index` of a rejected event.

## The revision a server holds

    GET <server>/api/v1/contract

Version 1 has grown by additions, each a revision. A server says which it holds:

```json
{"schema_version": 1, "revision": 7}
```

| Revision | Added |
|---|---|
| 1 | The request, the events `artifact.installed`, `artifact.updated`, `artifact.removed` and `artifact.used`, and the responses. |
| 2 | `unmanaged.used`. |
| 3 | `aart_cli.failed` and its `failure`. |
| 4 | The request's `automated`. |
| 5 | A failure's `causes`. |
| 6 | This endpoint. |
| 7 | The request's `environment`, and an event's `harness`, `scope`, `registry`, `interface`, `previous_version`, `via`, `collection`, `collection_version`, `package_digest`, `duration_ms`, `configuration_inputs`, `credential_inputs`, `tool` and `session`. |

The answer is `200` with that object. The client reads `schema_version` and `revision`, and ignores
any other field. The server needs no database to answer it.

The client asks before it sends, at most once per synchronisation. With a revision, it sends only
what that revision holds. An event of a type the revision does not hold, a failure with `causes`
below revision 5, and every event of a request that would carry `automated` below revision 4 wait
unsent until the server holds them. They are not marked rejected. Below revision 7 the client
leaves revision 7's fields out of each event and `environment` out of the request, and sends the
rest unchanged: those fields only add to what an event says. `401`, `403`, `429`, `5xx`, a
timeout or no connection are handled as they are for the batch. Any other answer -- a `404` from a
server older than revision 6, or a `200` that names no revision -- means the client sends as it
did before revision 6: everything but revision 7's fields and `environment`, handling a `422` as
above.

## Other endpoints of the server

These are the server's own and not part of what aart-cli relies on. `GET /docs` describes them.

- `GET /healthz` -- `{"status": "ok"}` when the database can be read.
- `GET /dashboard` -- the dashboard.
- `GET /api/v1/artifacts`, `/api/v1/artifacts/{artifact_type}/{artifact_id}`, `/api/v1/events`,
  `/api/v1/breakdown`, `/api/v1/client-versions` -- the read API.
- `GET /api/v1/stats/summary`, `/api/v1/stats/top-artifacts`, `/api/v1/stats/top-unmanaged`,
  `/api/v1/stats/failures`, `/api/v1/stats/daily`, `/api/v1/stats/by-source` -- the dashboard's
  aggregates.
- `POST /mcp` -- the same reads as a read-only MCP server.

## Versioning

A change that a version-1 server or client would misread is version 2: a new path
(`/api/v2/...`) and a new `schema_version`. An addition to version 1 is the next revision, in the
table above, and a server that holds it says so. From revision 6 on, a client holds back what the
server does not yet hold, so a client may be released before the server it sends to.

What follows is how an addition behaves with a server older than revision 6. A new event type is
added to version 1: a server that does not know it answers `422` naming those events, which the
client marks rejected while the rest of the batch is stored, so nothing is misread and only the new
type is lost. `unmanaged.used` was added this way, and `aart_cli.failed` with the `failure` field
only it carries. An optional field of the request is added to version 1 when a client sends it only
where it matters: a server that does not know it refuses just those requests, and the client marks
their events rejected. `automated` was added this way: an older server loses only what automation
sends, and a person's requests are unchanged. An optional field of a `failure` is added the same
way, sent only where it holds something: `causes` was, and an older server refuses only the
failures that have causes (`422`), while every other event of the request is stored.
