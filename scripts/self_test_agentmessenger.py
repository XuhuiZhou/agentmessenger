#!/usr/bin/env python3
"""Run an end-to-end AgentMessenger broker test with two simulated agents."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "agentmessenger.py"


def run_cli(
    url: str,
    *args: str,
    admin_token: str | None = None,
    api_key: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLI), *args, "--url", url, "--json"]
    if admin_token:
        command.extend(["--admin-token", admin_token])
    if api_key:
        command.extend(["--api-key", api_key])
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def json_cli(
    url: str,
    *args: str,
    admin_token: str | None = None,
    api_key: str | None = None,
) -> dict:
    result = run_cli(url, *args, admin_token=admin_token, api_key=api_key)
    return json.loads(result.stdout)


def run_raw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def free_tcp_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_server(proc: subprocess.Popen[str]) -> str:
    deadline = time.time() + 10
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early with code {proc.returncode}")
            continue
        match = re.search(r"(http://[0-9.]+:\d+)", line)
        if match:
            return match.group(1)
    raise TimeoutError("server did not report its listening URL")


def start_server(db_path: Path, admin_token: str | None = None) -> tuple[subprocess.Popen[str], str]:
    command = [
        sys.executable,
        str(CLI),
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--db",
        str(db_path),
        "--quiet",
    ]
    if admin_token:
        command.extend(["--admin-token", admin_token])
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, wait_for_server(proc)


def stop_server(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def register_agent(url: str, admin_token: str, agent: str, invite_code: str, contact: str | None = None) -> str:
    command = [
        "register",
        "--agent",
        agent,
        "--display-name",
        agent,
        "--invite-code",
        invite_code,
    ]
    if contact:
        command.extend(["--contact", contact])
    identity = json_cli(
        url,
        *command,
    )["identity"]
    assert identity["agent"] == agent
    if contact:
        assert identity["contact"] == contact
    assert identity["api_key"].startswith("am_key_")
    whoami = json_cli(url, "whoami", api_key=identity["api_key"])["credential"]
    assert whoami["kind"] == "identity"
    assert whoami["agent"] == agent
    if contact:
        assert whoami["contact"] == contact
    invites = json_cli(url, "invites", admin_token=admin_token)["invites"]
    assert invites[0]["uses"] >= 1
    return identity["api_key"]


def main() -> int:
    admin_token = secrets.token_urlsafe(18)
    with tempfile.TemporaryDirectory(prefix="agentmessenger-test-") as temp_dir:
        open_db_path = Path(temp_dir) / "open.sqlite3"
        proc, url = start_server(open_db_path)
        try:
            health = json_cli(url, "status")
            assert health["ok"] is True
            assert health["auth_required"] is False
            json_cli(
                url,
                "announce",
                "--agent",
                "open-agent",
                "--summary",
                "Open local demo still works.",
            )
            agents = json_cli(url, "agents")["agents"]
            assert [agent["name"] for agent in agents] == ["open-agent"]
        finally:
            stop_server(proc)

        setup_db_path = Path(temp_dir) / "setup.sqlite3"
        host_config = Path(temp_dir) / "host-config.json"
        friend_config = Path(temp_dir) / "friend-config.json"
        proc, url = start_server(setup_db_path, admin_token)
        try:
            hosted = json_cli(
                url,
                "host",
                "--no-start",
                "--agent",
                "owner",
                "--config",
                str(host_config),
                admin_token=admin_token,
            )
            assert hosted["agent"] == "owner"
            assert hosted["join_code"].startswith("am_join_")
            assert host_config.exists()

            joined = json_cli(
                url,
                "join",
                hosted["join_code"],
                "--agent",
                "friend",
                "--config",
                str(friend_config),
            )
            assert joined["agent"] == "friend"
            assert friend_config.exists()
            saved_friend = json.loads(friend_config.read_text())
            assert saved_friend["agent"] == "friend"
            assert saved_friend["api_key"].startswith("am_key_")

            whoami = json_cli(url, "whoami", "--config", str(friend_config))["credential"]
            assert whoami["kind"] == "identity"
            assert whoami["agent"] == "friend"
            json_cli(
                url,
                "announce",
                "--config",
                str(friend_config),
                "--summary",
                "Joined through one setup code.",
            )
            agents = json_cli(url, "agents", "--config", str(host_config))["agents"]
            assert "friend" in {agent["name"] for agent in agents}
        finally:
            stop_server(proc)

        if shutil.which("openssl"):
            secure_port = free_tcp_port()
            secure_host_config = Path(temp_dir) / "secure-host-config.json"
            secure_friend_config = Path(temp_dir) / "secure-friend-config.json"
            secure_host = json.loads(
                run_raw(
                    "host",
                    "--secure",
                    "--port",
                    str(secure_port),
                    "--config",
                    str(secure_host_config),
                    "--db",
                    str(Path(temp_dir) / "secure.sqlite3"),
                    "--log",
                    str(Path(temp_dir) / "secure.log"),
                    "--tls-cert",
                    str(Path(temp_dir) / "secure.crt"),
                    "--tls-key",
                    str(Path(temp_dir) / "secure.key"),
                    "--agent",
                    "secure-owner",
                    "--json",
                ).stdout
            )
            secure_pid = int(secure_host["server_pid"])
            try:
                assert secure_host["url"].startswith("https://")
                assert len(secure_host["tls_fingerprint"]) == 64
                secure_join = json.loads(
                    run_raw(
                        "join",
                        secure_host["join_code"],
                        "--agent",
                        "secure-friend",
                        "--config",
                        str(secure_friend_config),
                        "--json",
                    ).stdout
                )
                assert secure_join["agent"] == "secure-friend"
                assert secure_join["tls_fingerprint"] == secure_host["tls_fingerprint"]
                secure_whoami = json.loads(run_raw("whoami", "--config", str(secure_friend_config), "--json").stdout)
                assert secure_whoami["credential"]["agent"] == "secure-friend"
            finally:
                stop_pid(secure_pid)

        db_path = Path(temp_dir) / "broker.sqlite3"
        proc, url = start_server(db_path, admin_token)
        try:
            health = json_cli(url, "status", admin_token=admin_token)
            assert health["ok"] is True
            assert health["storage"] == "sqlite"
            assert health["credential"]["kind"] == "admin"

            invite = json_cli(
                url,
                "invite",
                "--label",
                "self-test",
                "--max-uses",
                "2",
                admin_token=admin_token,
            )["invite"]
            assert invite["code"].startswith("am_inv_")
            assert invite["remaining_uses"] == 2

            alice_key = register_agent(url, admin_token, "alice", invite["code"])
            bob_key = register_agent(url, admin_token, "bob", invite["code"])

            exhausted = run_cli(
                url,
                "register",
                "--agent",
                "charlie",
                "--invite-code",
                invite["code"],
                check=False,
            )
            assert exhausted.returncode != 0
            assert "403" in exhausted.stderr

            unauthorized = run_cli(
                url,
                "agents",
                "--api-key",
                "wrong",
                check=False,
            )
            assert unauthorized.returncode != 0
            assert "401" in unauthorized.stderr

            json_cli(
                url,
                "announce",
                "--agent",
                "alice",
                "--summary",
                "Alice has backend context.",
                "--context",
                "Alice context: cache key changed.",
                "--meta",
                "role=backend",
                api_key=alice_key,
            )
            spoof = run_cli(
                url,
                "announce",
                "--agent",
                "bob",
                "--summary",
                "Alice should not be able to announce as Bob.",
                api_key=alice_key,
                check=False,
            )
            assert spoof.returncode != 0
            assert "403" in spoof.stderr

            json_cli(
                url,
                "announce",
                "--agent",
                "bob",
                "--summary",
                "Bob has UI context.",
                "--context",
                "Bob context: Settings > Cache reproduces it.",
                "--meta",
                "role=frontend",
                api_key=bob_key,
            )

            agents = json_cli(url, "agents", api_key=alice_key)["agents"]
            assert [agent["name"] for agent in agents] == ["alice", "bob"]
            bob = json_cli(url, "fetch", "--agent", "bob", api_key=alice_key)["agent"]
            assert bob["metadata"]["role"] == "frontend"
            assert "Settings" in bob["context"]

            request = json_cli(
                url,
                "ask",
                "--from",
                "alice",
                "--to",
                "bob",
                "--question",
                "What UI context do you have?",
                api_key=alice_key,
            )["message"]
            assert request["id"] == "m000001"

            bob_spoof_read = run_cli(
                url,
                "inbox",
                "--agent",
                "alice",
                "--peek",
                api_key=bob_key,
                check=False,
            )
            assert bob_spoof_read.returncode != 0
            assert "403" in bob_spoof_read.stderr

            inbox = json_cli(url, "inbox", "--agent", "bob", "--peek", api_key=bob_key)["messages"]
            assert len(inbox) == 1
            assert inbox[0]["kind"] == "context_request"

            waiter = subprocess.Popen(
                [
                    sys.executable,
                    str(CLI),
                    "inbox",
                    "--agent",
                    "alice",
                    "--wait",
                    "--timeout",
                    "5",
                    "--url",
                    url,
                    "--api-key",
                    alice_key,
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.25)
            json_cli(
                url,
                "reply",
                "--from",
                "bob",
                "--to",
                "alice",
                "--request-id",
                request["id"],
                "--message",
                "Use the Settings cache repro.",
                "--context",
                "Relevant file: src/settings/cache.ts",
                api_key=bob_key,
            )
            stdout, stderr = waiter.communicate(timeout=10)
            assert waiter.returncode == 0, stderr
            replies = json.loads(stdout)["messages"]
            assert len(replies) == 1
            assert replies[0]["in_reply_to"] == request["id"]
            assert "Settings cache" in replies[0]["text"]

            alice_contact_invite = json_cli(
                url,
                "invite",
                "--label",
                "alice-contact",
                "--for",
                "Alice",
                "--max-uses",
                "2",
                admin_token=admin_token,
            )["invite"]
            assert alice_contact_invite["contact"] == "Alice"
            alice_mac_key = register_agent(url, admin_token, "alice-mac", alice_contact_invite["code"], contact="Alice")
            alice_aws_key = register_agent(url, admin_token, "alice-aws", alice_contact_invite["code"], contact="Alice")

            contacts = json_cli(url, "contacts", api_key=bob_key)["contacts"]
            alice_contact = next(contact for contact in contacts if contact["contact"] == "Alice")
            assert {agent["agent"] for agent in alice_contact["agents"]} == {"alice-mac", "alice-aws"}

            contact_request = json_cli(
                url,
                "ask",
                "--from",
                "bob",
                "--to",
                "Alice",
                "--question",
                "What context do Alice's agents have?",
                api_key=bob_key,
            )["message"]
            assert contact_request["recipient"] == "Alice"
            assert contact_request["recipient_kind"] == "contact"

            mac_inbox = json_cli(url, "inbox", "--agent", "alice-mac", "--peek", api_key=alice_mac_key)["messages"]
            aws_inbox = json_cli(url, "inbox", "--agent", "alice-aws", "--peek", api_key=alice_aws_key)["messages"]
            assert contact_request["id"] in {message["id"] for message in mac_inbox}
            assert contact_request["id"] in {message["id"] for message in aws_inbox}

            json_cli(
                url,
                "reply",
                "--from",
                "alice-aws",
                "--to",
                "bob",
                "--request-id",
                contact_request["id"],
                "--message",
                "Alice's AWS agent can answer this one.",
                api_key=alice_aws_key,
            )
            bob_contact_reply = json_cli(
                url,
                "inbox",
                "--agent",
                "bob",
                "--include-consumed",
                "--peek",
                api_key=bob_key,
            )["messages"]
            assert any(
                message["in_reply_to"] == contact_request["id"]
                and message["sender"] == "alice-aws"
                and "AWS agent" in message["text"]
                for message in bob_contact_reply
            )
        finally:
            stop_server(proc)

        proc, url = start_server(db_path, admin_token)
        try:
            agents = json_cli(url, "agents", api_key=alice_key)["agents"]
            assert {agent["name"] for agent in agents} == {"alice", "bob"}
            whoami = json_cli(url, "whoami", api_key=bob_key)["credential"]
            assert whoami["agent"] == "bob"
        finally:
            stop_server(proc)

    print("AgentMessenger self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
