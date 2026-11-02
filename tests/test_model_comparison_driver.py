"""Compare-controller contracts without model dispatch or host task execution."""
from contextlib import redirect_stderr, redirect_stdout
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from patchtrail.check_profiles import parse_check_command
from patchtrail.config import ROOT, Settings
from patchtrail.providers import OpenAICompatibleProvider, ProviderError, TOOL_SCHEMAS, initial_messages
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from patchtrail.tasks import get_task
from patchtrail.workspace import tree_hash
from scripts import benchmark_model_comparison as driver
from scripts.benchmark_quixbugs import summarize
from scripts.zhipu_benchmark_meter import ENDPOINT, ZhipuMeter


class InspectionRunner:
    """Confirm unchanged fixture bytes only; no Python source is imported."""
    def __init__(self, source_hash):
        self.source_hash, self.calls = source_hash, 0

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if parse_check_command(command) != ("unittest", ("tests",)):
            raise AssertionError("Only the manifest's fixed check may run")
        if tree_hash(workspace) != self.source_hash:
            raise AssertionError("Fixture bytes changed before screening")
        self.calls += 1
        return CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n",
                           1, False, False, "explicit-comparison-byte-inspection-no-execution")


class ModelComparisonDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-model-comparison-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def fixture(self):
        """A patched unit-test reference; the real checker pins all P1 bytes."""
        source_root = self.root / "sources"
        source = source_root / "fixture-00" / "source"
        files = {"app.py": "def increment(value):\n    return value\n",
                 "tests/__init__.py": "",
                 "tests/test_app.py": "# Unit fixture inspected as bytes only.\n"}
        for name, text in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        manifest = {"schema_version": 1, "project_name": "comparison-unit-fixture",
            "title": "Comparison fixture", "description": "Repair the unit fixture increment function.",
            "files": sorted(files), "editable_files": ["app.py"], "check_profile": "unittest",
            "test_paths": ["tests"], "tags": ["unit-fixture"], "provenance": "Self-authored inert unit fixture."}
        hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
        source_hash = tree_hash(source)
        cases = [{"name": f"fixture-{number:02}", "source_hash": source_hash,
                  "eligible": number == 0, "preflight": {"check": {"tests_run": 4}},
                  **({"skip_reason": "retained-unit-exclusion"} if number else {})}
                 for number in range(40)]
        reference = {"environment": {"image": "a" * 64}, "cases": cases,
            "dataset": {"tasks": [{"name": "fixture-00", "manifest": manifest, "files_sha256": hashes}],
                        "upstream": {"commit": "unit-reference-not-a-real-benchmark"}}}
        return source_root, source, source_hash, reference

    def arguments(self, action, source_root, directory=None):
        return [action, "--data-dir", str(directory or self.root / "comparison"),
                "--source-root", str(source_root), "--pytest-image", "prepared-unit-image",
                "--output-dir", str(self.root / "artifacts"), "--report-path", str(self.root / "report.md")]

    def test_started_directory_cannot_auto_retry_before_reference_runner_or_meter(self):
        directory = self.root / "started"
        directory.mkdir()
        marker = directory / "model-comparison-started.json"
        marker.write_text('{"artifact":"original-attempt.json"}\n')
        original = marker.read_bytes()
        with patch.object(driver, "frozen_reference") as reference, \
                patch.object(driver.subprocess, "run") as image, \
                patch.object(driver, "ZhipuMeter") as meter, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                driver.main(self.arguments("run", self.root / "sources", directory))
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(marker.read_bytes(), original)
        reference.assert_not_called()
        image.assert_not_called()
        meter.assert_not_called()

    def test_dry_run_keeps_reference_cohort_and_never_constructs_paid_provider(self):
        source_root, _, source_hash, reference = self.fixture()
        runner = InspectionRunner(source_hash)
        original_reference = copy.deepcopy(reference)

        def service(settings, **kwargs):
            return RunService(settings, runner=runner, **kwargs)

        with patch.object(driver, "frozen_reference", return_value=reference), \
                patch.object(driver.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="sha256:" + "a" * 64 + "\n")), \
                patch.object(driver, "RunService", side_effect=service), \
                patch("patchtrail.api.load_local_env"), \
                patch.object(driver, "ZhipuMeter") as meter, \
                patch.object(driver, "MeteredCompatibleAgent") as paid_agent, \
                patch.object(OpenAICompatibleProvider, "complete") as complete, \
                patch.dict(os.environ, {"ZHIPU_API_KEY": ""}), redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main(self.arguments("dry-run", source_root)), 0)
        meter.assert_not_called()
        paid_agent.assert_not_called()
        complete.assert_not_called()
        self.assertEqual(reference, original_reference)
        self.assertEqual(runner.calls, 1)
        paths = list((self.root / "artifacts").glob("benchmark-quixbugs-zhipu-*.json"))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record["state"], "dry_run_only")
        self.assertEqual(len(record["cases"]), 40)
        self.assertEqual(record["frozen_eligible_names"], ["fixture-00"])
        self.assertEqual(record["summary"]["agent"]["eligible_tasks"], 1)
        self.assertEqual(record["summary"]["agent"]["attempted_tasks"], 0)
        self.assertEqual(record["summary"]["agent"]["not_run_tasks"], 1)
        self.assertGreater(record["dry_run"]["estimated_yuan"], 0)
        self.assertNotIn("cost", record)
        self.assertFalse((self.root / "comparison/model-comparison-started.json").exists())

    def test_frozen_source_drift_aborts_before_screening_or_paid_dispatch(self):
        source_root, source, source_hash, reference = self.fixture()
        (source / "app.py").write_text("# Drift from frozen unit reference\n")
        runner = InspectionRunner(source_hash)

        def service(settings, **kwargs):
            return RunService(settings, runner=runner, **kwargs)

        with patch.object(driver, "frozen_reference", return_value=reference), \
                patch.object(driver.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="a" * 64)), \
                patch.object(driver, "RunService", side_effect=service), \
                patch("patchtrail.api.load_local_env"), \
                patch.object(driver, "ZhipuMeter") as meter, \
                patch.object(driver, "MeteredCompatibleAgent") as paid_agent:
            with self.assertRaisesRegex(RuntimeError, "frozen P1"):
                driver.main(self.arguments("dry-run", source_root))
        self.assertEqual(runner.calls, 0)
        meter.assert_not_called()
        paid_agent.assert_not_called()
        record = json.loads(next((self.root / "artifacts").glob("benchmark-quixbugs-zhipu-*.json")).read_text())
        self.assertEqual(record["state"], "aborted")
        self.assertEqual(len(record["cases"]), 40)
        self.assertFalse((self.root / "comparison/model-comparison-started.json").exists())

    def test_actual_reference_is_byte_pinned_and_drift_stops_before_image_probe(self):
        original = ROOT / "artifacts" / driver.BASELINE_NAME
        self.assertEqual(len(driver.frozen_reference(original)["cases"]), 40)
        self.assertEqual(sum(case["eligible"] for case in driver.frozen_reference(original)["cases"]), 34)
        changed = self.root / "changed-reference.json"
        changed.write_bytes(original.read_bytes() + b"\n")
        with patch.object(driver.subprocess, "run") as image, patch.object(driver, "ZhipuMeter") as meter:
            with self.assertRaisesRegex(ValueError, "exact retained P1"):
                driver.main(self.arguments("run", self.root / "sources") + ["--reference", str(changed)])
        image.assert_not_called()
        meter.assert_not_called()

    def test_metered_agent_dispatches_exact_product_payload_once_then_reuses_parser(self):
        settings = Settings(data_dir=self.root / "runtime", provider_kind="openai-compatible",
            api_key="", compatible_api_key="unit-zhipu-private", model="glm-4.7",
            base_url=ENDPOINT.removesuffix("/chat/completions"), model_options={"thinking": {"type": "disabled"}})
        task = get_task("quantity-shortfall")
        messages = initial_messages(task)
        tool = {"id": "call-comparison-unit", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": task.demo_path})}}
        captured = []

        def handler(request):
            self.assertEqual(str(request.url), ENDPOINT)
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "comparison-unit-response", "model": "glm-4.7",
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [tool]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                          "prompt_tokens_details": {"cached_tokens": 10}}})

        meter = ZhipuMeter(self.root / "calls.json", settings.compatible_api_key,
                           transport=httpx.MockTransport(handler))
        provider = driver.MeteredCompatibleAgent(settings, meter)
        expected = OpenAICompatibleProvider(replace(settings, api_key=settings.compatible_api_key),
                    request_options=settings.model_options).build_payload(messages)
        message, usage = provider.complete(messages, task, 1, 5)
        self.assertEqual(captured, [expected])
        self.assertEqual(captured[0]["tools"], TOOL_SCHEMAS)
        self.assertEqual(message["tool_calls"], [tool])
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(provider.last_completion_metadata["response_id"], "comparison-unit-response")
        self.assertEqual(len(meter.calls), 1)
        self.assertEqual(meter.calls[0]["context"]["method"], "agent")
        self.assertNotIn(settings.compatible_api_key, (self.root / "calls.json").read_text())

    def test_wrong_metered_product_endpoint_is_rejected_before_billable_send(self):
        meter = Mock()
        settings = Settings(data_dir=self.root / "runtime", provider_kind="openai-compatible",
            compatible_api_key="unit-unused-private", model="glm-4.7",
            base_url="https://example.invalid/v1", model_options={"thinking": {"type": "disabled"}})
        with self.assertRaises(ProviderError):
            provider = driver.MeteredCompatibleAgent(settings, meter)
            provider.complete([], get_task("quantity-shortfall"), 1, 5)
        meter.send.assert_not_called()

    def test_report_adds_one_column_for_all_forty_tasks_and_preserves_p1_statistics(self):
        original_reference = ROOT / "artifacts" / driver.BASELINE_NAME
        original_bytes = original_reference.read_bytes()
        reference = json.loads(original_bytes)
        cases = [{"name": case["name"], "eligible": case["eligible"],
                  "source_hash": case["source_hash"]} for case in reference["cases"]]
        eligible = [case for case in cases if case["eligible"]]
        for case, status in zip(eligible[:2], ("succeeded", "failed")):
            case["agent"] = {"status": status, "model_calls": 1, "usage": {"total_tokens": 120},
                "usage_complete": True, "unknown_calls": 0, "tariff_yuan": 0.001,
                "elapsed_seconds": 1, "failure_category": None if status == "succeeded" else "budget_exhausted"}
        record = {"state": "stopped", "cases": cases, "summary": summarize({"cases": cases}),
            "baseline_reference": {"artifact": driver.BASELINE_NAME},
            "calls_artifact": "unit-comparison-calls.json", "cap_yuan": 30}
        report = self.root / "report.md"
        report.write_text((ROOT / "docs/BENCHMARK_QUIXBUGS.md").read_text())
        original_metrics = [line for line in report.read_text().splitlines()
                            if line.startswith(("| 单次基线 |", "| PatchTrail Agent |"))]
        driver.update_report(record, report, "unit-comparison.json")
        first = report.read_text()
        driver.update_report(record, report, "unit-comparison.json")
        self.assertEqual(report.read_text(), first)
        self.assertEqual(first.count("<!-- ZHIPU_COMPARISON_START -->"), 1)
        self.assertEqual(first.count("| 题目 |"), 1)
        self.assertEqual([line for line in first.splitlines()
                          if line.startswith(("| 单次基线 |", "| PatchTrail Agent |"))], original_metrics)
        task_names = {case["name"] for case in cases}
        table_rows = [line for line in first.splitlines() if line.startswith("| ")
                      and line.split("|")[1].strip() in task_names]
        self.assertEqual(len(table_rows), 40)
        self.assertTrue(all(len(line.strip().strip("|").split("|")) == 5 for line in table_rows))
        self.assertEqual(sum("未运行（沿用 P1 排除）" in line for line in table_rows), 6)
        self.assertIn("budget_exhausted", first)
        self.assertIn("1/34", first)
        self.assertIn("2/32", first)
        eligible[1]["agent"].update(status="succeeded", failure_category=None)
        record["summary"] = summarize(record)
        driver.update_report(record, report, "unit-comparison.json")
        changed_row = next(line for line in report.read_text().splitlines()
                           if line.startswith("| " + eligible[1]["name"] + " |"))
        self.assertEqual(changed_row.strip().strip("|").split("|")[-1].strip(), "通过")
        self.assertEqual(original_reference.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
