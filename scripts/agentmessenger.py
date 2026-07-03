#!/usr/bin/env python3
"""Small SQLite-backed broker and CLI for Codex-to-Codex context exchange."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
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
import secrets


DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_DB = "~/.agentmessenger/broker.sqlite3"
DEFAULT_CONFIG = "~/.agentmessenger/config.json"
DEFAULT_LOG = "~/.agentmessenger/server.log"
DEFAULT_TLS_CERT = "~/.agentmessenger/server.crt"
DEFAULT_TLS_KEY = "~/.agentmessenger/server.key"
DEFAULT_TTL = 3600
DEFAULT_INVITE_TTL = 7 * 24 * 3600
MAX_WAIT_SECONDS = 120
JOIN_CODE_PREFIX = "am_join_"
VERSION = "0.5.0"


class BrokerError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


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


def default_db_path() -> str:
    return os.environ.get("AGENTMESSENGER_DB", DEFAULT_DB)


def resolve_db_path(value: str | None) -> str:
    if not value:
        value = default_db_path()
    if value == ":memory:":
        return value
    return str(Path(value).expanduser())


def resolve_config_path(value: str | None = None) -> Path:
    return Path(value or os.environ.get("AGENTMESSENGER_CONFIG", DEFAULT_CONFIG)).expanduser()


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = resolve_config_path(str(path) if path else None)
    if not config_path.exists():
        return {}
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid AgentMessenger config at {config_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit(f"Invalid AgentMessenger config at {config_path}: expected object")
    return loaded


def save_config(config: dict[str, Any], path: str | Path | None = None) -> Path:
    config_path = resolve_config_path(str(path) if path else None)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return config_path


def normalize_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Fa-f0-9]", "", value)
    if not cleaned:
        return None
    if len(cleaned) != 64:
        raise SystemExit("TLS fingerprint must be a SHA-256 hex digest")
    return cleaned.lower()


def pem_cert_fingerprint(cert_path: str | Path) -> str:
    pem = Path(cert_path).expanduser().read_text(encoding="utf-8")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def generate_self_signed_cert(cert_path: str | Path, key_path: str | Path, common_name: str, days: int) -> None:
    cert = Path(cert_path).expanduser()
    key = Path(key_path).expanduser()
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)
    subject = f"/CN={common_name or 'agentmessenger'}"
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        str(days),
        "-subj",
        subject,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise SystemExit("openssl is required for --secure certificate generation") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"openssl failed to generate a TLS certificate: {exc.stderr.strip()}") from exc
    try:
        os.chmod(key, 0o600)
        os.chmod(cert, 0o644)
    except OSError:
        pass


def ensure_tls_material(args: argparse.Namespace, config: dict[str, Any]) -> tuple[str, str, str]:
    cert_path = str(Path(args.tls_cert or str(config.get("tls_cert") or DEFAULT_TLS_CERT)).expanduser())
    key_path = str(Path(args.tls_key or str(config.get("tls_key") or DEFAULT_TLS_KEY)).expanduser())
    if not Path(cert_path).exists() or not Path(key_path).exists():
        public_url = args.public_url or args.url or ""
        parsed = urllib.parse.urlparse(public_url)
        common_name = parsed.hostname or socket.gethostname()
        generate_self_signed_cert(cert_path, key_path, common_name, args.tls_days)
    return cert_path, key_path, pem_cert_fingerprint(cert_path)


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(config)
    for key in ("api_key", "admin_token"):
        if key in redacted and redacted[key]:
            value = str(redacted[key])
            redacted[key] = f"{value[:8]}...{value[-4:]}" if len(value) > 16 else "***"
    return redacted


def encode_join_code(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{JOIN_CODE_PREFIX}{encoded}"


def decode_join_code(value: str) -> dict[str, Any]:
    value = value.strip()
    if not value:
        raise SystemExit("join code is required")
    if value.startswith("am_inv_"):
        return {"kind": "agentmessenger-join", "version": 1, "invite_code": value}
    if value.startswith(JOIN_CODE_PREFIX):
        encoded = value.removeprefix(JOIN_CODE_PREFIX)
        padding = "=" * (-len(encoded) % 4)
        try:
            decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
            payload = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise SystemExit("Invalid AgentMessenger join code") from exc
        if not isinstance(payload, dict):
            raise SystemExit("Invalid AgentMessenger join code")
        if payload.get("kind") != "agentmessenger-join":
            raise SystemExit("Join code is not for AgentMessenger")
        return payload
    raise SystemExit("Expected an am_join_ setup code or an am_inv_ invite code")


def configured_agent(args: argparse.Namespace, attr: str = "agent") -> str:
    value = getattr(args, attr, None)
    if value:
        return clean_agent_name(value)
    env_value = os.environ.get("AGENTMESSENGER_AGENT")
    if env_value:
        return clean_agent_name(env_value)
    config = load_config(getattr(args, "config", None))
    if config.get("agent"):
        return clean_agent_name(str(config["agent"]))
    return default_agent_name()


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


def json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def same_secret(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


def generate_invite_code() -> str:
    return f"am_inv_{secrets.token_urlsafe(24)}"


def generate_api_key() -> str:
    return f"am_key_{secrets.token_urlsafe(32)}"


def ttl_expires_at(ttl_seconds: int | None) -> float:
    ttl = DEFAULT_TTL if ttl_seconds is None else int(ttl_seconds)
    return now() + ttl


class BrokerState:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = resolve_db_path(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    name TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY,
                    id TEXT UNIQUE NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    in_reply_to TEXT,
                    thread_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_consumed (
                    message_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    consumed_at REAL NOT NULL,
                    PRIMARY KEY (message_id, agent),
                    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT UNIQUE NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS identities (
                    agent TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    api_key_hash TEXT UNIQUE NOT NULL,
                    invite_id INTEGER,
                    created_at REAL NOT NULL,
                    last_seen_at REAL,
                    FOREIGN KEY (invite_id) REFERENCES invites(id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_recipient_seq
                    ON messages(recipient, seq);
                CREATE INDEX IF NOT EXISTS idx_messages_reply
                    ON messages(in_reply_to);
                CREATE INDEX IF NOT EXISTS idx_identities_api_key
                    ON identities(api_key_hash);
                """
            )

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def cleanup(self) -> None:
        current = now()
        self.conn.execute("DELETE FROM agents WHERE expires_at <= ?", (current,))
        self.conn.execute(
            "DELETE FROM message_consumed WHERE message_id IN "
            "(SELECT id FROM messages WHERE expires_at <= ?)",
            (current,),
        )
        self.conn.execute("DELETE FROM messages WHERE expires_at <= ?", (current,))

    def has_identities(self) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM identities LIMIT 1").fetchone()
            return row is not None

    def create_invite(self, label: str, max_uses: int, ttl_seconds: int) -> dict[str, Any]:
        if max_uses < 1:
            raise BrokerError("max_uses must be at least 1")
        code = generate_invite_code()
        timestamp = now()
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO invites
                    (code_hash, label, max_uses, uses, created_at, expires_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (hash_secret(code), label, max_uses, timestamp, timestamp + ttl_seconds),
            )
            row = self.conn.execute("SELECT * FROM invites WHERE id = ?", (cursor.lastrowid,)).fetchone()
            invite = invite_from_row(row)
            invite["code"] = code
            return invite

    def list_invites(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM invites ORDER BY id DESC").fetchall()
            return [invite_from_row(row) for row in rows]

    def register_identity(self, agent_name: str, invite_code: str, display_name: str) -> dict[str, Any]:
        agent_name = clean_agent_name(agent_name)
        timestamp = now()
        api_key = generate_api_key()
        with self.lock:
            invite = self.conn.execute(
                "SELECT * FROM invites WHERE code_hash = ?",
                (hash_secret(invite_code),),
            ).fetchone()
            if invite is None:
                raise BrokerError("invalid invite code", HTTPStatus.FORBIDDEN)
            invite_data = invite_from_row(invite)
            if invite_data["revoked_at"] is not None:
                raise BrokerError("invite has been revoked", HTTPStatus.FORBIDDEN)
            if invite_data["expires_at"] <= timestamp:
                raise BrokerError("invite has expired", HTTPStatus.FORBIDDEN)
            if invite_data["uses"] >= invite_data["max_uses"]:
                raise BrokerError("invite has no remaining uses", HTTPStatus.FORBIDDEN)
            existing = self.conn.execute("SELECT agent FROM identities WHERE agent = ?", (agent_name,)).fetchone()
            if existing is not None:
                raise BrokerError("agent identity already exists", HTTPStatus.CONFLICT)
            self.conn.execute(
                """
                INSERT INTO identities
                    (agent, display_name, api_key_hash, invite_id, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    display_name,
                    hash_secret(api_key),
                    invite_data["id"],
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE id = ?",
                (invite_data["id"],),
            )
            identity = self.lookup_identity(api_key)
            if identity is None:
                raise BrokerError("failed to create identity")
            identity["api_key"] = api_key
            return identity

    def lookup_identity(self, api_key: str | None) -> dict[str, Any] | None:
        if not api_key:
            return None
        key_hash = hash_secret(api_key)
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM identities WHERE api_key_hash = ?",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            timestamp = now()
            self.conn.execute(
                "UPDATE identities SET last_seen_at = ? WHERE agent = ?",
                (timestamp, row["agent"]),
            )
            return identity_from_row(row, last_seen_at=timestamp)

    def list_identities(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM identities ORDER BY agent").fetchall()
            return [identity_from_row(row) for row in rows]

    def announce(self, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.cleanup()
            ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL)
            timestamp = now()
            metadata = payload.get("metadata") or {}
            self.conn.execute(
                """
                INSERT INTO agents
                    (name, summary, workspace, context, metadata_json, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    summary = excluded.summary,
                    workspace = excluded.workspace,
                    context = excluded.context,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    agent_name,
                    str(payload.get("summary") or ""),
                    str(payload.get("workspace") or ""),
                    str(payload.get("context") or ""),
                    json.dumps(metadata, sort_keys=True),
                    timestamp,
                    timestamp + ttl,
                ),
            )
            agent = self.fetch_agent(agent_name)
            self.condition.notify_all()
            return agent or {}

    def list_agents(self) -> list[dict[str, Any]]:
        with self.lock:
            self.cleanup()
            rows = self.conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
            return [agent_from_row(row) for row in rows]

    def fetch_agent(self, agent_name: str) -> dict[str, Any] | None:
        with self.lock:
            self.cleanup()
            row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (agent_name,)).fetchone()
            return agent_from_row(row) if row else None

    def next_message_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM messages").fetchone()
        return int(row["seq"])

    def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.cleanup()
            ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL)
            seq = self.next_message_seq()
            message_id = f"m{seq:06d}"
            thread_id = str(payload.get("thread_id") or payload.get("in_reply_to") or message_id)
            timestamp = now()
            self.conn.execute(
                """
                INSERT INTO messages
                    (seq, id, sender, recipient, kind, text, context, in_reply_to,
                     thread_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    message_id,
                    clean_agent_name(str(payload.get("sender") or "unknown")),
                    clean_recipient(str(payload.get("recipient") or "*")),
                    str(payload.get("kind") or "note"),
                    str(payload.get("text") or ""),
                    str(payload.get("context") or ""),
                    payload.get("in_reply_to"),
                    thread_id,
                    timestamp,
                    timestamp + ttl,
                ),
            )
            message = self.fetch_message(message_id)
            self.condition.notify_all()
            return message or {}

    def fetch_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return message_from_row(row) if row else None

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
                messages = self.query_messages(
                    agent_name,
                    since=since,
                    include_consumed=include_consumed,
                    in_reply_to=in_reply_to,
                )
                if messages or now() >= deadline:
                    if consume and messages:
                        timestamp = now()
                        self.conn.executemany(
                            """
                            INSERT OR IGNORE INTO message_consumed
                                (message_id, agent, consumed_at)
                            VALUES (?, ?, ?)
                            """,
                            [(message["id"], agent_name, timestamp) for message in messages],
                        )
                    return messages
                self.condition.wait(timeout=max(0.1, deadline - now()))

    def query_messages(
        self,
        agent_name: str,
        since: int,
        include_consumed: bool,
        in_reply_to: str | None,
    ) -> list[dict[str, Any]]:
        conditions = [
            "messages.seq > ?",
            "(messages.recipient = ? OR messages.recipient = '*')",
        ]
        params: list[Any] = [since, agent_name]
        if not include_consumed:
            conditions.append(
                "NOT EXISTS ("
                "SELECT 1 FROM message_consumed "
                "WHERE message_consumed.message_id = messages.id "
                "AND message_consumed.agent = ?)"
            )
            params.append(agent_name)
        if in_reply_to:
            conditions.append("messages.in_reply_to = ?")
            params.append(in_reply_to)
        sql = f"SELECT * FROM messages WHERE {' AND '.join(conditions)} ORDER BY seq"
        rows = self.conn.execute(sql, params).fetchall()
        return [message_from_row(row) for row in rows]


def agent_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "summary": row["summary"],
        "workspace": row["workspace"],
        "context": row["context"],
        "metadata": json_object(row["metadata_json"]),
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "kind": row["kind"],
        "text": row["text"],
        "context": row["context"],
        "in_reply_to": row["in_reply_to"],
        "thread_id": row["thread_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def invite_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "max_uses": row["max_uses"],
        "uses": row["uses"],
        "remaining_uses": max(0, row["max_uses"] - row["uses"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
    }


def identity_from_row(row: sqlite3.Row, last_seen_at: float | None = None) -> dict[str, Any]:
    return {
        "agent": row["agent"],
        "display_name": row["display_name"],
        "invite_id": row["invite_id"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"] if last_seen_at is None else last_seen_at,
    }


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = f"AgentMessenger/{VERSION}"

    @property
    def broker(self) -> BrokerState:
        return self.server.broker  # type: ignore[attr-defined]

    @property
    def admin_token(self) -> str | None:
        return self.server.admin_token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if not getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/health":
                credential = self.credential()
                payload = {
                    "ok": True,
                    "service": "agentmessenger",
                    "version": VERSION,
                    "storage": "sqlite",
                    "auth_required": self.admin_token is not None or self.broker.has_identities(),
                    "time": now(),
                }
                if credential is not None:
                    payload["db"] = self.broker.db_path
                    payload["credential"] = credential_public(credential)
                self.write_json(payload)
            elif path == "/whoami":
                credential = self.require_credential()
                if credential is not None:
                    self.write_json({"credential": credential_public(credential)})
            elif path == "/invites":
                if self.require_admin() is not None:
                    self.write_json({"invites": self.broker.list_invites()})
            else:
                credential = self.require_credential()
                if credential is None:
                    return
                if path == "/agents":
                    self.write_json({"agents": self.broker.list_agents()})
                elif path.startswith("/agents/") and path.endswith("/context"):
                    agent_name = urllib.parse.unquote(path.split("/")[2])
                    agent = self.broker.fetch_agent(agent_name)
                    if agent is None:
                        self.write_json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                    else:
                        self.write_json({"agent": agent})
                elif path == "/messages":
                    agent_name = clean_agent_name(str(one(query, "agent", default_agent_name())))
                    if not self.can_act_as(credential, agent_name):
                        self.write_json({"error": "credential cannot read this inbox"}, HTTPStatus.FORBIDDEN)
                        return
                    wait_seconds = float(str(one(query, "wait", "0")))
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
        except BrokerError as exc:
            self.write_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # pragma: no cover - surfaced to CLI.
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        self.handle_write()

    def do_PUT(self) -> None:
        self.handle_write()

    def handle_write(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self.read_json()
            if path == "/register":
                agent_name = clean_agent_name(str(body.get("agent") or ""))
                invite_code = str(body.get("invite_code") or "")
                display_name = str(body.get("display_name") or agent_name)
                if not agent_name:
                    raise BrokerError("agent is required")
                if not invite_code:
                    raise BrokerError("invite_code is required")
                identity = self.broker.register_identity(agent_name, invite_code, display_name)
                self.write_json({"identity": identity})
                return
            if path == "/invites":
                if self.require_admin() is None:
                    return
                label = str(body.get("label") or "")
                max_uses = int(body.get("max_uses") or 1)
                ttl_seconds = int(body.get("ttl_seconds") or DEFAULT_INVITE_TTL)
                invite = self.broker.create_invite(label, max_uses, ttl_seconds)
                self.write_json({"invite": invite})
                return

            credential = self.require_credential()
            if credential is None:
                return
            if path.startswith("/agents/"):
                agent_name = clean_agent_name(urllib.parse.unquote(path.split("/")[2]))
                if not self.can_act_as(credential, agent_name):
                    self.write_json({"error": "credential cannot announce as this agent"}, HTTPStatus.FORBIDDEN)
                    return
                agent = self.broker.announce(agent_name, body)
                self.write_json({"agent": agent})
            elif path == "/messages":
                sender = clean_agent_name(str(body.get("sender") or ""))
                if not self.can_act_as(credential, sender):
                    self.write_json({"error": "credential cannot send as this agent"}, HTTPStatus.FORBIDDEN)
                    return
                message = self.broker.send_message(body)
                self.write_json({"message": message})
            else:
                self.write_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BrokerError as exc:
            self.write_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # pragma: no cover - surfaced to CLI.
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def credential(self) -> dict[str, Any] | None:
        token = self.token_header()
        if same_secret(token, self.admin_token):
            return {"kind": "admin"}
        api_key = self.headers.get("X-AgentMessenger-Api-Key") or token
        identity = self.broker.lookup_identity(api_key)
        if identity is not None:
            return {"kind": "identity", **identity}
        if self.admin_token is None and not self.broker.has_identities():
            return {"kind": "public"}
        return None

    def require_credential(self) -> dict[str, Any] | None:
        credential = self.credential()
        if credential is None:
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return credential

    def require_admin(self) -> dict[str, Any] | None:
        credential = self.credential()
        if credential is None:
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return None
        if credential["kind"] != "admin":
            self.write_json({"error": "admin token required"}, HTTPStatus.FORBIDDEN)
            return None
        return credential

    def can_act_as(self, credential: dict[str, Any], agent_name: str) -> bool:
        if credential["kind"] in {"admin", "public"}:
            return True
        return credential["kind"] == "identity" and credential["agent"] == agent_name

    def token_header(self) -> str | None:
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            return bearer.removeprefix("Bearer ").strip()
        return self.headers.get("X-AgentMessenger-Token")

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


def credential_public(credential: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in credential.items() if key != "api_key"}


def one(query: dict[str, list[str]], key: str, default: str | None = "") -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def run_server(args: argparse.Namespace) -> int:
    db_path = args.db or args.state or default_db_path()
    broker = BrokerState(db_path)
    server = ThreadingHTTPServer((args.host, args.port), BrokerHandler)
    server.broker = broker  # type: ignore[attr-defined]
    server.admin_token = (
        args.admin_token
        or args.token
        or os.environ.get("AGENTMESSENGER_ADMIN_TOKEN")
        or os.environ.get("AGENTMESSENGER_TOKEN")
    )  # type: ignore[attr-defined]
    server.quiet = args.quiet  # type: ignore[attr-defined]

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    host, port = server.server_address[:2]
    display_host = args.host if args.host != "0.0.0.0" else host
    scheme = "https" if args.tls_cert or args.tls_key else "http"
    if args.tls_cert or args.tls_key:
        if not args.tls_cert or not args.tls_key:
            raise SystemExit("--tls-cert and --tls-key must be passed together")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(Path(args.tls_cert).expanduser()), str(Path(args.tls_key).expanduser()))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    url = f"{scheme}://{display_host}:{port}"
    print(f"AgentMessenger broker listening at {url}", flush=True)
    print(f"SQLite database: {broker.db_path}", flush=True)
    if scheme == "https":
        print(f"TLS fingerprint: {pem_cert_fingerprint(args.tls_cert)}", flush=True)
    if server.admin_token:  # type: ignore[attr-defined]
        print("Admin token is enabled. Use it to create invites; agents should register API keys.", flush=True)
    elif args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Warning: no admin token configured for a non-localhost bind.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAgentMessenger broker stopped.", flush=True)
    finally:
        server.server_close()
        broker.close()
    return 0


class Client:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        api_key: str | None = None,
        tls_fingerprint: str | None = None,
        timeout: float = MAX_WAIT_SECONDS + 10,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.api_key = api_key
        self.tls_fingerprint = normalize_fingerprint(tls_fingerprint)
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-AgentMessenger-Token"] = self.token
        if self.api_key:
            headers["X-AgentMessenger-Api-Key"] = self.api_key
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        scheme = urllib.parse.urlparse(self.url).scheme
        try:
            context = None
            if scheme == "https" and self.tls_fingerprint:
                context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                if scheme == "https" and self.tls_fingerprint:
                    self.verify_tls_fingerprint(response)
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise SystemExit(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Could not reach AgentMessenger broker at {self.url}: {exc.reason}") from exc

    def verify_tls_fingerprint(self, response: Any) -> None:
        sock = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(sock, "_sock", None)
        if sock is None:
            raise SystemExit("Could not inspect TLS certificate for fingerprint pinning")
        actual = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self.tls_fingerprint or ""):
            raise SystemExit(
                "TLS fingerprint mismatch. The broker certificate does not match the setup code."
            )


def make_client(
    args: argparse.Namespace,
    timeout: float = MAX_WAIT_SECONDS + 10,
    use_admin: bool = False,
) -> Client:
    config = load_config(getattr(args, "config", None))
    explicit_token = getattr(args, "token", None) or os.environ.get("AGENTMESSENGER_TOKEN")
    explicit_admin = getattr(args, "admin_token", None) or os.environ.get("AGENTMESSENGER_ADMIN_TOKEN")
    token = explicit_token or explicit_admin
    if use_admin and not token:
        token = str(config.get("admin_token") or "") or None
    api_key = getattr(args, "api_key", None) or os.environ.get("AGENTMESSENGER_API_KEY")
    if not api_key:
        api_key = str(config.get("api_key") or "") or None
    url = getattr(args, "url", None) or os.environ.get("AGENTMESSENGER_URL") or str(config.get("url") or DEFAULT_URL)
    tls_fingerprint = (
        getattr(args, "tls_fingerprint", None)
        or os.environ.get("AGENTMESSENGER_TLS_FINGERPRINT")
        or str(config.get("tls_fingerprint") or "")
        or None
    )
    if urllib.parse.urlparse(url).scheme != "https":
        tls_fingerprint = None
    return Client(
        url,
        token=token,
        api_key=api_key,
        tls_fingerprint=tls_fingerprint,
        timeout=timeout,
    )


def cmd_status(args: argparse.Namespace) -> int:
    result = make_client(args).request("GET", "/health")
    emit(args, result, "AgentMessenger broker is reachable.")
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    payload = {
        "label": args.label or "",
        "max_uses": args.max_uses,
        "ttl_seconds": args.ttl,
    }
    result = make_client(args, use_admin=True).request("POST", "/invites", payload)
    if args.json:
        emit(args, result)
    else:
        invite = result["invite"]
        print(f"Created invite {invite['id']} ({invite['remaining_uses']} use(s) remaining).")
        if invite.get("label"):
            print(f"label: {invite['label']}")
        print(f"invite code: {invite['code']}")
    return 0


def cmd_invites(args: argparse.Namespace) -> int:
    result = make_client(args, use_admin=True).request("GET", "/invites")
    if args.json:
        emit(args, result)
    else:
        invites = result.get("invites", [])
        if not invites:
            print("No invites.")
            return 0
        for invite in invites:
            expires = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(invite["expires_at"]))
            status = "revoked" if invite["revoked_at"] else "active"
            print(
                f"{invite['id']} {status} uses={invite['uses']}/{invite['max_uses']} "
                f"expires={expires} label={invite['label']}"
            )
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    agent = configured_agent(args)
    payload = {
        "agent": agent,
        "invite_code": args.invite_code,
        "display_name": args.display_name or agent,
    }
    result = make_client(args).request("POST", "/register", payload)
    if args.json:
        emit(args, result)
    else:
        identity = result["identity"]
        print(f"Registered {identity['agent']}.")
        print("API key (shown once):")
        print(identity["api_key"])
        print("\nSet these in this agent session:")
        print(f"export AGENTMESSENGER_AGENT={identity['agent']}")
        print(f"export AGENTMESSENGER_API_KEY={identity['api_key']}")
    return 0


def wait_for_broker(
    url: str,
    admin_token: str | None = None,
    timeout: float = 10,
    tls_fingerprint: str | None = None,
) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            Client(url, token=admin_token, tls_fingerprint=tls_fingerprint, timeout=1).request("GET", "/health")
            return
        except SystemExit as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        raise SystemExit(str(last_error))
    raise SystemExit(f"Could not reach AgentMessenger broker at {url}")


def broker_is_reachable(url: str, admin_token: str | None = None, tls_fingerprint: str | None = None) -> bool:
    try:
        Client(url, token=admin_token, tls_fingerprint=tls_fingerprint, timeout=1).request("GET", "/health")
        return True
    except SystemExit:
        return False


def start_background_server(
    host: str,
    port: int,
    db_path: str,
    admin_token: str,
    log_path: str,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    quiet: bool = True,
) -> int:
    resolved_log = Path(log_path).expanduser()
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "server",
        "--host",
        host,
        "--port",
        str(port),
        "--db",
        db_path,
        "--admin-token",
        admin_token,
    ]
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            raise SystemExit("tls_cert and tls_key must be passed together")
        command.extend(["--tls-cert", tls_cert, "--tls-key", tls_key])
    if quiet:
        command.append("--quiet")
    log_file = resolved_log.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        command,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    return int(proc.pid)


def register_with_invite(client: Client, agent: str, invite_code: str, display_name: str | None = None) -> dict[str, Any]:
    payload = {
        "agent": agent,
        "invite_code": invite_code,
        "display_name": display_name or agent,
    }
    return client.request("POST", "/register", payload)["identity"]


def create_invite(client: Client, label: str, max_uses: int, ttl: int) -> dict[str, Any]:
    payload = {"label": label, "max_uses": max_uses, "ttl_seconds": ttl}
    return client.request("POST", "/invites", payload)["invite"]


def cmd_host(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    secure = args.secure or str(args.public_url or args.url or config.get("url") or "").startswith("https://")
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_fingerprint = normalize_fingerprint(args.tls_fingerprint or str(config.get("tls_fingerprint") or ""))
    if secure:
        tls_cert, tls_key, tls_fingerprint = ensure_tls_material(args, config)
    scheme = "https" if secure else "http"
    explicit_url = args.url or os.environ.get("AGENTMESSENGER_URL")
    saved_url = str(config.get("url") or "")
    if explicit_url:
        local_url = explicit_url
    elif saved_url and (not secure or saved_url.startswith("https://")):
        local_url = saved_url
    else:
        local_url = f"{scheme}://127.0.0.1:{args.port}"
    public_url = args.public_url or local_url
    admin_token = (
        args.admin_token
        or os.environ.get("AGENTMESSENGER_ADMIN_TOKEN")
        or str(config.get("admin_token") or "")
        or secrets.token_urlsafe(24)
    )
    db_path = resolve_db_path(args.db or str(config.get("db") or DEFAULT_DB))
    log_path = str(Path(args.log or str(config.get("server_log") or DEFAULT_LOG)).expanduser())
    server_pid: int | None = None

    if args.no_start:
        wait_for_broker(local_url, admin_token=admin_token, timeout=args.timeout, tls_fingerprint=tls_fingerprint)
    elif broker_is_reachable(local_url, admin_token=admin_token, tls_fingerprint=tls_fingerprint):
        server_pid = int(config["server_pid"]) if str(config.get("server_pid") or "").isdigit() else None
    else:
        server_pid = start_background_server(
            args.host,
            args.port,
            db_path,
            admin_token,
            log_path,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
        wait_for_broker(local_url, admin_token=admin_token, timeout=args.timeout, tls_fingerprint=tls_fingerprint)

    admin_client = Client(local_url, token=admin_token, tls_fingerprint=tls_fingerprint)
    agent = clean_agent_name(args.agent or str(config.get("agent") or "") or default_agent_name())
    api_key = str(config.get("api_key") or "") if config.get("agent") == agent else ""
    try:
        if not api_key:
            self_invite = create_invite(admin_client, f"host:{agent}", 1, args.ttl)
            identity = register_with_invite(
                Client(local_url, tls_fingerprint=tls_fingerprint),
                agent,
                self_invite["code"],
                args.display_name or agent,
            )
            api_key = identity["api_key"]

        invite_label = args.label or f"join:{agent}"
        invite = create_invite(admin_client, invite_label, args.max_uses, args.ttl)
    except SystemExit as exc:
        raise SystemExit(
            "Could not create an invite. If this broker was already running, rerun with "
            "--admin-token or use the saved host config that contains the admin token. "
            f"Original error: {exc}"
        ) from exc
    join_payload = {
        "kind": "agentmessenger-join",
        "version": 1,
        "url": public_url,
        "invite_code": invite["code"],
        "label": invite_label,
    }
    if args.note:
        join_payload["note"] = args.note
    if tls_fingerprint:
        join_payload["tls_fingerprint"] = tls_fingerprint
    join_code = encode_join_code(join_payload)

    saved = {
        **config,
        "url": local_url,
        "agent": agent,
        "api_key": api_key,
        "admin_token": admin_token,
        "db": db_path,
        "server_log": log_path,
        "updated_at": now(),
    }
    if tls_fingerprint:
        saved["tls_fingerprint"] = tls_fingerprint
    if tls_cert and tls_key:
        saved["tls_cert"] = tls_cert
        saved["tls_key"] = tls_key
    if server_pid:
        saved["server_pid"] = server_pid
    save_config(saved, config_path)

    result = {
        "agent": agent,
        "config_path": str(config_path),
        "url": local_url,
        "public_url": public_url,
        "join_code": join_code,
        "invite": invite,
        "server_pid": server_pid,
        "server_log": log_path,
        "tls_fingerprint": tls_fingerprint,
    }
    if args.json:
        emit(args, result)
    else:
        print(f"AgentMessenger host is ready for {agent}.")
        print(f"config: {config_path}")
        print(f"broker url: {local_url}")
        if tls_fingerprint:
            print(f"tls fingerprint: {tls_fingerprint}")
        elif urllib.parse.urlparse(public_url).scheme == "http" and args.host not in {"127.0.0.1", "localhost", "::1"}:
            print("warning: public HTTP is not encrypted; prefer --secure for real conversations")
        if server_pid:
            print(f"server pid: {server_pid}")
        print("\nSend this setup code to your friend:")
        print(join_code)
        print("\nYour friend can run:")
        print(f"python3 {Path(__file__).resolve()} join {join_code}")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    payload = decode_join_code(args.code)
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    previous_url = str(config.get("url") or "")
    url = args.url or os.environ.get("AGENTMESSENGER_URL") or str(payload.get("url") or config.get("url") or "")
    if not url:
        raise SystemExit("Join code does not include a broker URL; pass --url http://host:port")
    invite_code = str(payload.get("invite_code") or "")
    if not invite_code:
        raise SystemExit("Join code does not include an invite code")
    tls_fingerprint = normalize_fingerprint(
        args.tls_fingerprint
        or str(payload.get("tls_fingerprint") or "")
        or str(config.get("tls_fingerprint") or "")
    )
    agent = configured_agent(args)
    identity = register_with_invite(
        Client(url, tls_fingerprint=tls_fingerprint),
        agent,
        invite_code,
        args.display_name or agent,
    )
    saved = {
        **config,
        "url": url,
        "agent": identity["agent"],
        "api_key": identity["api_key"],
        "joined_at": now(),
        "setup_label": payload.get("label", ""),
    }
    if tls_fingerprint:
        saved["tls_fingerprint"] = tls_fingerprint
    if previous_url and previous_url != url:
        for key in ("admin_token", "db", "server_log", "server_pid"):
            saved.pop(key, None)
    save_config(saved, config_path)
    result = {
        "agent": identity["agent"],
        "config_path": str(config_path),
        "url": url,
        "credential": "saved",
        "tls_fingerprint": tls_fingerprint,
    }
    if args.json:
        emit(args, result)
    else:
        print(f"Joined AgentMessenger as {identity['agent']}.")
        print(f"config: {config_path}")
        print("Saved broker URL and API key. Future commands can use the saved config automatically.")
        if tls_fingerprint:
            print("Pinned the broker TLS fingerprint from the setup code.")
        print("\nTry:")
        print(f"python3 {Path(__file__).resolve()} whoami")
        print(f"python3 {Path(__file__).resolve()} agents")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    payload = {"config_path": str(config_path), "config": redact_config(config)}
    if args.json:
        emit(args, payload)
    else:
        print(f"config: {config_path}")
        if not config:
            print("No saved AgentMessenger config yet.")
            return 0
        print(json.dumps(payload["config"], indent=2, sort_keys=True))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    result = make_client(args).request("GET", "/whoami")
    if args.json:
        emit(args, result)
    else:
        credential = result["credential"]
        if credential["kind"] == "identity":
            print(f"Authenticated as identity {credential['agent']}.")
        else:
            print(f"Authenticated as {credential['kind']}.")
    return 0


def cmd_announce(args: argparse.Namespace) -> int:
    agent = configured_agent(args)
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
    if agent_payload.get("metadata"):
        print(f"metadata: {json.dumps(agent_payload['metadata'], sort_keys=True)}")
    if agent_payload.get("context"):
        print("\ncontext:")
        print(agent_payload["context"])
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    sender = configured_agent(args, "from_agent")
    recipient = clean_recipient(args.to)
    payload = {
        "sender": sender,
        "recipient": recipient,
        "kind": "context_request",
        "text": args.question,
        "context": read_context(args.context, args.context_file),
        "ttl_seconds": args.ttl,
    }
    client = make_client(args, timeout=args.timeout + 10)
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
    agent = configured_agent(args)
    wait = args.timeout if args.wait else 0
    consume = "0" if args.no_consume or args.peek else "1"
    include = "1" if args.include_consumed else "0"
    path = (
        f"/messages?agent={urllib.parse.quote(agent)}"
        f"&wait={int(wait)}&consume={consume}"
        f"&include_consumed={include}&since={urllib.parse.quote(args.since or '0')}"
    )
    result = make_client(args, timeout=wait + 10).request("GET", path)
    if args.json:
        emit(args, result)
    else:
        print_messages(result.get("messages", []))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    sender = configured_agent(args, "from_agent")
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
    sender = configured_agent(args, "from_agent")
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
    parser.add_argument("--url")
    parser.add_argument("--token", help="legacy/admin token")
    parser.add_argument("--admin-token")
    parser.add_argument("--api-key")
    parser.add_argument("--config", help=f"config path; default {DEFAULT_CONFIG}")
    parser.add_argument("--tls-fingerprint", help="expected broker TLS certificate SHA-256 fingerprint")
    parser.add_argument("--json", action="store_true", help="emit JSON")


def add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context", help="inline context to include")
    parser.add_argument("--context-file", help="file containing context to include")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentMessenger broker and CLI")
    parser.add_argument("--version", action="version", version=f"agentmessenger {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="start broker")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--token", default=os.environ.get("AGENTMESSENGER_TOKEN"), help="deprecated alias for --admin-token")
    server.add_argument("--admin-token", default=os.environ.get("AGENTMESSENGER_ADMIN_TOKEN"))
    server.add_argument("--db", default=os.environ.get("AGENTMESSENGER_DB"), help=f"SQLite DB path; default {DEFAULT_DB}")
    server.add_argument("--state", help="deprecated alias for --db")
    server.add_argument("--tls-cert", help="TLS certificate path for HTTPS")
    server.add_argument("--tls-key", help="TLS private key path for HTTPS")
    server.add_argument("--quiet", action="store_true")
    server.set_defaults(func=run_server)

    status = subparsers.add_parser("status", help="check broker health")
    add_client_options(status)
    status.set_defaults(func=cmd_status)

    host = subparsers.add_parser("host", help="start or connect to a broker and print a one-use setup code")
    add_client_options(host)
    host.add_argument("--host", default="127.0.0.1", help="bind host when starting a broker")
    host.add_argument("--port", type=int, default=8765)
    host.add_argument("--public-url", help="URL embedded in the setup code; use for AWS or public hosts")
    host.add_argument("--db", help=f"SQLite DB path; default {DEFAULT_DB}")
    host.add_argument("--log", help=f"server log path; default {DEFAULT_LOG}")
    host.add_argument("--secure", action="store_true", help="serve HTTPS with a pinned self-signed certificate")
    host.add_argument("--tls-cert", help=f"TLS certificate path; default {DEFAULT_TLS_CERT}")
    host.add_argument("--tls-key", help=f"TLS private key path; default {DEFAULT_TLS_KEY}")
    host.add_argument("--tls-days", type=int, default=365)
    host.add_argument("--agent")
    host.add_argument("--display-name")
    host.add_argument("--label", default="", help="label for the friend invite")
    host.add_argument("--max-uses", type=int, default=1)
    host.add_argument("--ttl", type=int, default=DEFAULT_INVITE_TTL)
    host.add_argument("--timeout", type=int, default=10)
    host.add_argument("--note", help="optional note embedded in the setup code")
    host.add_argument("--no-start", action="store_true", help="use an already-running broker")
    host.set_defaults(func=cmd_host)

    join = subparsers.add_parser("join", help="redeem an am_join setup code and save local config")
    add_client_options(join)
    join.add_argument("code", help="am_join setup code or raw am_inv invite code")
    join.add_argument("--agent")
    join.add_argument("--display-name")
    join.set_defaults(func=cmd_join)

    config = subparsers.add_parser("config", help="show saved local config")
    config.add_argument("--config", help=f"config path; default {DEFAULT_CONFIG}")
    config.add_argument("--json", action="store_true", help="emit JSON")
    config.set_defaults(func=cmd_config)

    invite = subparsers.add_parser("invite", help="create an invite code with the admin token")
    add_client_options(invite)
    invite.add_argument("--label", default="")
    invite.add_argument("--max-uses", type=int, default=1)
    invite.add_argument("--ttl", type=int, default=DEFAULT_INVITE_TTL)
    invite.set_defaults(func=cmd_invite)

    invites = subparsers.add_parser("invites", help="list invites with the admin token")
    add_client_options(invites)
    invites.set_defaults(func=cmd_invites)

    register = subparsers.add_parser("register", help="exchange an invite code for an agent API key")
    add_client_options(register)
    register.add_argument("--agent")
    register.add_argument("--invite-code", required=True)
    register.add_argument("--display-name")
    register.set_defaults(func=cmd_register)

    whoami = subparsers.add_parser("whoami", help="show the current credential")
    add_client_options(whoami)
    whoami.set_defaults(func=cmd_whoami)

    announce = subparsers.add_parser("announce", help="announce this agent's context")
    add_client_options(announce)
    announce.add_argument("--agent")
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
    ask.add_argument("--from", dest="from_agent")
    ask.add_argument("--to", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--wait", action="store_true")
    ask.add_argument("--timeout", type=int, default=60)
    ask.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    add_context_options(ask)
    ask.set_defaults(func=cmd_ask)

    inbox = subparsers.add_parser("inbox", help="read this agent's inbox")
    add_client_options(inbox)
    inbox.add_argument("--agent")
    inbox.add_argument("--wait", action="store_true")
    inbox.add_argument("--timeout", type=int, default=60)
    inbox.add_argument("--since", default="0")
    inbox.add_argument("--include-consumed", action="store_true")
    inbox.add_argument("--no-consume", action="store_true")
    inbox.add_argument("--peek", action="store_true", help="show messages without marking them consumed")
    inbox.set_defaults(func=cmd_inbox)

    reply = subparsers.add_parser("reply", help="reply to a context request")
    add_client_options(reply)
    reply.add_argument("--from", dest="from_agent")
    reply.add_argument("--to", required=True)
    reply.add_argument("--request-id", required=True)
    reply.add_argument("--message", required=True)
    reply.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    add_context_options(reply)
    reply.set_defaults(func=cmd_reply)

    note = subparsers.add_parser("note", help="send a one-way note")
    add_client_options(note)
    note.add_argument("--from", dest="from_agent")
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
