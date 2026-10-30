"""Paid model calls require a complete failing fixed baseline from the same source."""
import time
import unittest
from unittest.mock import patch

from patchtrail.providers import ScriptedProvider
from patchtrail.sandbox import CheckResult
from test_api import TASK
from test_byok import ByokCase


FAIL = CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n", 1, False, False, "fake-no-execution")
PASS = CheckResult(0, "Ran 4 tests in 0.001s\n\nOK\n", 1, False, False, "fake-no-execution")
ZERO = CheckResult(0, "Ran 0 tests in 0.001s\n\nOK\n", 1, False, False, "fake-no-execution")
SKIP = CheckResult(0, "Ran 4 tests in 0.001s\n\nOK (skipped=4)\n", 1, False, False, "fake-no-execution")
TIMEOUT = CheckResult(None, "started", 20_000, True, False, "fake-no-execution")
TRUNCATED = CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n", 1, False, True, "fake-no-execution")
MISSING_IMPORT = CheckResult(1, "ERROR: unittest.loader._FailedTest.test_app\nModuleNotFoundError: No module named dependency\nRan 1 test in 0.001s\n\nFAILED (errors=1)\n", 1, False, False, "fake-no-execution")


class PreflightTests(ByokCase):
    def test_preflight_is_free_and_reports_baseline_without_creating_run(self):
        member = self.member()
        cases = ((FAIL, "ready", True), (PASS, "already_passed", False), (ZERO, "unconfirmed", False),
                 (SKIP, "unconfirmed", False), (TIMEOUT, "timed_out", False), (TRUNCATED, "unconfirmed", False))
        for result, status, eligible in cases:
            with self.subTest(status=status, output=result.output):
                with patch.object(self.runner, "run", return_value=result):
                    flight = self.preflight(member)
                self.assertEqual(flight["status"], status)
                self.assertEqual(flight["eligible"], eligible)
                self.assertEqual(flight["task_id"], TASK)
                self.assertRegex(flight["source_hash"], r"^[a-f0-9]{64}$")
                self.assertIn("check", flight)
                if not eligible:
                    response = self.create_live(member, "invalid-flight-" + status, flight)
                    self.assertEqual(response.status_code, 409, response.text)
        service = self.app.state.member_services[member["id"]]
        self.assertEqual(service.store.list_runs(), [])
        self.assertEqual(self.calls, [])

    def test_missing_container_returns_ineligible_preflight_without_model(self):
        member = self.member()
        self.runner.online = False
        response = self.client.post(f"/api/tasks/{TASK}/preflight", headers=member["headers"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["eligible"])
        self.assertEqual(response.json()["status"], "environment_missing")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.calls, 0)

    def test_member_live_requires_valid_own_unexpired_preflight(self):
        alice, bob = self.member("flightalice"), self.member("flightbob")
        denied = self.client.post("/api/runs", headers={**alice["headers"], "Idempotency-Key": "missing-preflight"},
                                  json={"task_id": TASK, "mode": "live"})
        self.assertEqual(denied.status_code, 409)
        flight = self.preflight(alice)
        crossed = self.create_live(bob, "crossed-preflight", flight)
        self.assertEqual(crossed.status_code, 409)
        expiry = time.time() + 901
        with patch("patchtrail.service.time.time", return_value=expiry):
            expired = self.create_live(alice, "expired-preflight", flight)
        self.assertEqual(expired.status_code, 409, expired.text)
        self.assertEqual(self.calls, [])

    def test_preflight_is_bound_to_test_configuration_and_current_snapshot(self):
        member = self.member()
        flight = self.preflight(member)
        service = self.app.state.member_services[member["id"]]
        original = service.settings.container_image
        service.settings.container_image = "different-fixed-test-image"
        changed = self.create_live(member, "changed-check-config", flight)
        self.assertEqual(changed.status_code, 409, changed.text)
        service.settings.container_image = original
        preview, task = self.import_task(member["headers"], "Drift protected private snapshot")
        with patch.object(self.runner, "run", return_value=FAIL):
            imported_flight = self.preflight(member, task["id"])
        snapshot = service.resolve_task(task["id"]).source_dir
        target = snapshot / "app.py"
        target.chmod(0o600)
        target.write_text("changed source after preflight\n")
        rejected = self.create_live(member, "changed-source-snapshot", imported_flight, task["id"])
        self.assertIn(rejected.status_code, (409, 503), rejected.text)
        self.assertEqual(self.calls, [])

    def test_legacy_direct_live_performs_baseline_before_any_model_request(self):
        calls = []
        class RecordingProvider:
            label = "explicit recording no-network provider"
            def complete(self, *args):
                calls.append(1)
                return ScriptedProvider().complete(*args)
        self.service.provider = RecordingProvider()
        for index, result in enumerate((PASS, ZERO, SKIP, TIMEOUT, TRUNCATED, MISSING_IMPORT)):
            with self.subTest(output=result.output):
                row = self.service.create(TASK, "live", "legacy-baseline-" + str(index))
                with patch.object(self.runner, "run", return_value=result):
                    self.service.diagnose(row["id"])
                final = self.service.store.get_run(row["id"])
                self.assertEqual(final["state"], "failed")
                self.assertIsNone(final["proposal"])
                self.assertEqual(calls, [])
                self.assertEqual([event for event in self.service.store.events(row["id"])
                                  if event["kind"] == "model_requested"], [])

    def test_expiring_session_between_model_calls_stops_next_request(self):
        member = self.member()
        row = self.create_live(member).json()
        calls = []
        registry = self.app.state.members
        expiration = member["session"]["expires_at"] + 1
        def complete_then_expire(provider, messages, task, call_number, remaining_seconds):
            calls.append(call_number)
            registry.clock = lambda: expiration
            return ScriptedProvider().complete(messages, task, call_number, remaining_seconds)
        with patch("patchtrail.providers.DeepSeekProvider.complete", new=complete_then_expire):
            self.app.state.member_services[member["id"]].diagnose(row["id"])
        self.assertEqual(calls, [1])
        final = self.app.state.member_services[member["id"]].store.get_run(row["id"])
        self.assertNotEqual(final["state"], "succeeded")
        self.assertIsNone(final["proposal"])


if __name__ == "__main__":
    unittest.main()
