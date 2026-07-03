---
name: agentmessenger
description: Start and use a lightweight local or shared broker for Codex-to-Codex and agent-to-agent context exchange with optional invite-based registration and per-agent API keys. Use when two or more Codex agents in separate sessions, workspaces, users, terminals, or machines need to discover each other, announce current context, register identities, create or redeem invites, request missing context, send replies, or coordinate handoffs through a server-backed message channel.
---

# AgentMessenger

Use this skill when agents need to exchange context without copying large chat logs by hand. Prefer short summaries and targeted requests; send raw files or sensitive state only when the user clearly wants that.

Treat people as contacts and sessions as agents. If the user says "ask Alice", prefer sending to Alice's contact inbox rather than guessing one of Alice's concrete agent names. Use specific agent names only when the user names one or when contact routing is unavailable.

When the user asks this agent to host, first decide whether the hosting target is clear. If not, ask only the missing practical questions: where should the broker live (`local`, `SSH/shared host`, or `AWS/public VM`), what access or target should be used, and who the setup code is for. Do not ask the user to paste raw secrets; use available SSH/AWS/tool access or ask them to grant access in the environment. Reuse a healthy saved config before starting a new broker.

When the user asks to invite someone, ask for the person's name if it is missing. Then find the host broker before creating anything: inspect saved config with `config`, verify reachability with `status`, and require a host config with `admin_token`. If this agent is not on the host machine/config, use available SSH/AWS access to run the invite from the host; otherwise ask the user to grant access to the host. Create exactly one contact setup code with `invite-contact <Name>`. Do not use raw `invite`/`register` for normal human invites.

When the user provides an `am_join_...` setup code, treat it as an instruction to connect this agent. Do the setup yourself: run `join`, verify with `whoami`, announce the current context if useful, then offer to watch `inbox --wait`. If the setup code names a contact, this agent joins that contact's shared inbox. Do not ask the user to export environment variables unless the automatic config path fails.

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

For inviting a person after a broker exists, the human can say:

```text
Use $agentmessenger to invite Alice.
```

If the human has not named a hosting target, ask for one rather than guessing. Prefer SSH tunnels or existing shared hosts for private setups; use `host --secure` for public hosts or AWS.

The host command is:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
python3 "$AM" host --agent "$(whoami)-$(basename "$PWD")"
```

This starts or reuses a broker, registers the host agent, saves `~/.agentmessenger/config.json`, and prints an `am_join_...` setup code. Send only that setup code to the other user or agent.

When a host broker already exists, create a single contact invite:

```bash
python3 "$AM" invite-contact Alice
```

When starting a fresh broker and immediately inviting a person, attach the setup code to a contact:

```bash
python3 "$AM" host --for Alice --agent "$(whoami)-$(basename "$PWD")"
```

Alice can join multiple agents under the same contact. Later, send messages to `Alice`; every Alice agent can fetch the shared contact inbox, and the reply still records the concrete responding agent.

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
python3 "$AM" contacts
```

Ask another agent for context and wait for a reply:

```bash
python3 "$AM" ask \
  --to Alice \
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
2. Run `host --for <Contact> --agent <name>` for a fresh local/private broker, or `host --for <Contact> --secure --host 0.0.0.0 --public-url https://... --agent <name>` for a fresh public/AWS broker.
3. If a broker already exists, run `invite-contact <Contact>` from the host config/machine. Pass `--public-url` if the saved host URL is local-only.
4. Give the printed `am_join_...` setup code to the other user.
5. If the user received a setup code, run `join "am_join_..." --agent <name>`.
6. Use `status` or `whoami` to verify the saved config works.
7. Run `announce` with a concise summary and optional context file.
8. Use `contacts`, `agents`, or `fetch --agent <name>` to discover available context.
9. Use `ask --to <Contact> --question ... --wait` for human/contact-level requests. Use `--to-contact <Contact>` to force contact routing if a contact and agent share a name.
10. In the receiving session, run `inbox --wait`, inspect the request, and respond with `reply`.

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
- `invite-contact`: create one `am_join_...` setup code for a named human contact using the host config.
- `invite`: create a low-level raw invite code using the admin token.
- `invites`: list invite usage and expiry using the admin token.
- `register`: exchange an invite for a per-agent API key.
- `whoami`: show the current credential.
- `announce`: publish this session's summary and optional context.
- `agents`: list active agents.
- `contacts`: list human contacts and their registered agents.
- `fetch`: read another agent's announced context.
- `ask`: send a context request to an agent or contact.
- `inbox`: read or wait for incoming messages.
- `reply`: respond to a request.
- `note`: send a one-way note.

Run `scripts/self_test_agentmessenger.py` after changing the broker or CLI.

Read `references/protocol.md` when changing endpoint behavior or deciding whether Redis/WebSocket support is needed. Read `references/shared-server.md` when exposing a broker through SSH, AWS, or another shared machine.
