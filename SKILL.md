---
name: agentmessenger
description: Start and use a lightweight local or shared broker for Codex-to-Codex and agent-to-agent context exchange with optional invite-based registration and per-agent API keys. Use when two or more Codex agents in separate sessions, workspaces, users, terminals, or machines need to discover each other, announce current context, register identities, create or redeem invites, request missing context, send replies, or coordinate handoffs through a server-backed message channel.
---

# AgentMessenger

Use this skill when agents need to exchange context without copying large chat logs by hand. Prefer short summaries and targeted requests; send raw files or sensitive state only when the user clearly wants that.

## Quick Start

The bundled broker is a zero-dependency Python HTTP server backed by SQLite. In shell examples, first point `AM` at the installed skill script.

For private localhost demos:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
python3 "$AM" server --host 127.0.0.1 --port 8765 --db ~/.agentmessenger/broker.sqlite3
```

For shared brokers, start with an admin token, create invite codes, and have each agent register its own API key:

```bash
export AGENTMESSENGER_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python3 "$AM" server --host 127.0.0.1 --port 8765 --db ~/.agentmessenger/broker.sqlite3 --admin-token "$AGENTMESSENGER_ADMIN_TOKEN"

python3 "$AM" invite --label "alice" --max-uses 1 --admin-token "$AGENTMESSENGER_ADMIN_TOKEN"
python3 "$AM" register --agent alice --invite-code "am_inv_..."
export AGENTMESSENGER_AGENT=alice
export AGENTMESSENGER_API_KEY=am_key_...
```

Set these in every participating Codex session:

```bash
export AGENTMESSENGER_URL=http://127.0.0.1:8765
export AGENTMESSENGER_AGENT="$(whoami)-$(basename "$PWD")"
```

Announce the local agent's current context:

```bash
python3 "$AM" announce \
  --summary "Working on the API bug in repo X; can share current findings." \
  --context-file /tmp/codex-context.md
```

Find peers:

```bash
python3 "$AM" agents
```

Ask another agent for context and wait for a reply:

```bash
python3 "$AM" ask \
  --to other-agent \
  --question "What have you learned about the failing test?" \
  --wait
```

Watch the local inbox in the other Codex session:

```bash
python3 "$AM" inbox --wait
```

Reply to a request:

```bash
python3 "$AM" reply \
  --to requesting-agent \
  --request-id m000001 \
  --message "The failure starts after the cache key change." \
  --context-file /tmp/relevant-context.md
```

## Workflow

1. Start or locate a broker. Use `status` to verify it is reachable.
2. For shared use, create an invite with `invite` and register the agent with `register`; set `AGENTMESSENGER_API_KEY`.
3. Pick a stable `AGENTMESSENGER_AGENT` name that identifies the session, user, and workspace.
4. Run `announce` with a concise summary and optional context file.
5. Use `agents` or `fetch --agent <name>` to discover available context.
6. Use `ask --to <agent> --question ... --wait` for targeted context requests.
7. In the receiving session, run `inbox --wait`, inspect the request, and respond with `reply`.

## Safety Rules

- Do not send API keys, SSH keys, tokens, private credentials, or secrets.
- Prefer summaries, file paths, command outputs, and bounded excerpts over whole transcripts.
- If binding beyond localhost, require `--admin-token`, issue per-agent API keys through invites, use a trusted network or SSH tunnel, and avoid `0.0.0.0` unless the user explicitly needs remote access.
- For shared smoke tests, use a fresh `--db` path and `--admin-token` so old messages or other clients cannot confuse the result.
- Treat broker state as coordination state. SQLite persistence helps recover from restarts, but it is not a secure long-term archive.

## Scripts

Use `scripts/agentmessenger.py` for all operations. It supports:

- `server`: start the SQLite-backed broker.
- `status`: check broker health.
- `invite`: create an invite code using the admin token.
- `invites`: list invite usage and expiry using the admin token.
- `register`: exchange an invite for a per-agent API key.
- `whoami`: show the current credential.
- `announce`: publish this session's summary and optional context.
- `agents`: list active agents.
- `fetch`: read another agent's announced context.
- `ask`: send a context request.
- `inbox`: read or wait for incoming messages.
- `reply`: respond to a request.
- `note`: send a one-way note.

Run `scripts/self_test_agentmessenger.py` after changing the broker or CLI.

Read `references/protocol.md` when changing endpoint behavior or deciding whether Redis/WebSocket support is needed. Read `references/shared-server.md` when exposing a broker through SSH, AWS, or another shared machine.
