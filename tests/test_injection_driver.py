"""Guard evaluation dispatch and spending without executing task code or networking."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from patchtrail.check_profiles import parse_check_command
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from scripts.benchmark_meter import Meter
from scripts.injection_eval import main
from scripts.injection_eval_cases import prepare_cases


class FixtureByteRunner:
    """Compare saved fixture bytes only; this is explicitly not OCI evidence."""
    def __init__(self, cases):
        self.cases = cases
        self.calls = 0

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if parse_check_command(command) != ("pytest", ("tests/test_calc.py",)):
            raise AssertionError("Only the fixed pytest profile is expected")
        texts = "\n".join(path.read_text() for path in workspace.rglob("*") if path.is_file())
        case = next(item for item in self.cases if item["exposure_marker"] in texts)
        passed = (workspace / "src/calc.py").read_text() == case["oracle_replacements"][0]["content"]
        self.calls += 1
        return CheckResult(0 if passed else 1,
            "================ " + ("3 passed" if passed else "3 failed") + " in 0.01s ================\n",
            1, False, False, "explicit-fixture-byte-comparison-no-execution", "pytest")


class InjectionDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="patchtrail-injection-driver-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_started_directory_is_rejected_before_runner_or_model_preparation(self):
        directory = self.root / "already-started"
        directory.mkdir()
        marker = directory / "injection-eval-started.json"
        marker.write_text('{"artifact":"retain-existing-attempt.json"}\n')
        original = marker.read_bytes()
        with patch("scripts.injection_eval.subprocess.run") as runner, \
                patch("scripts.injection_eval.prepare_cases") as prepare, \
                patch("scripts.injection_eval.Meter") as meter, \
                patch("scripts.injection_eval.create_app") as app, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["run", "--data-dir", str(directory), "--pytest-image", "unused-image"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(marker.read_bytes(), original)
        runner.assert_not_called()
        prepare.assert_not_called()
        meter.assert_not_called()
        app.assert_not_called()

    def test_invalid_caps_stop_before_container_or_paid_dispatch(self):
        for cap in ("0", "-1", "31.01", "nan", "inf"):
            with self.subTest(cap=cap), patch("scripts.injection_eval.subprocess.run") as runner, \
                    patch("scripts.injection_eval.Meter") as meter, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["run", "--data-dir", str(self.root / "invalid-cap"),
                          "--pytest-image", "unused-image", "--cap-yuan", cap])
                self.assertEqual(error.exception.code, 2)
                runner.assert_not_called()
                meter.assert_not_called()
        self.assertFalse((self.root / "invalid-cap").exists())

    def test_larger_eval_cap_requires_explicit_maximum_and_benchmark_default_stays_eight(self):
        default = Meter(self.root / "benchmark.json", "CANARY-PT-FAKE-KEY")
        self.assertEqual(default.summary()["cap_yuan"], 8)
        with self.assertRaises(ValueError):
            Meter(self.root / "unauthorized.json", "CANARY-PT-FAKE-KEY", cap_yuan=31)
        authorized = Meter(self.root / "authorized.json", "CANARY-PT-FAKE-KEY",
                           cap_yuan=31, maximum_cap_yuan=31)
        self.assertEqual(authorized.summary()["cap_yuan"], 31)
        self.assertEqual(authorized.summary()["dispatched_calls"], 0)
        with self.assertRaises(ValueError):
            Meter(self.root / "above-limit.json", "CANARY-PT-FAKE-KEY",
                  cap_yuan=32, maximum_cap_yuan=32)

    def test_complete_dry_run_never_constructs_paid_meter_or_agent(self):
        directory = self.root / "dry-run"
        output = self.root / "artifacts"
        dataset = prepare_cases(directory, canary_dir=self.root / "outside-canary")
        runner = FixtureByteRunner(dataset["tasks"])

        def service(settings, **kwargs):
            return RunService(settings, runner=runner, **kwargs)

        image = SimpleNamespace(returncode=0, stdout="sha256:" + "a" * 64 + "\n")
        with patch("scripts.injection_eval.subprocess.run", return_value=image), \
                patch("scripts.injection_eval.prepare_cases", return_value=dataset), \
                patch("scripts.injection_eval.RunService", side_effect=service), \
                patch("patchtrail.api.load_local_env"), \
                patch("scripts.injection_eval.Meter") as meter, \
                patch("scripts.injection_eval.MeteredAgent") as model, \
                redirect_stdout(io.StringIO()):
            result = main(["dry-run", "--data-dir", str(directory), "--pytest-image", "fake-prepared-image",
                           "--output-dir", str(output), "--report-path", str(self.root / "report.md")])
        self.assertEqual(result, 0)
        meter.assert_not_called()
        model.assert_not_called()
        self.assertFalse((directory / "injection-eval-started.json").exists())
        artifacts = list(output.glob("injection-eval-*-dry-run.json"))
        self.assertEqual(len(artifacts), 1)
        record = json.loads(artifacts[0].read_text())
        self.assertEqual(record["state"], "dry_run_only")
        self.assertEqual(len(record["cases"]), 12)
        self.assertTrue(all(case["deterministic"]["expectation_passed"] for case in record["cases"]))
        self.assertNotIn("cost", record)
        self.assertGreaterEqual(runner.calls, 36)


if __name__ == "__main__":
    unittest.main()
