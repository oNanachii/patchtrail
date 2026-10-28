"""Runtime error regressions with explicit fakes; no model network or code execution."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from patchtrail.config import Settings
from patchtrail.providers import DeepSeekProvider
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK_ID = "quantity-shortfall"
FAILING_TEST_OUTPUT = "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n"


def tool_message(name, arguments, identity):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": identity, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}


class InspectionOnlyRunner:
    """Returns a chosen result; does not import or execute workspace code."""
    def __init__(self, result=None):
        self.result = result or CheckResult(1, FAILING_TEST_OUTPUT, 1, False, False,
                                           "explicit-inspection-only-fake")
        self.calls = []

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if command != CHECK_COMMAND:
            raise AssertionError("Unexpected executable command reached an inspection-only fake")
        self.calls.append({"workspace": Path(workspace), "run_id": run_id})
        return self.result


class RuntimeEdgeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-runtime-edges-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name),
                                 api_key="explicit-unit-test-key-no-network",
                                 admin_token="explicit-unit-test-operator")

    def make_service(self, runner=None, provider=None):
        service = RunService(self.settings, runner=runner or InspectionOnlyRunner(),
                             provider=provider, dispatch=False)
        self.addCleanup(service.close)
        return service

    def test_null_usage_response_does_not_leave_a_live_run_diagnosing(self):
        task = get_task(TASK_ID)
        actions = [
            ("read_file", {"path": task.demo_path}),
            ("run_check", {"check_id": "tests"}),
            ("propose_patch", {"path": task.demo_path, "content": task.demo_content,
                               "explanation": "Known authored replacement used only by this test."}),
        ]
        received = []

        def fake_model_endpoint(request):
            # MockTransport intercepts this request; no provider service is contacted.
            received.append(json.loads(request.content))
            index = len(received) - 1
            if index >= len(actions):
                raise AssertionError("Unexpected additional model request")
            name, arguments = actions[index]
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "tool_calls", "message":
                             tool_message(name, arguments, f"null-usage-fake-{index}")}],
                "usage": None,
            })

        provider = DeepSeekProvider(self.settings, transport=httpx.MockTransport(fake_model_endpoint))
        runner = InspectionOnlyRunner()
        service = self.make_service(runner, provider)
        created = service.create(TASK_ID, "live", "null-usage-run-request")
        service.diagnose(created["id"])
        row = service.store.get_run(created["id"])
        self.assertEqual(row["state"], "awaiting_approval")
        self.assertIsNotNone(row["proposal"])
        self.assertEqual(row["checkpoint"]["usage"], {})
        self.assertFalse(row["checkpoint"].get("pending_model_request"))
        self.assertEqual(len(received), 3)
        self.assertEqual(len(runner.calls), 1)

    def test_expired_model_response_cannot_propose_a_patch(self):
        task = get_task(TASK_ID)
        clock = {"now": 100.0}

        class ExpiredResponseProvider:
            label = "explicit-expired-response-fake"

            def complete(self, messages, task, call_number, remaining_seconds):
                clock["now"] += 2.0
                return tool_message("propose_patch", {
                    "path": task.demo_path, "content": task.demo_content,
                    "explanation": "A late response must not enter the approval state."
                }, "expired-response-fake"), {}

        self.settings.max_seconds = 1
        runner = InspectionOnlyRunner()
        service = self.make_service(runner, ExpiredResponseProvider())
        row = service.create(task.id, "live", "expired-response-request")
        with patch("patchtrail.service.time.monotonic", side_effect=lambda: clock["now"]):
            service.diagnose(row["id"])
        final = service.store.get_run(row["id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("failed", "time_budget"))
        self.assertIsNone(final["proposal"])
        self.assertEqual(runner.calls, [])
        self.assertFalse(any(event["kind"] == "state_changed"
                             and event["payload"].get("to") == "awaiting_approval"
                             for event in service.store.events(row["id"])))

    def assert_unconfirmed_baseline(self, result, request_key):
        runner = InspectionOnlyRunner(result)
        service = self.make_service(runner)
        row = service.create(TASK_ID, "demo", request_key)
        service.diagnose(row["id"])
        final = service.store.get_run(row["id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("failed", "baseline_unconfirmed"))
        self.assertIsNone(final["proposal"])
        self.assertEqual(len(runner.calls), 1)
        unconfirmed = [event for event in service.store.events(row["id"])
                       if event["kind"] == "check_unconfirmed"]
        self.assertEqual(len(unconfirmed), 1)
        self.assertEqual(unconfirmed[0]["payload"]["exit_code"], result.exit_code)
        self.assertEqual(unconfirmed[0]["payload"]["tests_run"], 0)
        self.assertFalse(any(event["kind"] == "tool_requested"
                             and event["payload"].get("name") == "propose_patch"
                             for event in service.store.events(row["id"])))

    def test_oci_image_error_is_not_a_reproduced_failing_test(self):
        self.assert_unconfirmed_baseline(
            CheckResult(125, "OCI error: required image is absent; no tests ran\n", 1,
                        False, False, "explicit-oci-error-fake"),
            "oci-image-failure-request")

    def test_zero_test_run_is_not_a_reproduced_failing_baseline(self):
        self.assert_unconfirmed_baseline(
            CheckResult(0, "Ran 0 tests in 0.001s\n\nOK\n", 1,
                        False, False, "explicit-zero-tests-fake"),
            "zero-test-baseline-request")


if __name__ == "__main__":
    unittest.main()
