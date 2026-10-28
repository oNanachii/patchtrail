"""Private project HTTP/orchestration boundaries with explicit no-execution fakes."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.projects import ProjectCatalog
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService


class NoExecutionRunner:
    def available(self):
        return True

    def run(self, workspace, command, run_id):
        fixed = (workspace / "a.py").read_text() == "VALUE = 2\n" and (workspace / "b.py").read_text() == "VALUE = 3\n"
        return CheckResult(0 if fixed else 1,
            "Ran 2 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=2)\n"),
            1, False, False, "fake-no-execution")


class NoNetworkProvider:
    label = "explicit fake; no network"

    def complete(self, messages, task, call_number, remaining_seconds):
        name, arguments = ("run_check", {"check_id": "tests"}) if call_number == 1 else (
            "propose_patch_bundle", {"files": [{"path": "a.py", "content": "VALUE = 2\n"},
                                               {"path": "b.py", "content": "VALUE = 3\n"}],
                                     "explanation": "An explicit fake proposal for protocol validation"})
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "same-provider-id",
            "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, {}


class PrivateProjectTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="patchtrail-private-api-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "source"
        source.mkdir()
        for name, text in {"a.py": "VALUE = 0\n", "b.py": "VALUE = 0\n", "tests/__init__.py": "",
                           "tests/test_values.py": "import unittest\nclass Values(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n"}.items():
            p = source / name
            p.parent.mkdir(exist_ok=True)
            p.write_text(text)
        self.settings = Settings(data_dir=root / "data", api_key="fake-provider-key", admin_token="private-operator-test")
        catalog = ProjectCatalog(self.settings.data_dir / "projects")
        catalog.import_project(source, {"schema_version": 1, "project_name": "private-sentinel",
            "title": "Private title sentinel", "description": "Private source only",
            "files": ["a.py", "b.py", "tests/__init__.py", "tests/test_values.py"],
            "editable_files": ["a.py", "b.py"], "check_profile": "unittest", "test_paths": ["tests"]})
        self.task_id = catalog.list_tasks()[0]["id"]
        self.service = RunService(self.settings, runner=NoExecutionRunner(), provider=NoNetworkProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        with patch("patchtrail.api.load_local_env"):
            self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)
        self.auth = {"Authorization": "Bearer " + self.settings.admin_token}

    def test_private_catalog_and_create_do_not_leak_to_anonymous_visitors(self):
        self.assertNotIn("Private title sentinel", self.client.get("/api/tasks").text)
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertIn(self.task_id, self.client.get("/api/tasks", headers=self.auth).text)
        self.assertEqual(self.client.post("/api/runs", json={"task_id": self.task_id, "mode": "demo"},
            headers={"Idempotency-Key": "private-attempt-key"}).status_code, 401)
        denied = self.client.post("/api/runs", json={"task_id": self.task_id, "mode": "demo"},
            headers={**self.auth, "Idempotency-Key": "private-attempt-key"})
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(denied.json()["error"]["code"], "private_task_requires_live")

    def test_bundle_workflow_export_and_all_private_read_routes(self):
        headers = {**self.auth, "Idempotency-Key": "private-live-create"}
        response = self.client.post("/api/runs", headers=headers, json={"task_id": self.task_id, "mode": "live"})
        self.assertEqual(response.status_code, 201, response.text)
        rid = response.json()["id"]
        self.service.diagnose(rid)
        detail = self.client.get("/api/runs/" + rid, headers=self.auth).json()
        self.assertEqual(detail["state"], "awaiting_approval")
        self.assertTrue(detail["private"])
        self.assertEqual(len(detail["proposal"]["files"]), 2)
        self.assertTrue(all("content" not in entry for entry in detail["proposal"]["files"]))
        for suffix in ("", "/events", "/report", "/patch.diff"):
            self.assertEqual(self.client.get("/api/runs/" + rid + suffix).status_code, 401)
        self.assertNotIn(rid, self.client.get("/api/runs").text)
        exported = self.client.get("/api/runs/" + rid + "/patch.diff", headers=self.auth)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("a/a.py", exported.text)
        self.assertIn("a/b.py", exported.text)
        self.assertEqual(exported.headers["x-proposal-digest"], detail["proposal"]["digest"])
        approval = {"proposal_digest": detail["proposal"]["digest"], "expected_version": detail["version"]}
        first = self.client.post("/api/runs/" + rid + "/approve", headers=self.auth, json=approval)
        retry = self.client.post("/api/runs/" + rid + "/approve", headers=self.auth, json=approval)
        self.assertEqual(first.json()["version"], retry.json()["version"])
        self.service.verify(rid)
        final = self.client.get("/api/runs/" + rid + "/report", headers=self.auth).json()
        self.assertEqual(final["run"]["state"], "succeeded")
        self.assertEqual(final["run"]["result"]["runner"], "fake-no-execution")

    def test_already_passing_baseline_stops_before_a_patch_or_second_model_call(self):
        class PassingRunner(NoExecutionRunner):
            def run(self, *args):
                return CheckResult(0, "Ran 2 tests in 0.001s\n\nOK\n", 1, False, False, "fake-no-execution")
        self.service.runner = PassingRunner()
        row = self.service.create(self.task_id, "live", "passing-baseline-private")
        self.service.diagnose(row["id"])
        final = self.service.store.get_run(row["id"])
        self.assertEqual(final["stop_reason"], "baseline_passed")
        self.assertIsNone(final["proposal"])
        calls = [event for event in self.service.store.events(row["id"]) if event["kind"] == "model_requested"]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
