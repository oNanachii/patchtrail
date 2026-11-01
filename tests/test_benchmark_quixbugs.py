"""Benchmark integration with explicit no-network/no-execution test doubles."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.providers import ScriptedProvider, TOOL_SCHEMAS, initial_messages
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from patchtrail.tasks import get_task
from patchtrail.workspace import tree_hash
from scripts.benchmark_meter import Meter
from scripts.benchmark_quixbugs import (MeteredAgent, approve_http, call_metrics,
                                        main as benchmark_main, run_baseline, summarize)


class InspectionRunner:
    """Only compare bytes; never execute model output or fixture code."""
    def __init__(self, task):
        self.task = task
        self.calls = 0

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if command != self.task.check_command:
            raise AssertionError("Unexpected test command")
        self.calls += 1
        fixed = (workspace / self.task.demo_path).read_text() == self.task.demo_content
        output = "Ran 4 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=1)\n")
        return CheckResult(0 if fixed else 1, output, 1, False, False,
                           "explicit-fake-inspection-no-execution")


def completion(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "test-response-identity", "model": "deepseek-flash",
            "choices": [{"finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 20,
                      "completion_tokens": 20, "total_tokens": 120}}


class BenchmarkIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="benchmark-integration-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.task = get_task("quantity-shortfall")
        self.settings = Settings(data_dir=self.directory / "runtime", api_key="fake-model-key",
                                 admin_token="benchmark-test-operator", public_demo=False)
        self.runner = InspectionRunner(self.task)
        self.service = RunService(self.settings, runner=self.runner,
                                  provider=ScriptedProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        self.preflight = {"source_hash": tree_hash(self.task.source_dir),
                          "check": {"output": "FAILED (failures=1)\n"}}

    def meter(self, body, captured=None):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            if captured is not None:
                captured.append(json.loads(request.content))
            return httpx.Response(200, json=body)
        return Meter(self.directory / "calls.json", self.settings.api_key,
                     transport=httpx.MockTransport(handler))

    def test_agent_uses_original_provider_parser_after_one_metered_dispatch(self):
        tool = {"id": "call-test", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": self.task.demo_path})}}
        captured = []
        meter = self.meter(completion(tool_calls=[tool]), captured)
        provider = MeteredAgent(self.settings, meter)
        messages = initial_messages(self.task)
        message, usage = provider.complete(messages, self.task, 1, 5)
        self.assertEqual(message["tool_calls"], [tool])
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(provider.last_completion_metadata["response_id"], "test-response-identity")
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(meter.calls), 1)
        self.assertEqual(captured[0]["tools"], TOOL_SCHEMAS)
        self.assertEqual(captured[0]["messages"], messages)
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertEqual(meter.calls[0]["context"]["method"], "agent")

    def test_baseline_single_completion_applies_only_allowed_file_and_verifies(self):
        replacement = {"files": [{"path": self.task.demo_path, "content": self.task.demo_content}]}
        captured = []
        meter = self.meter(completion(content=json.dumps(replacement)), captured)
        original = tree_hash(self.task.source_dir)
        result = run_baseline(self.service, self.task, self.preflight, meter)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(result["verification"]["runner"], "explicit-fake-inspection-no-execution")
        self.assertTrue(result["immutable_tests_unchanged"])
        self.assertEqual(tree_hash(self.task.source_dir), original)
        self.assertNotIn("tools", captured[0])
        prompt = json.loads(captured[0]["messages"][1]["content"])
        self.assertIn(self.task.demo_path, prompt["source_files"])
        self.assertFalse(any(path.startswith("tests/") for path in prompt["source_files"]))
        self.assertEqual(prompt["failing_test_output"], self.preflight["check"]["output"])

    def test_baseline_test_replacement_is_rejected_before_verifier(self):
        replacement = {"files": [{"path": "tests/test_quantities.py", "content": "# bypass tests\n"}]}
        meter = self.meter(completion(content=json.dumps(replacement)))
        original = tree_hash(self.task.source_dir)
        result = run_baseline(self.service, self.task, self.preflight, meter)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "unauthorized_or_invalid_patch_rejected")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(tree_hash(self.task.source_dir), original)

    def test_http_approval_requires_exact_version_and_records_script_actor(self):
        row = self.service.create(self.task.id, "live", "benchmark-test-request")
        self.service.diagnose(row["id"])
        row = self.service.store.get_run(row["id"])
        self.assertEqual(row["state"], "awaiting_approval")
        with patch("patchtrail.api.load_local_env"):
            app = create_app(service=self.service)
        stale = {**row, "version": row["version"] - 1}
        with self.assertRaises(RuntimeError):
            approve_http(app, self.settings.admin_token, stale)
        self.assertEqual(self.service.store.get_run(row["id"])["state"], "awaiting_approval")
        approval = approve_http(app, self.settings.admin_token, row)
        self.assertEqual(approval["actor"], "脚本自动批准")
        self.assertFalse(approval["human_review"])
        self.assertEqual(approval["proposal_digest"], row["proposal"]["digest"])
        current = self.service.store.get_run(row["id"])
        self.assertEqual(current["state"], "verifying")
        self.assertEqual(current["checkpoint"]["approval"]["from_version"], row["version"])
        self.service.verify(row["id"])
        self.assertEqual(self.service.store.get_run(row["id"])["state"], "succeeded")

    def test_call_metrics_preserves_unknown_usage_and_trace_indexes(self):
        calls = [{"index": 1, "status": "confirmed", "usage": {"total_tokens": 120},
                  "tariff_yuan": 0.001}, {"index": 2, "status": "unknown"}]
        result = call_metrics(calls)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["unknown_calls"], 1)
        self.assertEqual(result["call_indexes"], [1, 2])
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertFalse(result["usage_complete"])

    def test_summary_keeps_unknown_tokens_unavailable_and_eligible_denominator(self):
        confirmed = {"status": "succeeded", "model_calls": 1,
                     "usage": {"total_tokens": 120}, "usage_complete": True,
                     "unknown_calls": 0, "tariff_yuan": 0.001, "elapsed_seconds": 1}
        unknown = {"status": "failed", "model_calls": 1, "usage": {},
                   "usage_complete": False, "unknown_calls": 1, "tariff_yuan": 0,
                   "elapsed_seconds": 2, "failure_category": "unknown_model_outcome"}
        summary = summarize({"cases": [
            {"eligible": True, "baseline": confirmed},
            {"eligible": True, "baseline": unknown},
            {"eligible": False, "skip_reason": "environment_missing"}]})
        baseline = summary["baseline"]
        self.assertEqual(baseline["eligible_tasks"], 2)
        self.assertEqual(baseline["attempted_tasks"], 2)
        self.assertEqual(baseline["repair_rate"], 0.5)
        self.assertIsNone(baseline["mean_total_tokens"])
        self.assertEqual(baseline["total_tokens"], 120)
        self.assertFalse(baseline["tokens_complete"])
        self.assertEqual(baseline["unknown_calls"], 1)
        self.assertEqual(summary["agent"]["not_run_tasks"], 2)
        self.assertEqual(summary["paired"]["tasks"], 0)

    def test_cli_app_initialization_failure_is_saved_before_screening_or_dispatch(self):
        directory = self.directory / "cli-abort"
        output = self.directory / "artifacts"
        dataset = {"upstream": {"commit": "test-pinned-revision"}, "skipped": [], "tasks": [
            {"name": "must-not-screen", "source_dir": "unused", "manifest": {}}]}
        image = SimpleNamespace(returncode=0, stdout="sha256:" + "a" * 64 + "\n")
        with patch("scripts.benchmark_quixbugs.subprocess.run", return_value=image), \
                patch("scripts.benchmark_quixbugs.prepare_dataset", return_value=dataset), \
                patch("scripts.benchmark_quixbugs.create_app",
                      side_effect=RuntimeError("Required static directory is unavailable")), \
                patch.object(RunService, "preflight") as preflight, \
                patch.object(Meter, "send") as dispatch:
            with self.assertRaises(RuntimeError):
                benchmark_main(["dry-run", "--data-dir", str(directory),
                                "--output-dir", str(output), "--pytest-image", "prepared-test-image"])
        artifacts = list(output.glob("benchmark-quixbugs-*.json"))
        self.assertEqual(len(artifacts), 1)
        saved = json.loads(artifacts[0].read_text())
        self.assertEqual(saved["state"], "aborted")
        self.assertEqual(saved["runtime_failure"]["type"], "RuntimeError")
        self.assertEqual(saved["cases"], [])
        self.assertEqual(saved["summary"]["baseline"]["attempted_tasks"], 0)
        self.assertFalse((directory / "benchmark-started.json").exists())
        preflight.assert_not_called()
        dispatch.assert_not_called()

    def test_cli_started_directory_refuses_before_any_runner_or_api_dispatch(self):
        directory = self.directory / "already-started"
        directory.mkdir()
        marker = directory / "benchmark-started.json"
        marker.write_text('{"artifact":"existing-paid-attempt.json"}\n')
        original = marker.read_bytes()
        with patch("scripts.benchmark_quixbugs.subprocess.run") as runner, \
                patch("scripts.benchmark_quixbugs.prepare_dataset") as converter, \
                patch("scripts.benchmark_quixbugs.create_app") as app, \
                patch.object(Meter, "send") as dispatch, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                benchmark_main(["run", "--data-dir", str(directory),
                                "--pytest-image", "prepared-test-image"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(marker.read_bytes(), original)
        runner.assert_not_called()
        converter.assert_not_called()
        app.assert_not_called()
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
