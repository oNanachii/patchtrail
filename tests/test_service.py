"""Runtime integration with explicit fake execution; never runs fixture code here."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from patchtrail.config import Settings
from patchtrail.sandbox import CheckResult, SandboxUnavailable
from patchtrail.service import RunService, ServiceError, public_run
from patchtrail.storage import StoreConflict
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK = "quantity-shortfall"


class FakeRunner:
    """Only compares source text with a curated answer; does not execute any code."""

    def __init__(self):
        self.online = True
        self.calls = []
        self.force_result = None

    def available(self):
        return self.online

    def run(self, workspace, command, run_id):
        if not self.online:
            raise SandboxUnavailable("explicit test fake is offline")
        if command != CHECK_COMMAND:
            raise AssertionError("A model-selected command reached the runner")
        task = get_task(TASK)
        content = (workspace / task.demo_path).read_text(encoding="utf-8")
        self.calls.append({"workspace": Path(workspace), "run_id": run_id, "content": content})
        if self.force_result is not None:
            return self.force_result
        fixed = content == task.demo_content
        output = "Ran 4 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=1)\n")
        return CheckResult(0 if fixed else 1, output, 1, False, False, runner="fake-no-code-execution")


class FakeProvider:
    label = "Explicit fake provider for tests"

    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def complete(self, messages, task, call_number, remaining_seconds):
        self.calls.append(copy.deepcopy(messages))
        index = len(self.calls) - 1
        if index >= len(self.batches) or self.batches[index] is None:
            return {"role": "assistant", "content": "I fixed everything; trust me."}, {}
        calls = [{"id": f"fake-{index}-{number}", "type": "function",
                  "function": {"name": name, "arguments": json.dumps(arguments)}}
                 for number, (name, arguments) in enumerate(self.batches[index])]
        return {"role": "assistant", "content": None, "tool_calls": calls}, {"total_tokens": 10}


class SimulatedProcessExit(BaseException):
    pass


class CrashProvider:
    label = "Explicit process-interruption fake"

    def __init__(self):
        self.calls = 0

    def complete(self, *args):
        self.calls += 1
        raise SimulatedProcessExit()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-service-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name), api_key="test-key-not-real",
                                 admin_token="test-operator-not-real")
        self.runner = FakeRunner()
        self.service = self.make_service()

    def make_service(self, provider=None):
        service = RunService(self.settings, runner=self.runner, provider=provider, dispatch=False)
        self.addCleanup(service.close)
        return service

    def create(self, key="request-one", mode="demo"):
        return self.service.create(TASK, mode, key)

    def pending(self):
        row = self.create()
        self.service.diagnose(row["id"])
        result = self.service.store.get_run(row["id"])
        self.assertEqual(result["state"], "awaiting_approval")
        return result

    def test_scripted_diagnosis_approval_and_fresh_verification(self):
        task = get_task(TASK)
        fixture_before = (task.source_dir / task.demo_path).read_bytes()
        pending = self.pending()
        self.assertEqual(len(self.runner.calls), 1)
        investigation = self.runner.calls[0]["workspace"]
        self.assertEqual((investigation / task.demo_path).read_bytes(), fixture_before)
        self.assertNotEqual(pending["proposal"]["content"], fixture_before.decode())
        self.assertIn("Scripted", public_run(pending)["provider_label"])
        approved = self.service.approve(pending["id"], pending["proposal"]["digest"], pending["version"])
        self.assertEqual(approved["state"], "verifying")
        self.service.verify(pending["id"])
        final = self.service.store.get_run(pending["id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("succeeded", "verifier_passed"))
        self.assertEqual(final["result"]["runner"], "fake-no-code-execution")
        self.assertNotEqual(investigation, self.runner.calls[-1]["workspace"])
        self.assertEqual((investigation / task.demo_path).read_bytes(), fixture_before)
        self.assertEqual((task.source_dir / task.demo_path).read_bytes(), fixture_before)
        kinds = [event["kind"] for event in self.service.store.events(pending["id"])]
        self.assertIn("verification_started", kinds)

    def test_wrong_digest_and_stale_version_leave_proposal_pending(self):
        pending = self.pending()
        for digest, version in [("0" * 64, pending["version"]),
                                (pending["proposal"]["digest"], pending["version"] - 1)]:
            with self.assertRaises(StoreConflict):
                self.service.approve(pending["id"], digest, version)
        self.assertEqual(self.service.store.get_run(pending["id"])["state"], "awaiting_approval")
        self.assertEqual(len(self.runner.calls), 1)

    def test_lost_approval_response_can_be_retried_without_repeating_verification(self):
        pending = self.pending()
        arguments = (pending["id"], pending["proposal"]["digest"], pending["version"])
        first = self.service.approve(*arguments)
        retry = self.service.approve(*arguments)
        self.assertEqual((retry["state"], retry["version"]), ("verifying", first["version"]))
        self.service.verify(pending["id"])
        final = self.service.approve(*arguments)
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(len(self.runner.calls), 2)  # One failing baseline and one fake verification.
        self.assertEqual(sum(event["kind"] == "verification_started"
                             for event in self.service.store.events(pending["id"])), 1)

    def test_workspace_drift_invalidates_approval(self):
        pending = self.pending()
        path = self.settings.data_dir / "workspaces" / pending["id"] / get_task(TASK).demo_path
        path.write_text(path.read_text() + "\n# changed after review\n")
        with self.assertRaisesRegex(StoreConflict, "Workspace changed"):
            self.service.approve(pending["id"], pending["proposal"]["digest"], pending["version"])
        self.assertEqual(self.service.store.get_run(pending["id"])["state"], "awaiting_approval")

    def test_zero_tests_or_failed_verifier_cannot_mark_success(self):
        pending = self.pending()
        self.service.approve(pending["id"], pending["proposal"]["digest"], pending["version"])
        self.runner.force_result = CheckResult(0, "Ran 0 tests in 0.000s\n\nOK\n", 1, False, False, "fake")
        self.service.verify(pending["id"])
        final = self.service.store.get_run(pending["id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("failed", "verification_unconfirmed"))

    def test_natural_language_claim_is_not_task_completion(self):
        provider = FakeProvider([None])
        self.service.provider = provider
        row = self.create(mode="live")
        self.service.diagnose(row["id"])
        final = self.service.store.get_run(row["id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("failed", "no_patch_proposal"))
        self.assertEqual(len(self.runner.calls), 1)  # Fixed baseline runs before the model.

    def test_rejected_tools_return_observations_and_never_run_model_commands(self):
        task = get_task(TASK)
        provider = FakeProvider([
            [("run_check", {"check_id": "tests"})],
            [("run_shell", {"command": "do not execute"}),
             ("read_file", {"path": "../../outside"}),
             ("run_check", {"check_id": "arbitrary-command"}),
             ("propose_patch", {"path": "tests/test_quantities.py", "content": "bad", "explanation": "bad"})],
            [("propose_patch", {"path": task.demo_path, "content": task.demo_content, "explanation": "a bounded test patch"})],
        ])
        self.service.provider = provider
        row = self.create(mode="live")
        self.service.diagnose(row["id"])
        final = self.service.store.get_run(row["id"])
        self.assertEqual(final["state"], "awaiting_approval")
        events = self.service.store.events(row["id"])
        self.assertEqual(sum(event["kind"] == "tool_rejected" for event in events), 4)
        self.assertEqual(len(self.runner.calls), 1)
        messages = final["checkpoint"]["messages"]
        for number, message in enumerate(messages):
            if message.get("role") == "assistant":
                ids = [call["id"] for call in message.get("tool_calls", [])]
                following = messages[number + 1:number + 1 + len(ids)]
                self.assertEqual(ids, [result.get("tool_call_id") for result in following])

    def test_model_call_and_context_budgets_fail_before_unbounded_work(self):
        self.settings.max_calls = 1
        row = self.create()
        self.service.diagnose(row["id"])
        self.assertEqual(self.service.store.get_run(row["id"])["stop_reason"], "budget_exhausted")
        self.assertEqual(self.runner.calls, [])
        self.settings.max_calls = 8
        self.settings.max_input_chars = 10
        other = self.create("small-context")
        self.service.diagnose(other["id"])
        self.assertEqual(self.service.store.get_run(other["id"])["stop_reason"], "context_budget")

    def test_restart_preserves_pending_approval_and_explicit_verification_resume(self):
        pending = self.pending()
        self.service.close()
        restarted = self.make_service()
        restored = restarted.store.get_run(pending["id"])
        self.assertEqual(restored["state"], "awaiting_approval")
        self.assertEqual(restored["proposal"], pending["proposal"])
        restarted.approve(restored["id"], restored["proposal"]["digest"], restored["version"])
        restarted.close()
        recovered = self.make_service()
        interrupted = recovered.store.get_run(pending["id"])
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(len(self.runner.calls), 1)
        with self.assertRaises(StoreConflict):
            recovered.resume(interrupted["id"], interrupted["version"] - 1)
        resumed = recovered.resume(interrupted["id"], interrupted["version"])
        self.assertEqual(resumed["state"], "verifying")
        recovered.verify(interrupted["id"])
        self.assertEqual(recovered.store.get_run(interrupted["id"])["state"], "succeeded")

    def test_interrupted_model_request_waits_for_explicit_resume(self):
        crashing = CrashProvider()
        self.service.provider = crashing
        row = self.create(mode="live")
        with self.assertRaises(SimulatedProcessExit):
            self.service.diagnose(row["id"])
        self.assertTrue(self.service.store.get_run(row["id"])["checkpoint"]["pending_model_request"])
        self.service.close()
        provider = FakeProvider([None])
        restarted = self.make_service(provider)
        interrupted = restarted.store.get_run(row["id"])
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(provider.calls, [])
        restarted.resume(row["id"], interrupted["version"])
        restarted.diagnose(row["id"])
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("explicit_resume_after_unknown_model_outcome",
                      [event["kind"] for event in restarted.store.events(row["id"])])

    def test_idempotent_retry_survives_full_queue_and_runner_disconnection(self):
        rows = [self.create(f"request-{index}") for index in range(6)]
        with self.assertRaises(ServiceError) as full:
            self.create("one-too-many")
        self.assertEqual((full.exception.code, full.exception.status), ("queue_full", 429))
        self.runner.online = False
        retried = self.create("request-0")
        self.assertEqual(retried["id"], rows[0]["id"])
        with self.assertRaises(StoreConflict):
            self.service.create("batch-expiry", "demo", "request-0")
        with self.assertRaises(ServiceError) as offline:
            self.create("new-while-offline")
        self.assertEqual((offline.exception.code, offline.exception.status), ("sandbox_unavailable", 503))

    def test_verification_unavailability_fails_closed_and_cancel_is_terminal(self):
        pending = self.pending()
        self.service.approve(pending["id"], pending["proposal"]["digest"], pending["version"])
        self.runner.online = False
        self.service.verify(pending["id"])
        self.assertEqual(self.service.store.get_run(pending["id"])["stop_reason"], "sandbox_unavailable")
        self.runner.online = True
        queued = self.create("cancel-me")
        self.service.cancel(queued["id"])
        self.service.diagnose(queued["id"])
        self.assertEqual(self.service.store.get_run(queued["id"])["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
