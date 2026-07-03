---
name: agentmessenger
description: Start and use a lightweight local or shared broker for Codex-to-Codex and agent-to-agent context exchange with optional invite-based registration and per-agent API keys. Use when two or more Codex agents in separate sessions, workspaces, users, terminals, or machines need to discover each other, announce current context, register identities, create or redeem invites, request missing context, send replies, or coordinate handoffs through a server-backed message channel.
---

# AgentMessenger

Use this skill when agents need to exchange context without copying large chat logs by hand. Prefer short summaries and targeted requests; send raw files or sensitive state only when the user clearly wants that.

## Quick Start

The bundled broker is a zero-dependency Python HTTP server backed by SQLite. In shell examples, first point `AM` at the installed skill script.

Prefer the one-code setup flow when helping users connect multiple agents. The host side runs:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
python3 "$AM" host --agent "$(whoami)-$(basename "$PWD")"
```

This starts or reuses a broker, registers the host agent, saves `~/.agentmessenger/config.json`, and prints an `am_join_...` setup code. Send only that setup code to the other user or agent.

The joining side runs:

```bash
python3 "$AM" join "am_join_..." --agent "$(whoami)-$(basename "$PWD")"
```

After `host` or `join`, normal commands read the saved config automatically. Use `config` to inspect it with secrets redacted:

```bash
python3 "$AM" whoami
python3 "$AM" config
```

For private localhost demos without saved config:

```bash
python3 "$AM" server --host 127.0.0.1 --port 8765 --db ~/.agentmessenger/broker.sqlite3
```

For manual shared brokers, start with an admin token, create invite codes, and have each agent register its own API key. Prefer `host` and `join` unless the user explicitly wants the lower-level flow.

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

1. If the user is hosting, run `host --agent <name>` and give the printed `am_join_...` setup code to the other user.
2. If the user received a setup code, run `join "am_join_..." --agent <name>`.
3. Use `status` or `whoami` to verify the saved config works.
4. Run `announce` with a concise summary and optional context file.
5. Use `agents` or `fetch --agent <name>` to discover available context.
6. Use `ask --to <agent> --question ... --wait` for targeted context requests.
7. In the receiving session, run `inbox --wait`, inspect the request, and respond with `reply`.

## Safety Rules

- Do not send API keys, SSH keys, tokens, private credentials, or secrets.
- Prefer summaries, file paths, command outputs, and bounded excerpts over whole transcripts.
- If binding beyond localhost, require `--admin-token`, issue per-agent API keys through invites, use a trusted network or SSH tunnel, and avoid `0.0.0.0` unless the user explicitly needs remote access.
- Treat `am_join_...` setup codes as bearer invites. They include a broker URL and a one-use invite code, but not an API key.
- For shared smoke tests, use a fresh `--db` path and `--admin-token` so old messages or other clients cannot confuse the result.
- Treat broker state as coordination state. SQLite persistence helps recover from restarts, but it is not a secure long-term archive.

## Scripts

Use `scripts/agentmessenger.py` for all operations. It supports:

- `server`: start the SQLite-backed broker.
- `status`: check broker health.
- `host`: start or reuse a broker, register the host agent, save local config, and print a one-use `am_join_...` setup code.
- `join`: redeem an `am_join_...` setup code or raw `am_inv_...` invite and save local config.
- `config`: show saved local config with secrets redacted.
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
