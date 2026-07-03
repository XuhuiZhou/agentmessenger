#!/usr/bin/env python3
"""Small HTTP broker and CLI for Codex-to-Codex context exchange."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TTL = 3600
MAX_WAIT_SECONDS = 120


def now() -> float:
    return time.time()


def clean_agent_name(value: str) -> str:
    value = value.strip() or "agent"
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
    return value.strip("-") or "agent"


def clean_recipient(value: str) -> str:
    value = value.strip()
    if value == "*":
        return "*"
    return clean_agent_name(value)


def default_agent_name() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "codex"
    cwd = Path.cwd().name or socket.gethostname()
    return clean_agent_name(f"{user}-{cwd}")


def read_context(text: str | None, file_path: str | None) -> str:
    parts: list[str] = []
    if text:
        parts.append(text)
    if file_path:
        parts.append(Path(file_path).expanduser().read_text(encoding="utf-8"))
    return "\n\n".join(part.strip() for part in parts if part.strip())


def parse_metadata(items: list[str] | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"metadata must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def parse_since(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"(\d+)$", value)
    if not match:
        raise ValueError(f"cannot parse message sequence from {value!r}")
    return int(match.group(1))


class BrokerState:
    def __init__(self, state_file: str | None = None) -> None:
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.agents: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.next_seq = 1
        self.load()

    def load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.agents = data.get("agents", {})
        self.messages = data.get("messages", [])
        self.next_seq = int(data.get("next_seq", 1))

    def save(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agents": self.agents,
            "messages": self.messages,
            "next_seq": self.next_seq,
        }
        self.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def cleanup(self) -> None:
        current = now()
        self.agents = {
            name: agent
            for name, agent in self.agents.items()
            if agent.get("expires_at", 0) > current
        }
        self.messages = [
            message
            for message in self.messages
            if message.get("expires_at", 0) > current
        ]

    def announce(self, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.cleanup()
            ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL)
            timestamp = now()
            agent = {
                "name": agent_name,
                "summary": str(payload.get("summary") or ""),
                "workspace": str(payload.get("workspace") or ""),
                "context": str(payload.get("context") or ""),
                "metadata": payload.get("metadata") or {},
                "updated_at": timestamp,
                "expires_at": timestamp + ttl,
            }
            self.agents[agent_name] = agent
            self.save()
            self.condition.notify_all()
            return agent

    def list_agents(self) -> list[dict[str, Any]]:
        with self.lock:
            self.cleanup()
            return sorted(self.agents.values(), key=lambda item: item["name"])

    def fetch_agent(self, agent_name: str) -> dict[str, Any] | None:
        with self.lock:
            self.cleanup()
            return self.agents.get(agent_name)

    def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.cleanup()
            ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL)
            seq = self.next_seq
            self.next_seq += 1
            message_id = f"m{seq:06d}"
            thread_id = str(payload.get("thread_id") or payload.get("in_reply_to") or message_id)
            timestamp = now()
            message = {
                "id": message_id,
                "seq": seq,
                "sender": clean_agent_name(str(payload.get("sender") or "unknown")),
                "recipient": clean_recipient(str(payload.get("recipient") or "*")),
                "kind": str(payload.get("kind") or "note"),
                "text": str(payload.get("text") or ""),
                "context": str(payload.get("context") or ""),
                "in_reply_to": payload.get("in_reply_to"),
                "thread_id": thread_id,
                "created_at": timestamp,
                "expires_at": timestamp + ttl,
                "consumed_by": [],
            }
            self.messages.append(message)
            self.save()
            self.condition.notify_all()
            return message

    def get_messages(
        self,
        agent_name: str,
        since: int = 0,
        wait_seconds: float = 0,
        consume: bool = False,
        include_consumed: bool = False,
        in_reply_to: str | None = None,
    ) -> list[dict[str, Any]]:
        deadline = now() + min(max(wait_seconds, 0), MAX_WAIT_SECONDS)
        with self.condition:
            while True:
                self.cleanup()
                messages = [
                    message
                    for message in self.messages
                    if message["seq"] > since
                    and (message["recipient"] in {agent_name, "*"})
                    and (include_consumed or agent_name not in message.get("consumed_by", []))
                    and (not in_reply_to or message.get("in_reply_to") == in_reply_to)
                ]
                if messages or now() >= deadline:
                    if consume and messages:
                        for message in messages:
                            consumed_by = message.setdefault("consumed_by", [])
                            if agent_name not in consumed_by:
                                consumed_by.append(agent_name)
                        self.save()
                    return [public_message(message) for message in messages]
                self.condition.wait(timeout=max(0.1, deadline - now()))


def public_message(message: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in message.items() if key != "consumed_by"}


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "AgentMessenger/0.1"

    @property
    def broker(self) -> BrokerState:
        return self.server.broker  # type: ignore[attr-defined]

    @property
    def token(self) -> str | None:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if not getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        if not self.authorized():
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/health":
                self.write_json({"ok": True, "service": "agentmessenger", "time": now()})
            elif path == "/agents":
                self.write_json({"agents": self.broker.list_agents()})
            elif path.startswith("/agents/") and path.endswith("/context"):
                agent_name = urllib.parse.unquote(path.split("/")[2])
                agent = self.broker.fetch_agent(agent_name)
                if agent is None:
                    self.write_json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.write_json({"agent": agent})
            elif path == "/messages":
                agent_name = clean_agent_name(one(query, "agent", default_agent_name()))
                wait_seconds = float(one(query, "wait", "0"))
                since = parse_since(one(query, "since", "0"))
                consume = one(query, "consume", "0") in {"1", "true", "yes"}
                include_consumed = one(query, "include_consumed", "0") in {"1", "true", "yes"}
                in_reply_to = one(query, "in_reply_to", None)
                messages = self.broker.get_messages(
                    agent_name,
                    since=since,
                    wait_seconds=wait_seconds,
                    consume=consume,
                    include_consumed=include_consumed,
                    in_reply_to=in_reply_to,
                )
                self.write_json({"messages": messages})
            else:
                self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - surfaced to CLI.
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        self.handle_write()

    def do_PUT(self) -> None:
        self.handle_write()

    def handle_write(self) -> None:
        if not self.authorized():
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self.read_json()
            if path.startswith("/agents/"):
                agent_name = clean_agent_name(urllib.parse.unquote(path.split("/")[2]))
                agent = self.broker.announce(agent_name, body)
                self.write_json({"agent": agent})
            elif path == "/messages":
                message = self.broker.send_message(body)
                self.write_json({"message": public_message(message)})
            else:
                self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - surfaced to CLI.
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def authorized(self) -> bool:
        if not self.token:
            return True
        bearer = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-AgentMessenger-Token")
        return header_token == self.token or bearer == f"Bearer {self.token}"

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def one(query: dict[str, list[str]], key: str, default: str | None = "") -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def run_server(args: argparse.Namespace) -> int:
    broker = BrokerState(args.state)
    server = ThreadingHTTPServer((args.host, args.port), BrokerHandler)
    server.broker = broker  # type: ignore[attr-defined]
    server.token = args.token or os.environ.get("AGENTMESSENGER_TOKEN")  # type: ignore[attr-defined]
    server.quiet = args.quiet  # type: ignore[attr-defined]

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    url = f"http://{args.host}:{args.port}"
    print(f"AgentMessenger broker listening at {url}", flush=True)
    if server.token:  # type: ignore[attr-defined]
        print("Token auth is enabled. Set AGENTMESSENGER_TOKEN in each client session.", flush=True)
    elif args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Warning: no token configured for a non-localhost bind.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAgentMessenger broker stopped.", flush=True)
    finally:
        server.server_close()
    return 0


class Client:
    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-AgentMessenger-Token"] = self.token
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=MAX_WAIT_SECONDS + 10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise SystemExit(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Could not reach AgentMessenger broker at {self.url}: {exc.reason}") from exc


def make_client(args: argparse.Namespace) -> Client:
    return Client(args.url or os.environ.get("AGENTMESSENGER_URL", DEFAULT_URL), args.token or os.environ.get("AGENTMESSENGER_TOKEN"))


def cmd_status(args: argparse.Namespace) -> int:
    result = make_client(args).request("GET", "/health")
    emit(args, result, "AgentMessenger broker is reachable.")
    return 0


def cmd_announce(args: argparse.Namespace) -> int:
    agent = clean_agent_name(args.agent or os.environ.get("AGENTMESSENGER_AGENT") or default_agent_name())
    payload = {
        "summary": args.summary,
        "workspace": args.workspace or str(Path.cwd()),
        "context": read_context(args.context, args.context_file),
        "metadata": parse_metadata(args.meta),
        "ttl_seconds": args.ttl,
    }
    result = make_client(args).request("PUT", f"/agents/{urllib.parse.quote(agent)}", payload)
    emit(args, result, f"Announced {agent}.")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    result = make_client(args).request("GET", "/agents")
    if args.json:
        emit(args, result)
        return 0
    agents = result.get("agents", [])
    if not agents:
        print("No active agents.")
        return 0
    for agent in agents:
        updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(agent["updated_at"]))
        print(f"{agent['name']}  updated={updated}")
        if agent.get("workspace"):
            print(f"  workspace: {agent['workspace']}")
        if agent.get("summary"):
            print(f"  summary: {agent['summary']}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    agent = clean_agent_name(args.agent)
    result = make_client(args).request("GET", f"/agents/{urllib.parse.quote(agent)}/context")
    if args.json:
        emit(args, result)
        return 0
    agent_payload = result["agent"]
    print(f"{agent_payload['name']}")
    if agent_payload.get("workspace"):
        print(f"workspace: {agent_payload['workspace']}")
    if agent_payload.get("summary"):
        print(f"summary: {agent_payload['summary']}")
    if agent_payload.get("context"):
        print("\ncontext:")
        print(agent_payload["context"])
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    sender = clean_agent_name(args.from_agent or os.environ.get("AGENTMESSENGER_AGENT") or default_agent_name())
    recipient = clean_recipient(args.to)
    payload = {
        "sender": sender,
        "recipient": recipient,
        "kind": "context_request",
        "text": args.question,
        "context": read_context(args.context, args.context_file),
        "ttl_seconds": args.ttl,
    }
    client = make_client(args)
    result = client.request("POST", "/messages", payload)
    message = result["message"]
    if args.json and not args.wait:
        emit(args, result)
    elif not args.wait:
        print(f"Sent request {message['id']} to {recipient}.")
    if args.wait:
        wait_path = (
            f"/messages?agent={urllib.parse.quote(sender)}"
            f"&wait={int(args.timeout)}&consume=1"
            f"&in_reply_to={urllib.parse.quote(message['id'])}"
        )
        responses = client.request("GET", wait_path)
        if args.json:
            emit(args, {"request": message, "responses": responses.get("messages", [])})
        else:
            print(f"Sent request {message['id']} to {recipient}.")
            print_messages(responses.get("messages", []))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    agent = clean_agent_name(args.agent or os.environ.get("AGENTMESSENGER_AGENT") or default_agent_name())
    wait = args.timeout if args.wait else 0
    consume = "0" if args.no_consume else "1"
    include = "1" if args.include_consumed else "0"
    path = (
        f"/messages?agent={urllib.parse.quote(agent)}"
        f"&wait={int(wait)}&consume={consume}"
        f"&include_consumed={include}&since={urllib.parse.quote(args.since or '0')}"
    )
    result = make_client(args).request("GET", path)
    if args.json:
        emit(args, result)
    else:
        print_messages(result.get("messages", []))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    sender = clean_agent_name(args.from_agent or os.environ.get("AGENTMESSENGER_AGENT") or default_agent_name())
    recipient = clean_recipient(args.to)
    payload = {
        "sender": sender,
        "recipient": recipient,
        "kind": "context_response",
        "text": args.message,
        "context": read_context(args.context, args.context_file),
        "in_reply_to": args.request_id,
        "thread_id": args.request_id,
        "ttl_seconds": args.ttl,
    }
    result = make_client(args).request("POST", "/messages", payload)
    emit(args, result, f"Sent reply {result['message']['id']} to {recipient}.")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    sender = clean_agent_name(args.from_agent or os.environ.get("AGENTMESSENGER_AGENT") or default_agent_name())
    recipient = clean_recipient(args.to)
    payload = {
        "sender": sender,
        "recipient": recipient,
        "kind": "note",
        "text": args.message,
        "context": read_context(args.context, args.context_file),
        "ttl_seconds": args.ttl,
    }
    result = make_client(args).request("POST", "/messages", payload)
    emit(args, result, f"Sent note {result['message']['id']} to {recipient}.")
    return 0


def emit(args: argparse.Namespace, payload: dict[str, Any], text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif text:
        print(text)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def print_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        print("No messages.")
        return
    for message in messages:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(message["created_at"]))
        print(f"{message['id']} {message['kind']} from={message['sender']} to={message['recipient']} at={timestamp}")
        if message.get("in_reply_to"):
            print(f"  in_reply_to: {message['in_reply_to']}")
        if message.get("text"):
            print(f"  text: {message['text']}")
        if message.get("context"):
            print("  context:")
            for line in message["context"].splitlines():
                print(f"    {line}")


def add_client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.environ.get("AGENTMESSENGER_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("AGENTMESSENGER_TOKEN"))
    parser.add_argument("--json", action="store_true", help="emit JSON")


def add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context", help="inline context to include")
    parser.add_argument("--context-file", help="file containing context to include")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentMessenger broker and CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="start broker")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--token", default=os.environ.get("AGENTMESSENGER_TOKEN"))
    server.add_argument("--state", help="optional JSON state file")
    server.add_argument("--quiet", action="store_true")
    server.set_defaults(func=run_server)

    status = subparsers.add_parser("status", help="check broker health")
    add_client_options(status)
    status.set_defaults(func=cmd_status)

    announce = subparsers.add_parser("announce", help="announce this agent's context")
    add_client_options(announce)
    announce.add_argument("--agent", default=os.environ.get("AGENTMESSENGER_AGENT"))
    announce.add_argument("--summary", required=True)
    announce.add_argument("--workspace", default=str(Path.cwd()))
    announce.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    announce.add_argument("--meta", action="append", help="metadata as KEY=VALUE")
    add_context_options(announce)
    announce.set_defaults(func=cmd_announce)

    agents = subparsers.add_parser("agents", help="list active agents")
    add_client_options(agents)
    agents.set_defaults(func=cmd_agents)

    fetch = subparsers.add_parser("fetch", help="fetch an agent's announced context")
    add_client_options(fetch)
    fetch.add_argument("--agent", required=True)
    fetch.set_defaults(func=cmd_fetch)

    ask = subparsers.add_parser("ask", help="ask another agent for context")
    add_client_options(ask)
    ask.add_argument("--from", dest="from_agent", default=os.environ.get("AGENTMESSENGER_AGENT"))
    ask.add_argument("--to", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--wait", action="store_true")
    ask.add_argument("--timeout", type=int, default=60)
    ask.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    add_context_options(ask)
    ask.set_defaults(func=cmd_ask)

    inbox = subparsers.add_parser("inbox", help="read this agent's inbox")
    add_client_options(inbox)
    inbox.add_argument("--agent", default=os.environ.get("AGENTMESSENGER_AGENT"))
    inbox.add_argument("--wait", action="store_true")
    inbox.add_argument("--timeout", type=int, default=60)
    inbox.add_argument("--since", default="0")
    inbox.add_argument("--include-consumed", action="store_true")
    inbox.add_argument("--no-consume", action="store_true")
    inbox.set_defaults(func=cmd_inbox)

    reply = subparsers.add_parser("reply", help="reply to a context request")
    add_client_options(reply)
    reply.add_argument("--from", dest="from_agent", default=os.environ.get("AGENTMESSENGER_AGENT"))
    reply.add_argument("--to", required=True)
    reply.add_argument("--request-id", required=True)
    reply.add_argument("--message", required=True)
    reply.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    add_context_options(reply)
    reply.set_defaults(func=cmd_reply)

    note = subparsers.add_parser("note", help="send a one-way note")
    add_client_options(note)
    note.add_argument("--from", dest="from_agent", default=os.environ.get("AGENTMESSENGER_AGENT"))
    note.add_argument("--to", required=True)
    note.add_argument("--message", required=True)
    note.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    add_context_options(note)
    note.set_defaults(func=cmd_note)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
