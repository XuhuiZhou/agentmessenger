# AgentMessenger Protocol

AgentMessenger is intentionally small: one broker, named agents, TTL-based announcements, SQLite persistence, and message inboxes.

## Transport Choice

The bundled implementation uses HTTP JSON plus SQLite from Python's standard library. This is easier for Codex sessions than WebSocket or Redis because it needs no package install, works in shell commands, can be driven with `urllib` or `curl`, and survives broker restarts.

Use Redis later when the broker must fan out to many users, coordinate multiple broker processes, or rely on managed hosted storage. A Redis version should keep the same command concepts: agent announcements, inbox messages, request IDs, consumed markers, and TTL cleanup.

Use WebSocket later when agents need streaming token-by-token updates or a UI with live presence. For Codex shell workflows, long polling through `inbox --wait` is usually enough.

## Endpoints

All bodies and responses are JSON. If the server was started with `--token`, clients must send either `X-AgentMessenger-Token: <token>` or `Authorization: Bearer <token>`.

### `GET /health`

Return broker status.

### `GET /agents`

Return active agents. Expired agents are omitted.

### `PUT /agents/<agent>`

Announce or refresh an agent.

Request fields:

- `summary`: short human-readable state.
- `workspace`: current working directory or project label.
- `context`: optional detailed context.
- `metadata`: optional object of string-ish values.
- `ttl_seconds`: optional lifetime, default 3600.

### `GET /agents/<agent>/context`

Fetch one active agent's announced context.

### `POST /messages`

Send a message.

Request fields:

- `sender`: source agent name.
- `recipient`: target agent name or `*`.
- `kind`: `context_request`, `context_response`, or `note`.
- `text`: question, answer, or note text.
- `context`: optional detailed context.
- `in_reply_to`: optional request message ID.
- `thread_id`: optional conversation ID.
- `ttl_seconds`: optional lifetime, default 3600.

### `GET /messages?agent=<agent>&wait=<seconds>&consume=1`

Read messages addressed to `agent` or `*`. `wait` enables long polling. When `consume=1`, returned messages are marked consumed for that agent.

## Message Shape

```json
{
  "id": "m000001",
  "seq": 1,
  "sender": "alice-repo",
  "recipient": "bob-repo",
  "kind": "context_request",
  "text": "What have you learned about the failing test?",
  "context": "Optional supporting context.",
  "in_reply_to": null,
  "thread_id": "m000001",
  "created_at": 1783036800.0,
  "expires_at": 1783040400.0
}
```

## SQLite Storage

The server defaults to `~/.agentmessenger/broker.sqlite3`, or `AGENTMESSENGER_DB` when set. Use `--db :memory:` only for isolated tests.

Tables:

- `agents`: one active row per announced agent.
- `messages`: all unexpired messages, ordered by `seq`.
- `message_consumed`: per-agent consumption markers, so broadcast messages can be consumed independently by each recipient.

Expired agents and messages are removed opportunistically on each broker operation.

## Cross-Machine Use

Prefer SSH tunneling:

```bash
ssh -L 8765:127.0.0.1:8765 user@shared-host
```

If binding directly:

```bash
python3 scripts/agentmessenger.py server --host 0.0.0.0 --port 8765 --token "$AGENTMESSENGER_TOKEN"
```

Share only the URL and token with trusted agents.

Run the bundled end-to-end test after protocol changes:

```bash
python3 scripts/self_test_agentmessenger.py
```
