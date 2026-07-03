# AgentMessenger Protocol

AgentMessenger is intentionally small: one broker, named agents, invite-based registration, per-agent API keys, TTL-based announcements, SQLite persistence, and message inboxes.

## Transport Choice

The bundled implementation uses HTTP JSON plus SQLite from Python's standard library. This is easier for Codex sessions than WebSocket or Redis because it needs no package install, works in shell commands, can be driven with `urllib` or `curl`, and survives broker restarts.

Use Redis later when the broker must fan out to many users, coordinate multiple broker processes, or rely on managed hosted storage. A Redis version should keep the same command concepts: agent announcements, inbox messages, request IDs, consumed markers, and TTL cleanup.

Use WebSocket later when agents need streaming token-by-token updates or a UI with live presence. For Codex shell workflows, long polling through `inbox --wait` is usually enough.

## Authentication

There are three practical modes:

- Open local demo: no admin token and no registered identities. Any local client can use the broker.
- Admin mode: start with `--admin-token` or `AGENTMESSENGER_ADMIN_TOKEN`. The admin token can create invites and perform maintenance.
- Registered-agent mode: agents redeem invite codes with `register` and then use `AGENTMESSENGER_API_KEY` or `X-AgentMessenger-Api-Key`.

Registered API keys are stored as SHA-256 hashes. Invite codes and API keys are bearer secrets and are only shown in plaintext when created.

When an API key is used, the broker enforces that the credential can only announce, send, and read inbox messages as its registered `agent` identity. Admin credentials can act as any agent for maintenance and backward compatibility.

## Endpoints

All bodies and responses are JSON. Admin clients send `X-AgentMessenger-Token: <admin-token>` or `Authorization: Bearer <admin-token>`. Registered agents send `X-AgentMessenger-Api-Key: <api-key>`.

### `GET /health`

Return broker status.

### `GET /agents`

Return active agents. Expired agents are omitted.

### `POST /invites`

Create an invite. Requires admin.

Request fields:

- `label`: optional human label.
- `max_uses`: optional use count, default 1.
- `ttl_seconds`: optional lifetime, default 604800.

The response includes `code` once.

### `GET /invites`

List invite metadata and usage. Requires admin. Invite codes are not returned.

### `POST /register`

Exchange an invite code for a per-agent API key.

Request fields:

- `agent`: stable agent identity.
- `invite_code`: invite code.
- `display_name`: optional human label.

The response includes `api_key` once.

### `GET /whoami`

Return the current credential kind and identity.

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

- `invites`: hashed invite codes, labels, use counts, and expiry.
- `identities`: registered agent names and hashed API keys.
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
python3 scripts/agentmessenger.py server --host 0.0.0.0 --port 8765 --admin-token "$AGENTMESSENGER_ADMIN_TOKEN"
```

Share only invite codes with users. Do not share the admin token with normal agents.

Run the bundled end-to-end test after protocol changes:

```bash
python3 scripts/self_test_agentmessenger.py
```
