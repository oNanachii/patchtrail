"""Exercise the actual HTTP demo and container verifier; no model API calls."""
import argparse
import json
from pathlib import Path
import time
import uuid

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.base_url, timeout=30, follow_redirects=False, trust_env=False) as client:
        health = client.get("/api/health").raise_for_status().json()
        for task in client.get("/api/tasks").raise_for_status().json()["tasks"]:
            headers = {"Idempotency-Key": "http-smoke-" + uuid.uuid4().hex}
            row = client.post("/api/runs", json={"task_id": task["id"], "mode": "demo"}, headers=headers).raise_for_status().json()
            retry = client.post("/api/runs", json={"task_id": task["id"], "mode": "demo"}, headers=headers).raise_for_status().json()
            if retry["id"] != row["id"]:
                raise RuntimeError("HTTP create retry returned a different run")
            deadline = time.monotonic() + 60
            approved = False
            while time.monotonic() < deadline:
                row = client.get("/api/runs/" + row["id"]).raise_for_status().json()
                if row["state"] == "awaiting_approval" and not approved:
                    body = {"proposal_digest": row["proposal"]["digest"], "expected_version": row["version"]}
                    url = "/api/runs/" + row["id"] + "/approve"
                    client.post(url, json=body).raise_for_status()
                    client.post(url, json=body).raise_for_status()
                    approved = True
                if row["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.25)
            report = client.get("/api/runs/" + row["id"] + "/report").raise_for_status().json()
            results.append(report)
    passed = len(results) > 0 and all(row["run"]["state"] == "succeeded" for row in results)
    result = {"status": "passed" if passed else "failed", "scope": "actual HTTP and OCI scripted workflow",
              "model_called": False, "health": health, "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "task_count": len(results), "model_called": False,
                      "states": [row["run"]["state"] for row in results]}, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
