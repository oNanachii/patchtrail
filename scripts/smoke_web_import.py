"""Authenticated source-import HTTP smoke with authored text; never calls a model."""
import argparse
import json
from pathlib import Path
import socket
import ssl
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from patchtrail.config import Settings, load_local_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        parser.error("Use an explicit service origin without credentials or query")
    if not (url.scheme == "https" and url.hostname == "math.madokanun.top"
            or url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}):
        parser.error("Use the project HTTPS host or private loopback")
    load_local_env()
    token = Settings().admin_token
    if not token:
        parser.error("Configure the private operator token locally; never pass it in arguments")
    files = [{"path": "calculator.py", "content": "# Authored import-only smoke fixture.\n" * 160 + "def add(a, b):\n    return a - b\n"},
             {"path": "test_calculator.py", "content": "from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n"}]
    manifest = {"schema_version": 1, "project_name": "web-import-http-smoke",
        "title": "网页导入HTTP烟测（自编）", "description": "Authored import-only fixture. No model call or code execution is requested.",
        "files": [row["path"] for row in files], "editable_files": ["calculator.py"],
        "check_profile": "pytest", "test_paths": ["test_calculator.py"],
        "tags": ["web-import", "authored-smoke"], "provenance": "Project-authored HTTP integration text; no private user code"}
    body = {"manifest": manifest, "files": files}
    assert len(json.dumps(body).encode()) > 4096
    auth = {"Authorization": "Bearer " + token}
    with httpx.Client(base_url=args.base_url, trust_env=False, timeout=20, follow_redirects=False) as c:
        anonymous = c.post("/api/imports/preview", json=body)
        assert anonymous.status_code == 401
        wrong = c.post("/api/imports/preview", json=body, headers={"Authorization": "Bearer deliberately-invalid-test-token"})
        assert wrong.status_code == 401
        first = c.post("/api/imports/preview", json=body, headers=auth).raise_for_status().json()
        repeated = c.post("/api/imports/preview", json=body, headers=auth).raise_for_status().json()
        assert first["preview_id"] == repeated["preview_id"]
        assert first["execution_performed"] is False and first["model_called"] is False
        assert first["file_count"] == 2 and first["total_bytes"] > 4096
        assert all("content" not in row for row in first["files"])
        preview_id = first["preview_id"]
        assert c.get("/api/imports/" + preview_id).status_code == 401
        preview = c.get("/api/imports/" + preview_id, headers=auth).raise_for_status().json()
        assert preview["preview_id"] == preview_id
        confirmed = c.post("/api/imports/commit", json={"preview_id": preview_id}, headers=auth).raise_for_status().json()
        retry = c.post("/api/imports/commit", json={"preview_id": preview_id}, headers=auth).raise_for_status().json()
        assert confirmed["task"]["id"] == retry["task"]["id"] and retry["created"] is False
        assert confirmed["model_called"] is False and confirmed["execution_performed"] is False
        tid = confirmed["task"]["id"]
        assert tid not in c.get("/api/tasks").raise_for_status().text
        assert tid in c.get("/api/tasks", headers=auth).raise_for_status().text
        assert c.post("/api/runs", content=b"x" * 5000, headers={"Content-Type": "application/json"}).status_code == 413
    # No body is sent: authentication must reject from headers, without waiting
    # for a source upload. This is one connection, not a load test.
    port = url.port or (443 if url.scheme == "https" else 80)
    with socket.create_connection((url.hostname, port), timeout=5) as tcp:
        stream = ssl.create_default_context().wrap_socket(tcp, server_hostname=url.hostname) if url.scheme == "https" else tcp
        with stream:
            stream.sendall((f"POST /api/imports/preview HTTP/1.1\r\nHost: {url.hostname}:{port}\r\n"
                            "Content-Type: application/json\r\nContent-Length: 16384\r\nConnection: close\r\n\r\n").encode())
            received = b""
            while b"\r\n" not in received and len(received) < 4096:
                chunk = stream.recv(4096 - len(received))
                if not chunk:
                    break
                received += chunk
            status = received.split(b"\r\n", 1)[0]
            assert b" 401 " in status, status
    result = {"status": "passed", "scope": "actual HTTP; authored files over4KiB; no code/model execution",
              "origin": args.base_url, "files": 2, "source_bytes": first["total_bytes"],
              "preview_id": preview_id, "task_id": tid, "preview_retry_same_identity": True,
              "commit_retry_same_task": True, "anonymous_private_reads_denied": True,
              "auth_rejected_before_body_sent": True, "other_control_limit4096_preserved": True,
              "model_called": False, "execution_performed": False}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
