#!/usr/bin/env python3
"""Run an end-to-end AgentMessenger broker test with two simulated agents."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "agentmessenger.py"


def run_cli(url: str, token: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args, "--url", url, "--token", token, "--json"],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def json_cli(url: str, token: str, *args: str) -> dict:
    result = run_cli(url, token, *args)
    return json.loads(result.stdout)


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


def start_server(db_path: Path, token: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [
            sys.executable,
            str(CLI),
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--db",
            str(db_path),
            "--token",
            token,
            "--quiet",
        ],
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


def main() -> int:
    token = secrets.token_urlsafe(18)
    with tempfile.TemporaryDirectory(prefix="agentmessenger-test-") as temp_dir:
        db_path = Path(temp_dir) / "broker.sqlite3"
        proc, url = start_server(db_path, token)
        try:
            health = json_cli(url, token, "status")
            assert health["ok"] is True
            assert health["storage"] == "sqlite"

            unauthorized = subprocess.run(
                [sys.executable, str(CLI), "status", "--url", url, "--token", "wrong"],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            assert unauthorized.returncode != 0
            assert "401" in unauthorized.stderr

            json_cli(
                url,
                token,
                "announce",
                "--agent",
                "alice",
                "--summary",
                "Alice has backend context.",
                "--context",
                "Alice context: cache key changed.",
                "--meta",
                "role=backend",
            )
            json_cli(
                url,
                token,
                "announce",
                "--agent",
                "bob",
                "--summary",
                "Bob has UI context.",
                "--context",
                "Bob context: Settings > Cache reproduces it.",
                "--meta",
                "role=frontend",
            )

            agents = json_cli(url, token, "agents")["agents"]
            assert [agent["name"] for agent in agents] == ["alice", "bob"]
            bob = json_cli(url, token, "fetch", "--agent", "bob")["agent"]
            assert bob["metadata"]["role"] == "frontend"
            assert "Settings" in bob["context"]

            request = json_cli(
                url,
                token,
                "ask",
                "--from",
                "alice",
                "--to",
                "bob",
                "--question",
                "What UI context do you have?",
            )["message"]
            assert request["id"] == "m000001"

            inbox = json_cli(url, token, "inbox", "--agent", "bob", "--peek")["messages"]
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
                    "--token",
                    token,
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
                token,
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
            )
            stdout, stderr = waiter.communicate(timeout=10)
            assert waiter.returncode == 0, stderr
            replies = json.loads(stdout)["messages"]
            assert len(replies) == 1
            assert replies[0]["in_reply_to"] == request["id"]
            assert "Settings cache" in replies[0]["text"]
        finally:
            stop_server(proc)

        proc, url = start_server(db_path, token)
        try:
            agents = json_cli(url, token, "agents")["agents"]
            assert {agent["name"] for agent in agents} == {"alice", "bob"}
        finally:
            stop_server(proc)

    print("AgentMessenger self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
