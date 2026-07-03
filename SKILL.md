---
name: agentmessenger
description: Start and use a lightweight local broker for Codex-to-Codex or agent-to-agent context exchange. Use when two or more Codex agents in separate sessions, workspaces, users, terminals, or machines need to discover each other, announce current context, request missing context, send replies, or coordinate handoffs through a simple server-backed message channel.
---

# AgentMessenger

Use this skill when agents need to exchange context without copying large chat logs by hand. Prefer short summaries and targeted requests; send raw files or sensitive state only when the user clearly wants that.

## Quick Start

The bundled broker is a zero-dependency Python HTTP server. It is easiest to run one broker per machine or shared SSH host:

```bash
python3 scripts/agentmessenger.py server --host 127.0.0.1 --port 8765
```

Set these in every participating Codex session:

```bash
export AGENTMESSENGER_URL=http://127.0.0.1:8765
export AGENTMESSENGER_AGENT="$(whoami)-$(basename "$PWD")"
```

Announce the local agent's current context:

```bash
python3 scripts/agentmessenger.py announce \
  --summary "Working on the API bug in repo X; can share current findings." \
  --context-file /tmp/codex-context.md
```

Find peers:

```bash
python3 scripts/agentmessenger.py agents
```

Ask another agent for context and wait for a reply:

```bash
python3 scripts/agentmessenger.py ask \
  --to other-agent \
  --question "What have you learned about the failing test?" \
  --wait
```

Watch the local inbox in the other Codex session:

```bash
python3 scripts/agentmessenger.py inbox --wait
```

Reply to a request:

```bash
python3 scripts/agentmessenger.py reply \
  --to requesting-agent \
  --request-id m000001 \
  --message "The failure starts after the cache key change." \
  --context-file /tmp/relevant-context.md
```

## Workflow

1. Start or locate a broker. Use `status` to verify it is reachable.
2. Pick a stable `AGENTMESSENGER_AGENT` name that identifies the session, user, and workspace.
3. Run `announce` with a concise summary and optional context file.
4. Use `agents` or `fetch --agent <name>` to discover available context.
5. Use `ask --to <agent> --question ... --wait` for targeted context requests.
6. In the receiving session, run `inbox --wait`, inspect the request, and respond with `reply`.

## Safety Rules

- Do not send API keys, SSH keys, tokens, private credentials, or secrets.
- Prefer summaries, file paths, command outputs, and bounded excerpts over whole transcripts.
- If binding beyond localhost, require `--token`, use a trusted network or SSH tunnel, and avoid `0.0.0.0` unless the user explicitly needs remote access.
- Treat broker state as ephemeral coordination state, not durable storage.

## Scripts

Use `scripts/agentmessenger.py` for all operations. It supports:

- `server`: start the broker.
- `status`: check broker health.
- `announce`: publish this session's summary and optional context.
- `agents`: list active agents.
- `fetch`: read another agent's announced context.
- `ask`: send a context request.
- `inbox`: read or wait for incoming messages.
- `reply`: respond to a request.
- `note`: send a one-way note.

Read `references/protocol.md` when changing the broker, debugging endpoint behavior, exposing it across machines, or deciding whether Redis/WebSocket support is needed.
