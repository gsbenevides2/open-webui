# Artifact access to tools and MCP servers

> Implemented on the `feat/artifact-tools-mcp` branch of `gsbenevides2/open-webui`.
> Planning notes and the design rationale live in
> [`artifacts-tools-mcp-plan.md`](./artifacts-tools-mcp-plan.md).

## What it does

Artifacts (the HTML/CSS/JS the model writes and the chat renders in a sandboxed iframe)
can now call the **tools and MCP servers available to the person looking at the chat**.

Inside an artifact:

```js
// Which tools may I use here?
const tools = await window.owui.tools.list();

// Call one of them.
const result = await window.owui.tools.call('query_database', { sql: 'select 1' });

// MCP tools also have a short form.
const issues = await window.owui.mcp.call('github', 'list_issues', { repo: 'open-webui/open-webui' });
```

Both calls return a promise that resolves with the tool result (`result`, plus `files`,
`embeds` and `duration` when the tool produced them) and rejects with an `Error` whose
message is the backend's detail string.

## How it stays safe

| Concern | Behaviour |
|---|---|
| Credentials | The artifact iframe never receives a token. It talks to `Artifacts.svelte` over a private `MessageChannel`; the host re-issues the request with the viewer's own session. |
| Sandbox | Unchanged: `allow-scripts`, **never** `allow-same-origin`. The artifact still cannot read `localStorage`, cookies or the API. |
| Allowlist | Only the tools selected in that chat are offered. Every id is re-resolved server-side with the same access-grant checks the chat uses, so a revoked or unavailable tool simply answers `404`. |
| Consent | The host asks before the first call to each tool; the decision is remembered per artifact tab. Escape or "Cancel" rejects the promise. |
| Limits | `ARTIFACT_TOOL_TIMEOUT` (120 s), `ARTIFACT_TOOL_MAX_ARGUMENT_BYTES` (1 MiB), `ARTIFACT_TOOL_MAX_RESULT_BYTES` (2 MiB — oversized results arrive as `{truncated: true}`), and a per-user rate limit of `ARTIFACT_TOOL_RATE_LIMIT` calls per `ARTIFACT_TOOL_RATE_WINDOW` seconds (60/60 by default). |
| Shared chats | The artifact runs with the **viewer's** permissions, never the chat owner's. |
| Approval | `tool_approval_mode` is forced to `full` for artifact calls: the human approval happens in the host dialog, and an artifact cannot approve itself. |
| Audit | Every call logs user, chat, message, tool, outcome and duration. |

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ENABLE_ARTIFACT_TOOLS` | `true` | Master switch. Also requires `ENABLE_TOOLS` (local/OpenAPI tools) or `ENABLE_TOOL_SERVERS` (MCP). |
| `ARTIFACT_TOOL_TIMEOUT` | `120` | Seconds a single artifact tool call may take. |
| `ARTIFACT_TOOL_MAX_ARGUMENT_BYTES` | `1048576` | Reject larger argument payloads with `413`. |
| `ARTIFACT_TOOL_MAX_RESULT_BYTES` | `2097152` | Truncate larger results. |
| `ARTIFACT_TOOL_RATE_LIMIT` / `ARTIFACT_TOOL_RATE_WINDOW` | `60` / `60` | Per-user call budget. |

The frontend only enables the bridge when the user allows iframe scripts
(`iframeSandboxAllowScripts`) and `/api/config` reports
`features.enable_artifact_tools`.

## API

```
POST /api/v1/chats/{chat_id}/artifacts/tools/list
POST /api/v1/chats/{chat_id}/artifacts/tools/call
GET  /api/v1/artifacts/tools/config
```

Request bodies (`extra='forbid'`, so unknown fields are rejected):

```json
{ "message_id": "…", "tool_ids": ["tool-id", "server:mcp:github|list_issues"] }

{ "message_id": "…", "tool_ids": ["…"], "name": "github_list_issues", "arguments": { "repo": "…" } }
```

The call response:

```json
{ "name": "github_list_issues", "ok": true, "result": { }, "files": [], "embeds": [], "duration": 0.42 }
```

## Code map

| Layer | File |
|---|---|
| Runtime injected into the artifact document | `src/lib/utils/artifacts.ts` |
| Host side of the bridge (handshake, consent, spinner) | `src/lib/components/chat/Artifacts.svelte` |
| API client | `src/lib/apis/artifacts/index.ts` |
| Allowlist plumbing | `src/lib/components/chat/Chat.svelte`, `Tabs.svelte`, `Tabs/ChatTab.svelte` |
| Endpoints | `backend/open_webui/routers/artifacts.py` |
| Resolution + execution | `backend/open_webui/utils/artifacts.py` |
| Pure shaping (tested without the app) | `backend/open_webui/utils/artifact_selection.py` |
| Shared MCP tool naming | `backend/open_webui/utils/tools.py` (`mcp_tool_name`) |

## Tests

```bash
cd backend && python -m pytest tests/ -q          # 24 unit tests, no database
npx vitest run src/lib/utils/artifacts.test.ts   # 10 runtime/injection tests
```

## Known limitations

- The allowlist is the chat's current tool selection (updated live), not a per-message
  snapshot; a saved chat that is reopened inherits whatever its composer has selected.
- Artifacts cannot stream partial results: one call, one response.
- Embedded HTML/UI results from tools are returned as data (`embeds`); the artifact has to
  render them itself.
