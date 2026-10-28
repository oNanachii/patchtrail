"""OCI CLI simulations; these tests do not claim an actual Docker/Podman run."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from patchtrail.sandbox import CheckResult, DockerRunner, OUTPUT_LIMIT, SandboxUnavailable
from patchtrail.tasks import CHECK_COMMAND, get_task
from patchtrail.workspace import prepare_workspace


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="patchtrail-oci-cli-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = prepare_workspace(self.base / "workspaces", "run", get_task("quantity-shortfall"))
        self.log = self.base / "cli-calls.jsonl"

    def fake_cli(self, run_body, *, info_status=0):
        path = self.base / "fake-podman"
        script = f'''#!{sys.executable}
import json, os, sys, time
with open({str(self.log)!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "info":
    raise SystemExit({info_status})
if sys.argv[1] == "rm":
    raise SystemExit(0)
if sys.argv[1] == "run":
{run_body}
'''
        path.write_text(script)
        path.chmod(0o755)
        return str(path)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_success_requires_executed_tests_not_just_zero_exit(self):
        self.assertFalse(CheckResult(0, "OK\n", 1, False, False).passed)
        self.assertFalse(CheckResult(0, "Ran 0 tests in 0.001s\n\nOK\n", 1, False, False).passed)
        good = "Ran 4 tests in 0.001s\n\nOK\n"
        self.assertTrue(CheckResult(0, good, 1, False, False).passed)
        self.assertFalse(CheckResult(0, good, 1, True, False).passed)
        self.assertFalse(CheckResult(0, good, 1, False, True).passed)
        self.assertFalse(CheckResult(1, good, 1, False, False).passed)
        self.assertFalse(CheckResult(0, "Ran " + "9" * 10000 + " tests in 0s\n\nOK\n", 1, False, False).passed)
        self.assertEqual(CheckResult(0, good, 1, False, False).to_dict()["tests_run"], 4)

    def test_restricted_oci_arguments_and_clean_environment(self):
        binary = self.fake_cli('    print(os.environ.get("DEEPSEEK_API_KEY", "NO_MODEL_KEY"))\n    print("Ran 4 tests in 0.001s\\n\\nOK")')
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "UNIT_TEST_SECRET_MUST_NOT_PASS"}):
            result = DockerRunner(binary=binary).run(self.workspace, CHECK_COMMAND, "run")
        self.assertTrue(result.passed)
        self.assertEqual(result.runner, "fake-podman")
        self.assertIn("NO_MODEL_KEY", result.output)
        self.assertNotIn("UNIT_TEST_SECRET", result.output)
        calls = self.calls()
        argv = next(row for row in calls if row[0] == "run")
        for option in ["--pull=never", "--read-only", "--rm", "--pids-limit", "--cpus", "--memory", "--tmpfs"]:
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--user") + 1], "65534:65534")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges")
        self.assertIn("readonly", argv[argv.index("--mount") + 1])
        self.assertEqual(tuple(argv[-len(CHECK_COMMAND):]), CHECK_COMMAND)
        name = argv[argv.index("--name") + 1]
        self.assertTrue(name.startswith("patchtrail-"))
        self.assertIn(["rm", "--force", name], calls)

    def test_unavailable_runner_never_executes_a_host_check(self):
        binary = self.fake_cli('    raise SystemExit("must not run")', info_status=1)
        runner = DockerRunner(binary=binary)
        self.assertFalse(runner.available())
        with self.assertRaises(SandboxUnavailable):
            runner.run(self.workspace, CHECK_COMMAND, "run")
        self.assertTrue(all(call[0] == "info" for call in self.calls()))
        self.assertFalse(DockerRunner(binary=str(self.base / "missing-cli")).available())

    def test_arbitrary_command_is_rejected_before_cli_invocation(self):
        runner = DockerRunner(binary=str(self.base / "missing-cli"))
        with self.assertRaises(ValueError):
            runner.run(self.workspace, ("python", "-c", "print('not allowed')"), "run")
        self.assertFalse(self.log.exists())

    def test_output_is_drained_but_bounded_and_cannot_pass_when_truncated(self):
        binary = self.fake_cli('    print("x" * 100000)\n    print("Ran 4 tests in 0.001s\\n\\nOK")')
        # Probe success/failure is covered separately. A fresh Python shebang
        # fixture can exceed the probe's 3s before its own first log write on
        # this host; interpreter startup is outside this output-drain contract.
        # The run itself still starts the real fixture process below.
        with patch.object(DockerRunner, "available", return_value=True):
            result = DockerRunner(binary=binary).run(self.workspace, CHECK_COMMAND, "run")
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output.encode()), OUTPUT_LIMIT)
        self.assertFalse(result.passed)
        self.assertEqual(sum(call[0] == "run" for call in self.calls()), 1)

    def test_timeout_is_bounded_and_cleans_its_own_named_container(self):
        binary = self.fake_cli('    print("started", flush=True)\n    time.sleep(10)')
        started = time.monotonic()
        result = DockerRunner(binary=binary, timeout=1).run(self.workspace, CHECK_COMMAND, "run")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)
        self.assertIn("started", result.output)
        run = next(call for call in self.calls() if call[0] == "run")
        name = run[run.index("--name") + 1]
        self.assertTrue(all(call[-1] == name for call in self.calls() if call[0] == "rm"))


if __name__ == "__main__":
    unittest.main()
