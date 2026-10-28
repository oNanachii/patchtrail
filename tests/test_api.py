"""HTTP contract tests using injected fakes, never a real model or host executor."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.sandbox import CheckResult, SandboxUnavailable
from patchtrail.service import RunService
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK = "quantity-shortfall"


class FakeRunner:
    """Inspection-only fake: no subprocess, imports or execution of fixture code."""

    def __init__(self):
        self.online = True
        self.calls = 0

    def available(self):
        return self.online

    def run(self, workspace, command, run_id):
        if not self.online:
            raise SandboxUnavailable("explicit fake runner offline")
        if command != CHECK_COMMAND:
            raise AssertionError("Unexpected command")
        self.calls += 1
        task = get_task(TASK)
        fixed = (workspace / task.demo_path).read_text() == task.demo_content
        output = "Ran 4 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=1)\n")
        return CheckResult(0 if fixed else 1, output, 1, False, False, "fake-no-execution")


class NoNetworkProvider:
    label = "Explicit no-network fake"

    def complete(self, *args):
        raise AssertionError("API tests do not dispatch live model calls")


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-api-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="",
                                 admin_token="operator-token-for-tests")
        self.runner = FakeRunner()
        self.service = RunService(self.settings, runner=self.runner,
                                  provider=NoNetworkProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        # Tests must not read a user's local credential file or mutate their env.
        with patch("patchtrail.api.load_local_env"):
            self.app = create_app(self.service)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def headers(self, key="request-for-tests", auth=False):
        result = {"Idempotency-Key": key}
        if auth:
            result["Authorization"] = "Bearer " + self.settings.admin_token
        return result

    def create(self, key="request-for-tests", mode="demo", auth=False, **fields):
        return self.client.post("/api/runs", headers=self.headers(key, auth),
                                json={"task_id": TASK, "mode": mode, **fields})

    def test_health_and_catalog_are_honest_and_do_not_expose_solutions(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["model_configured"])
        self.assertIn("scripted", health.json()["demo_mode"])
        self.assertNotIn(self.settings.admin_token, health.text)
        tasks = self.client.get("/api/tasks")
        self.assertEqual(tasks.status_code, 200)
        self.assertIn("not blind", tasks.json()["evaluation_scope"])
        for task in tasks.json()["tasks"]:
            self.assertIn("source_hash", task)
            self.assertNotIn("demo_content", task)
            self.assertNotIn("source_dir", task)

    def test_demo_flow_exposes_diff_events_and_verifier_but_no_private_messages(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["id"]
        self.assertIn("Scripted", response.json()["provider_label"])
        self.service.diagnose(run_id)
        pending = self.client.get("/api/runs/" + run_id)
        self.assertEqual(pending.json()["state"], "awaiting_approval")
        self.assertNotIn("messages", pending.json()["checkpoint"])
        self.assertNotIn("content", pending.json()["proposal"])
        self.assertIn("diff", pending.json()["proposal"])
        approval = {"proposal_digest": pending.json()["proposal"]["digest"],
                    "expected_version": pending.json()["version"]}
        approved = self.client.post(f"/api/runs/{run_id}/approve", json=approval)
        self.assertEqual(approved.status_code, 200, approved.text)
        retry = self.client.post(f"/api/runs/{run_id}/approve", json=approval)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["version"], approved.json()["version"])
        self.service.verify(run_id)
        completed_retry = self.client.post(f"/api/runs/{run_id}/approve", json=approval)
        self.assertEqual(completed_retry.status_code, 200, completed_retry.text)
        self.assertEqual(completed_retry.json()["state"], "succeeded")
        self.assertEqual(self.runner.calls, 2)
        report = self.client.get(f"/api/runs/{run_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["run"]["state"], "succeeded")
        self.assertEqual(report.json()["run"]["result"]["runner"], "fake-no-execution")
        self.assertIn("separate", report.json()["evidence_boundary"])
        events = self.client.get(f"/api/runs/{run_id}/events").json()["events"]
        self.assertIn("verification_started", [event["kind"] for event in events])
        cursor = events[-2]["id"]
        tail = self.client.get(f"/api/runs/{run_id}/events", params={"after": cursor}).json()["events"]
        self.assertTrue(all(event["id"] > cursor for event in tail))
        self.assertEqual(report.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", report.headers["Content-Security-Policy"])

    def test_live_requires_operator_and_configured_key(self):
        denied = self.create(mode="live")
        self.assertEqual(denied.status_code, 401)
        missing_key = self.create(mode="live", auth=True)
        self.assertEqual(missing_key.status_code, 503)
        self.assertEqual(missing_key.json()["error"]["code"], "model_not_configured")
        self.settings.api_key = "not-a-real-key-for-tests"
        allowed = self.create(mode="live", auth=True)
        self.assertEqual(allowed.status_code, 201, allowed.text)
        self.assertNotIn(self.settings.api_key, allowed.text)

    def test_live_reads_and_mutations_require_auth_and_checkpoint_is_filtered(self):
        self.settings.api_key = "fake-private-key"
        live = self.create(mode="live", auth=True).json()
        run_id = live["id"]
        marker = "private-model-history-must-not-be-public"
        self.service.store.transition(run_id, {"queued"}, "diagnosing", {
            "checkpoint": {"messages": [{"role": "assistant", "content": marker}],
                           "calls": 1, "pending_model_request": "unconfirmed-id"}
        })
        self.service.store.append_event(run_id, "private_event", {"note": "private run evidence"})
        anonymous = self.client.get("/api/runs")
        self.assertEqual(anonymous.status_code, 200)
        self.assertEqual(anonymous.json()["runs"], [])
        for suffix in ("", "/events", "/report"):
            endpoint = f"/api/runs/{run_id}{suffix}"
            self.assertEqual(self.client.get(endpoint).status_code, 401, endpoint)
            response = self.client.get(endpoint, headers=self.headers(auth=True))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(marker, response.text)
            self.assertNotIn(self.settings.api_key, response.text)
        authenticated = self.client.get("/api/runs", headers=self.headers(auth=True))
        self.assertEqual([row["id"] for row in authenticated.json()["runs"]], [run_id])
        self.assertEqual(self.client.post(f"/api/runs/{run_id}/cancel").status_code, 401)
        public = self.client.get(f"/api/runs/{run_id}", headers=self.headers(auth=True)).json()
        self.assertTrue(public["checkpoint"]["unconfirmed_model_request"])

    def test_disabling_public_demo_protects_all_run_access(self):
        demo = self.create().json()
        self.settings.public_demo = False
        self.assertEqual(self.create("second-demo-key").status_code, 401)
        for endpoint in ["/api/runs", f"/api/runs/{demo['id']}",
                         f"/api/runs/{demo['id']}/events", f"/api/runs/{demo['id']}/report"]:
            self.assertEqual(self.client.get(endpoint).status_code, 401, endpoint)
            self.assertEqual(self.client.get(endpoint, headers=self.headers(auth=True)).status_code, 200)

    def test_idempotent_http_retry_is_available_after_queue_fills_and_runner_disconnects(self):
        first = self.create("request-0000").json()
        for number in range(1, 6):
            self.assertEqual(self.create(f"request-{number:04}").status_code, 201)
        self.runner.online = False
        retry = self.create("request-0000")
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["id"], first["id"])
        conflict = self.client.post("/api/runs", headers=self.headers("request-0000"),
                                    json={"task_id": "batch-expiry", "mode": "demo"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.service.store.list_runs()), 6)

    def test_no_container_returns_503_without_creating_or_executing_run(self):
        self.runner.online = False
        response = self.create()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "sandbox_unavailable")
        self.assertEqual(self.service.store.list_runs(), [])
        self.assertEqual(self.runner.calls, 0)

    def test_input_contract_rejects_extra_code_and_never_echoes_it(self):
        marker = "untrusted-user-code-secret-marker"
        response = self.create(content=marker)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(marker, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(self.create(mode="unknown").status_code, 422)
        self.assertEqual(self.client.post("/api/runs", json={"task_id": TASK}).status_code, 422)
        pending = self.create().json()
        self.service.diagnose(pending["id"])
        row = self.service.store.get_run(pending["id"])
        bad = self.client.post(f"/api/runs/{row['id']}/approve", json={
            "proposal_digest": row["proposal"]["digest"], "expected_version": True})
        self.assertEqual(bad.status_code, 422)
        wrong = self.client.post(f"/api/runs/{row['id']}/approve", json={
            "proposal_digest": "0" * 64, "expected_version": row["version"]})
        self.assertEqual(wrong.status_code, 409)

    def test_declared_and_actual_request_body_sizes_are_bounded(self):
        for content, extra in [(b"x" * 4097, {}), (b"x" * 4097, {"Content-Length": "1"})]:
            response = self.client.post("/api/runs", content=content,
                                        headers={**self.headers(), **extra})
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(response.json()["error"]["code"], "request_too_large")
        invalid = self.client.post("/api/runs", content=b"{}",
                                   headers={**self.headers(), "Content-Length": "not-a-number"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.service.store.list_runs(), [])

    def test_missing_run_unknown_task_and_invalid_event_cursor_have_structured_errors(self):
        for suffix in ("", "/events", "/report"):
            response = self.client.get("/api/runs/missing" + suffix)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        unknown = self.client.post("/api/runs", headers=self.headers(), json={"task_id": "unknown-task"})
        self.assertEqual(unknown.status_code, 404)
        row = self.create().json()
        bad_cursor = self.client.get(f"/api/runs/{row['id']}/events", params={"after": -1})
        self.assertEqual(bad_cursor.status_code, 400)


if __name__ == "__main__":
    unittest.main()
