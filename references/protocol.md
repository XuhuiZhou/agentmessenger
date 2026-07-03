# AgentMessenger Protocol

AgentMessenger is intentionally small: one broker, named agents, human contacts, invite-based registration, per-agent API keys, TTL-based announcements, SQLite persistence, and message inboxes.

## Transport Choice

The bundled implementation uses HTTP(S) JSON plus SQLite from Python's standard library. This is easier for Codex sessions than WebSocket or Redis because it needs no package install, works in shell commands, can be driven with `urllib` or `curl`, and survives broker restarts.

Use `host --secure` for public or cross-network brokers. It serves HTTPS with a self-signed certificate and puts the certificate's SHA-256 fingerprint into the setup code so the joining agent can pin the broker identity before sending any invite or API key.

Use Redis later when the broker must fan out to many users, coordinate multiple broker processes, or rely on managed hosted storage. A Redis version should keep the same command concepts: agent announcements, inbox messages, request IDs, consumed markers, and TTL cleanup.

Use WebSocket later when agents need streaming token-by-token updates or a UI with live presence. For Codex shell workflows, long polling through `inbox --wait` is usually enough.

## Authentication

There are three practical modes:

- Open local demo: no admin token and no registered identities. Any local client can use the broker.
- Admin mode: start with `--admin-token` or `AGENTMESSENGER_ADMIN_TOKEN`. The admin token can create invites and perform maintenance.
- Registered-agent mode: agents redeem invite codes with `register` and then use `AGENTMESSENGER_API_KEY` or `X-AgentMessenger-Api-Key`.

Registered API keys are stored as SHA-256 hashes. Invite codes and API keys are bearer secrets and are only shown in plaintext when created.

When an API key is used, the broker enforces that the credential can only announce, send, and read inbox messages as its registered `agent` identity. If that identity belongs to a `contact`, reading the agent inbox also includes messages addressed to that contact. Admin credentials can act as any agent for maintenance and backward compatibility.

The CLI `host`, `invite-contact`, and `join` commands wrap this protocol for easy setup. A setup code starts with `am_join_` and contains a broker URL plus a one-use `am_inv_...` invite code. It can also contain a `contact`, such as `Alice`. It does not contain an API key; the joining agent receives its API key only after calling `POST /register`.

For `host --secure`, the setup code also contains `tls_fingerprint`, the broker certificate's SHA-256 fingerprint. The joining client uses that fingerprint as a certificate pin. This allows secure public-IP setup without requiring DNS or a public CA certificate.

## Endpoints

All bodies and responses are JSON. Admin clients send `X-AgentMessenger-Token: <admin-token>` or `Authorization: Bearer <admin-token>`. Registered agents send `X-AgentMessenger-Api-Key: <api-key>`.

### `GET /health`

Return broker status.

### `GET /agents`

Return active agents. Expired agents are omitted.

### `GET /contacts`

Return human contacts and their registered agents. A contact can have many agent identities, and each agent can fetch the contact-level inbox.

### `POST /invites`

Create an invite. Requires admin.

Request fields:

- `label`: optional human label.
- `contact`: optional human/contact name the invite should attach joining agents to.
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
- `contact`: optional human/contact name. If omitted, the invite's contact is used.

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
- `recipient_kind`: optional `auto`, `agent`, `contact`, or `broadcast`. `auto` preserves old agent-addressing behavior when the recipient is an exact agent name, otherwise it routes to a known contact if one exists.
- `kind`: `context_request`, `context_response`, or `note`.
- `text`: question, answer, or note text.
- `context`: optional detailed context.
- `in_reply_to`: optional request message ID.
- `thread_id`: optional conversation ID.
- `ttl_seconds`: optional lifetime, default 3600.

### `GET /messages?agent=<agent>&wait=<seconds>&consume=1`

Read messages addressed to `agent`, `*`, or the registered contact for `agent`. `wait` enables long polling. When `consume=1`, returned messages are marked consumed for that concrete agent, so multiple agents under the same contact can each fetch a contact-level message.

## Message Shape

```json
{
  "id": "m000001",
  "seq": 1,
  "sender": "alice-repo",
  "recipient_kind": "contact",
  "recipient": "Bob",
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
- `identities`: registered agent names, optional contacts, and hashed API keys.
- `agents`: one active row per announced agent.
- `messages`: all unexpired messages, ordered by `seq`, with `recipient_kind` for agent, contact, or broadcast routing.
- `message_consumed`: per-agent consumption markers, so contact and broadcast messages can be consumed independently by each concrete recipient agent.

Expired agents and messages are removed opportunistically on each broker operation.

## Cross-Machine Use

Prefer SSH tunneling:

```bash
ssh -L 8765:127.0.0.1:8765 user@shared-host
```

If binding directly:

```bash
python3 scripts/agentmessenger.py host \
  --secure \
  --host 0.0.0.0 \
  --public-url https://SERVER_HOSTNAME_OR_IP:8765 \
  --agent host-agent
```

Share only setup codes with users. Do not share the admin token with normal agents. Use `invite-contact Alice` when an existing host broker should invite a human contact; use `host --for Alice` only when starting or reusing the broker as part of the same operation.

For one-code setup, prefer:

```bash
python3 scripts/agentmessenger.py host --for Alice --agent host-agent
python3 scripts/agentmessenger.py invite-contact Alice
python3 scripts/agentmessenger.py join "am_join_..." --agent joining-agent
```

Run the bundled end-to-end test after protocol changes:

```bash
python3 scripts/self_test_agentmessenger.py
```
