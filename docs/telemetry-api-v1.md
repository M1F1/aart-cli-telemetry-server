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
| `events` | 1–500 events. |

Every event:

| Field | Rule |
|---|---|
| `event_id` | A lowercase UUID, drawn by aart-cli when the event is recorded. The server's idempotency key. |
| `occurred_at` | UTC, `YYYY-MM-DDTHH:MM:SS` with an optional fraction of 1–6 digits, then `Z`. |
| `event_type` | `artifact.installed`, `artifact.updated`, `artifact.removed`, `artifact.used` or `unmanaged.used`. |
| `source` | `aart_cli` for the first three; `claude_hook` or `opencode_plugin` for `artifact.used` and `unmanaged.used`. |
| `artifact_id` | The artifact's name, 1–200 characters of `[0-9A-Za-z._@:+-]`. |
| `artifact_type` | The artifact's kind, 1–64 characters of `[a-z0-9-]` (`skill`, `mcp`, `agent`, ...). |
| `artifact_version` | Optional. 1–64 characters of `[0-9A-Za-z.+-]`. Never present in `unmanaged.used`. |

No other field is accepted, in the request or in an event. In particular an event never carries a
prompt, what a tool was called with, a file's content, a path, a project, a Registry address, a
user name, a host name or any credential.

`artifact.used` is sent only for an artifact aart-cli installed and could name with certainty, and
names that installation. `unmanaged.used` is a use of a skill or MCP that no artifact aart-cli
installed answers to: `artifact_id` is the name the harness used (a skill's name, an MCP server's
for Claude Code, the whole tool name for opencode) and `artifact_type` is `skill` or `mcp`. aart-cli
sends it only while the person, or without their choice the build, says such uses are sent. A use
aart-cli can attribute is never sent as `unmanaged.used`. Installation, use of an installed artifact
and use of anything else are separate event types and the server keeps them apart.

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

## Other endpoints of the server

These are the server's own and not part of what aart-cli relies on.

- `GET /healthz` -- `{"status": "ok"}` when the database can be read.
- `GET /dashboard` -- the dashboard.
- `GET /api/v1/stats/summary`, `/api/v1/stats/top-artifacts`, `/api/v1/stats/daily`,
  `/api/v1/stats/by-source` -- the dashboard's aggregates.

## Versioning

A change that a version-1 server or client would misread is version 2: a new path
(`/api/v2/...`) and a new `schema_version`. A new event type is added to version 1: a server that
does not know it answers `422` naming those events, which the client marks rejected while the
rest of the batch is stored, so nothing is misread and only the new type is lost. `unmanaged.used`
was added this way.
