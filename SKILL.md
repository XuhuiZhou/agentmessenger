---
name: agentmessenger
description: Start and use a lightweight local or shared broker for Codex-to-Codex and agent-to-agent context exchange with optional invite-based registration and per-agent API keys. Use when two or more Codex agents in separate sessions, workspaces, users, terminals, or machines need to discover each other, announce current context, register identities, create or redeem invites, request missing context, send replies, or coordinate handoffs through a server-backed message channel.
---

# AgentMessenger

Use this skill when agents need to exchange context without copying large chat logs by hand. Prefer short summaries and targeted requests; send raw files or sensitive state only when the user clearly wants that.

When the user asks this agent to host, first decide whether the hosting target is clear. If not, ask only the missing practical questions: where should the broker live (`local`, `SSH/shared host`, or `AWS/public VM`), what access or target should be used, and who the setup code is for. Do not ask the user to paste raw secrets; use available SSH/AWS/tool access or ask them to grant access in the environment. Reuse a healthy saved config before starting a new broker.

When the user provides an `am_join_...` setup code, treat it as an instruction to connect this agent. Do the setup yourself: run `join`, verify with `whoami`, announce the current context if useful, then offer to watch `inbox --wait`. Do not ask the user to export environment variables unless the automatic config path fails.

## Quick Start

The bundled broker is a zero-dependency Python HTTP server backed by SQLite. In shell examples, first point `AM` at the installed skill script.

Prefer the one-code setup flow when helping users connect multiple agents. The human handoff should be:

```text
Use $agentmessenger to join this setup code: am_join_...
```

For hosting, the human can simply say:

```text
Use $agentmessenger to host a secure broker for me.
```

If the human has not named a hosting target, ask for one rather than guessing. Prefer SSH tunnels or existing shared hosts for private setups; use `host --secure` for public hosts or AWS.

The host command is:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
python3 "$AM" host --agent "$(whoami)-$(basename "$PWD")"
```

This starts or reuses a broker, registers the host agent, saves `~/.agentmessenger/config.json`, and prints an `am_join_...` setup code. Send only that setup code to the other user or agent.

For public hosts or AWS, use pinned HTTPS:

```bash
python3 "$AM" host \
  --secure \
  --host 0.0.0.0 \
  --public-url https://SERVER_HOSTNAME_OR_IP:8765 \
  --agent "$(whoami)-$(basename "$PWD")"
```

`--secure` creates or reuses a self-signed certificate, embeds its SHA-256 fingerprint in the setup code, and makes the joining agent verify that fingerprint before sending the invite or API key.

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

1. If the user is hosting, inspect saved config with `config` or `status`. If the target is unclear, ask where to host and what access is available.
2. Run `host --agent <name>` for local/private hosting, or `host --secure --host 0.0.0.0 --public-url https://... --agent <name>` for public/AWS hosting. Give the printed `am_join_...` setup code to the other user.
3. If the user received a setup code, run `join "am_join_..." --agent <name>`.
4. Use `status` or `whoami` to verify the saved config works.
5. Run `announce` with a concise summary and optional context file.
6. Use `agents` or `fetch --agent <name>` to discover available context.
7. Use `ask --to <agent> --question ... --wait` for targeted context requests.
8. In the receiving session, run `inbox --wait`, inspect the request, and respond with `reply`.

## Safety Rules

- Do not send API keys, SSH keys, tokens, private credentials, or secrets.
- Prefer summaries, file paths, command outputs, and bounded excerpts over whole transcripts.
- If binding beyond localhost, prefer `host --secure` or an SSH tunnel. Avoid public plain HTTP for real conversations.
- Treat `am_join_...` setup codes as bearer invites. They include a broker URL and a one-use invite code, but not an API key.
- Pinned HTTPS protects the network path and prevents broker impersonation. The broker operator can still inspect SQLite state, so do not treat it as end-to-end encrypted storage.
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
