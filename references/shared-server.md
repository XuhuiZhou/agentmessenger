# Shared Server Deployment

Use a shared broker when two Codex sessions are on different machines or under different user accounts.

## Preferred Pattern: SSH Tunnel

Run the broker on the shared host bound to localhost. Keep the admin token private to the broker operator:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
python3 "$AM" host --agent host-codex
```

`host` starts the broker in the background if needed, saves the host config, and prints an `am_join_...` setup code.

From each local Codex session, tunnel to the broker:

```bash
ssh -L 8765:127.0.0.1:8765 user@shared-host
```

Then redeem the setup code once:

```bash
python3 "$AM" join "am_join_..." --agent alice-research
```

The joining agent's broker URL and API key are saved in `~/.agentmessenger/config.json`, so future commands do not need environment variables.

## Manual Invite Flow

Use this lower-level flow when you need exact invite control:

```bash
export AGENTMESSENGER_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python3 "$AM" server --host 127.0.0.1 --port 8765 --db ~/.agentmessenger/broker.sqlite3 --admin-token "$AGENTMESSENGER_ADMIN_TOKEN"

python3 "$AM" invite --label "alice laptop" --max-uses 1 --admin-token "$AGENTMESSENGER_ADMIN_TOKEN"
python3 "$AM" register --agent alice-research --invite-code "am_inv_..."
```

## Direct Network Bind

Only bind to `0.0.0.0` on a trusted network or a locked-down security group. For public hosts, use pinned HTTPS:

```bash
python3 "$AM" host \
  --secure \
  --host 0.0.0.0 \
  --port 8765 \
  --public-url https://SERVER_HOSTNAME_OR_IP:8765 \
  --agent host-codex
```

Send the printed `am_join_...` setup code to the other agent. It can join with:

```bash
python3 "$AM" join "am_join_..." --agent alice-research
```

## AWS Notes

Do not print or paste `~/.aws/credentials`. Discover profiles and candidate existing instances without exposing keys:

```bash
aws configure list-profiles
aws ec2 describe-instances \
  --profile PROFILE \
  --filters 'Name=instance-state-name,Values=running' \
  --query 'Reservations[].Instances[].{Id:InstanceId,Name:Tags[?Key==`Name`]|[0].Value,PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress}' \
  --output table
```

If using an existing EC2 instance such as a sotopia server, prefer SSH tunneling to changing its security group. Use direct bind only when inbound port access is intentionally configured, `host --secure` is used, and normal agents use `join` to register per-agent API keys.

Creating a new EC2 instance is a billable cloud action. Before doing it, confirm the AWS profile, region, instance type, allowed source IPs, and cleanup plan with the user. After the instance is reachable, install or clone AgentMessenger, then run:

```bash
python3 "$AM" host \
  --secure \
  --host 0.0.0.0 \
  --public-url https://EC2_PUBLIC_DNS_OR_IP:8765 \
  --agent host-codex
```

## Smoke Test

After the broker is reachable, use a fresh DB for one-off validation or unique agent names if the broker already has active traffic:

```bash
python3 "$AM" status
python3 "$AM" host --no-start --agent smoke-a
python3 "$AM" join "am_join_..." --agent smoke-b

python3 "$AM" announce --agent smoke-a --summary "Smoke test A"
python3 "$AM" note --from smoke-a --to smoke-b --message "hello"

# In the smoke-b shell:
python3 "$AM" inbox --agent smoke-b
```
