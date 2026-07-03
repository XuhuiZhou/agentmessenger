# Shared Server Deployment

Use a shared broker when two Codex sessions are on different machines or under different user accounts.

## Preferred Pattern: SSH Tunnel

Run the broker on the shared host bound to localhost:

```bash
AM="${CODEX_HOME:-$HOME/.codex}/skills/agentmessenger/scripts/agentmessenger.py"
mkdir -p ~/.agentmessenger
export AGENTMESSENGER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
nohup python3 "$AM" server \
  --host 127.0.0.1 \
  --port 8765 \
  --db ~/.agentmessenger/broker.sqlite3 \
  --token "$AGENTMESSENGER_TOKEN" \
  > ~/.agentmessenger/server.log 2>&1 &
```

From each local Codex session:

```bash
ssh -L 8765:127.0.0.1:8765 user@shared-host
export AGENTMESSENGER_URL=http://127.0.0.1:8765
export AGENTMESSENGER_TOKEN="<token-from-shared-host>"
```

## Direct Network Bind

Only bind to `0.0.0.0` on a trusted network or a locked-down security group:

```bash
python3 "$AM" server \
  --host 0.0.0.0 \
  --port 8765 \
  --db ~/.agentmessenger/broker.sqlite3 \
  --token "$AGENTMESSENGER_TOKEN"
```

Then connect with:

```bash
export AGENTMESSENGER_URL=http://SERVER_HOSTNAME_OR_IP:8765
export AGENTMESSENGER_TOKEN="<shared-token>"
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

If using an existing EC2 instance such as a sotopia server, prefer SSH tunneling to changing its security group. Use direct bind only when inbound port access is intentionally configured and token auth is enabled.

## Smoke Test

After the broker is reachable, use a fresh DB for one-off validation or unique agent names if the broker already has active traffic:

```bash
python3 "$AM" status
python3 "$AM" announce --agent smoke-a --summary "Smoke test A"
python3 "$AM" note --from smoke-a --to smoke-b --message "hello"
python3 "$AM" inbox --agent smoke-b
```
