"""Bounded live validation. Starting, reviewing and approving are separate commands."""
import argparse
import json
import os
import re
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from patchtrail.config import ROOT, Settings, load_local_env

import httpx


def request(client, method, path, **kwargs):
    """Retry only reads. Unknown write outcomes keep their original request identity."""
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError("Private API response was not confirmed; inspect the existing run before retrying") from None
        if response.status_code in {500, 502, 503, 504} and attempt + 1 < attempts:
            time.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Private API returned HTTP {response.status_code}; inspect the run before retrying")
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("Private API returned invalid JSON; no write was automatically retried") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "approve"])
    parser.add_argument("target", help="Task ID for start; run ID otherwise")
    parser.add_argument("--base-url", default="http://127.0.0.1:18816")
    parser.add_argument("--digest")
    parser.add_argument("--version", type=int)
    parser.add_argument("--request-key")
    parser.add_argument("--wait", type=int, default=150)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        parser.error("Use the explicit private loopback preview URL")
    if not 1 <= args.wait <= 180:
        parser.error("Wait must be between1 and180 seconds")
    if args.request_key and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", args.request_key):
        parser.error("Request key must be8–128 letters, digits, underscores or hyphens")
    load_local_env()
    settings = Settings()
    if not settings.admin_token:
        parser.error("Operator token is not configured in local.env")
    evidence = ROOT / "artifacts" / "private" / "live"
    evidence.mkdir(parents=True, exist_ok=True)
    evidence.chmod(0o700)

    with httpx.Client(base_url=args.base_url, headers={"Authorization": "Bearer " + settings.admin_token},
                      timeout=15, trust_env=False, follow_redirects=False) as client:
        health = request(client, "GET", "/api/health")
        if not health["model_configured"] or not health["container_available"]:
            raise RuntimeError("Live model or container is not ready")
        if args.action == "start":
            key = args.request_key or "live-check-" + uuid.uuid4().hex
            pending = evidence / (key + ".request.json")
            pending.write_text(json.dumps({"task_id": args.target, "mode": "live", "request_key": key}))
            pending.chmod(0o600)
            row = request(client, "POST", "/api/runs", json={"task_id": args.target, "mode": "live"},
                          headers={"Idempotency-Key": key})
        else:
            row = request(client, "GET", "/api/runs/" + args.target)
            if args.action == "approve":
                if not args.digest or args.version is None:
                    parser.error("Review the diff, then provide its exact digest and version")
                row = request(client, "POST", "/api/runs/" + args.target + "/approve",
                              json={"proposal_digest": args.digest, "expected_version": args.version})
        print(json.dumps({"run_id": row["id"], "task_id": row["task_id"], "state": row["state"]}), flush=True)
        deadline, previous = time.monotonic() + args.wait, None
        while row["state"] in {"queued", "diagnosing", "verifying"} and time.monotonic() < deadline:
            if row["state"] != previous:
                print("state=" + row["state"], flush=True)
                previous = row["state"]
            time.sleep(0.5)
            row = request(client, "GET", "/api/runs/" + row["id"])
        report = request(client, "GET", "/api/runs/" + row["id"] + "/report")
        origin = next((event["payload"] for event in report["events"] if event["kind"] == "run_created"), {})
        report["validation_context"] = {"configured_model": row.get("configured_model") or health["model"],
            "run_origin_runtime_code_hash": origin.get("runtime_code_hash"),
            "controller_runtime_code_hash_at_capture": health.get("runtime_code_hash"),
            "container_image": "docker.io/library/python@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b",
            "approval_actor": "Codex code review during validation; not user acceptance",
            "scope": "one attempt on an authored, known regression task; not a blind benchmark"}
        output = evidence / (row["id"] + ".json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        output.chmod(0o600)
        print(json.dumps({"run_id": row["id"], "state": row["state"], "stop_reason": row["stop_reason"],
              "calls": row["checkpoint"].get("calls"), "steps": row["checkpoint"].get("steps"),
              "usage": row["checkpoint"].get("usage"), "observed_models": row.get("observed_models"),
              "proposal_digest": (row.get("proposal") or {}).get("digest"), "version": row["version"],
              "evidence_path": str(output)}, ensure_ascii=False), flush=True)
        if row.get("proposal"):
            print(row["proposal"]["diff"], flush=True)
        if row.get("result"):
            print(json.dumps(row["result"], ensure_ascii=False), flush=True)
        raise SystemExit(0 if row["state"] in {"awaiting_approval", "succeeded"} else 1)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
