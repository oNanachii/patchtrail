"""Test selection is data; arbitrary runner flags and commands are never accepted."""
import unittest

from patchtrail.check_profiles import build_check_command, parse_check_command
from patchtrail.sandbox import CheckResult
from patchtrail.tasks import CHECK_COMMAND


class ProfileTests(unittest.TestCase):
    def test_builtin_command_is_unchanged_and_round_trips(self):
        self.assertEqual(build_check_command("unittest", ["tests"]), CHECK_COMMAND)
        self.assertEqual(parse_check_command(CHECK_COMMAND), ("unittest", ("tests",)))

    def test_directory_file_and_pytest_nodes_round_trip(self):
        selections = [
            ("unittest", ["checks/unit"]),
            ("unittest", ["tests/test_case.py"]),
            ("unittest", ["test_case.py"]),
            ("pytest", ["tests"]),
            ("pytest", ["checks/test_api.py::TestClient::test_missing", "checks/test_data.py"]),
        ]
        for profile, paths in selections:
            with self.subTest(profile=profile, paths=paths):
                command = build_check_command(profile, paths)
                self.assertEqual(parse_check_command(command), (profile, tuple(paths)))

    def test_options_paths_shell_fragments_and_unbounded_selectors_rejected(self):
        invalid = ["/tests", "../tests", "tests/../source", "tests//unit", "./tests",
                   "tests/.secret", ".env", "-c", "tests;id", "tests\nnext", "tests\\case.py",
                   "tests/test_case.py::test_a[param]", "tests/test_case.py::test_a::bad::node",
                   "tests/test_case.py::", "tests.py --maxfail=1", "tests/test_case.py::$HOME"]
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(ValueError):
                build_check_command("pytest", [path])
        for profile, paths in (("shell", ["tests"]), ("pytest", []), ("pytest", ["tests"] * 9),
                               ("pytest", ["tests", "tests"]), ("pytest", [{}]),
                               ("unittest", ["tests", "more"]), ("unittest", ["other.py"])):
            with self.subTest(profile=profile, paths=paths), self.assertRaises(ValueError):
                build_check_command(profile, paths)

    def test_argv_cannot_add_flags_or_replace_fixed_settings(self):
        command = build_check_command("pytest", ["tests"])
        altered = [command + ("--pyargs",), command[:-1], ("python", "-c", "print(1)"),
                   tuple("--rootdir=/etc" if x == "--rootdir=/workspace" else x for x in command),
                   tuple("/workspace/pyproject.toml" if x == "/dev/null" else x for x in command),
                   list(command)]
        for value in altered:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_check_command(value)


class OutcomeTests(unittest.TestCase):
    def result(self, code, output, *, timed_out=False, truncated=False, profile=""):
        return CheckResult(code, output, 1, timed_out, truncated, "unit-test-no-container", profile)

    def test_pytest_success_failure_and_case_evidence(self):
        text = ("platform linux -- Python 3.12.0, pytest-9.0.3, pluggy-1.5.0\n"
                "rootdir: /workspace\nconfigfile: /dev/null\n"
                "collecting ... collected 3 items\n\n"
                "tests/test_x.py::test_one PASSED [ 33%]\n"
                "tests/test_x.py::test_two PASSED [ 66%]\n"
                "tests/test_x.py::test_three SKIPPED [100%]\n"
                "================ 2 passed, 1 skipped in 0.02s ================\n")
        result = self.result(0, text, profile="pytest")
        self.assertTrue(result.complete)
        self.assertTrue(result.passed)
        self.assertEqual(result.tests_run, 3)
        self.assertEqual(result.framework, "pytest")
        self.assertEqual([r["state"] for r in result.test_outcomes], ["passed", "passed", "skipped"])
        failed = self.result(1, "collected 3 items\n================ 1 failed, 2 passed in 0.02s ================\n")
        self.assertTrue(failed.complete)
        self.assertFalse(failed.passed)
        selected = self.result(0, "collected 3 items / 1 deselected / 2 selected\n"
                               "================ 2 passed, 1 deselected in 0.02s ================\n")
        self.assertTrue(selected.complete)
        self.assertTrue(selected.passed)

    def test_pytest_incomplete_environment_or_inconsistent_evidence(self):
        cases = [
            (0, "================ no tests ran in 0.01s ================\n"),
            (5, "================ no tests ran in 0.01s ================\n"),
            (0, "================ 2 skipped in 0.01s ================\n"),
            (0, "================ 2 xfailed in 0.01s ================\n"),
            (0, "collected 3 items\n================ 2 passed in 0.01s ================\n"),
            (0, "collecting ... collected 3 items\n================ 2 passed in 0.01s ================\n"),
            (1, "================ 1 error in 0.01s ================\n"),
            (1, "================ 1 failed, 1 error in 0.01s ================\n"),
            (0, "================ 1 failed in 0.01s ================\n"),
            (2, "================ 1 passed in 0.01s ================\n"),
            (0, "================ 1 passed in 0.01s ================\nnot finished\n"),
            (0, "================ 1 passed, 2 passed in 0.01s ================\n"),
            (0, "================ " + "9" * 1000 + " passed in 0.01s ================\n"),
        ]
        for code, text in cases:
            with self.subTest(code=code, output=text[:100]):
                result = self.result(code, text, profile="pytest")
                self.assertFalse(result.complete)
                self.assertFalse(result.passed)

    def test_profile_and_deadline_cannot_be_overridden_by_a_summary(self):
        pytest = "================ 2 passed in 0.01s ================\n"
        unittest_output = "Ran 2 tests in 0.01s\n\nOK\n"
        self.assertFalse(self.result(0, pytest, profile="unittest").complete)
        self.assertFalse(self.result(0, unittest_output, profile="pytest").complete)
        self.assertFalse(self.result(0, pytest, timed_out=True).complete)
        self.assertFalse(self.result(0, pytest, truncated=True).complete)

    def test_unittest_complete_failures_skips_and_loader_errors(self):
        failed = self.result(1, "Ran 4 tests in 0.01s\n\nFAILED (failures=1)\n")
        self.assertTrue(failed.complete)
        self.assertFalse(failed.passed)
        skipped = self.result(0, "Ran 4 tests in 0.01s\n\nOK (skipped=1)\n")
        self.assertTrue(skipped.complete)
        self.assertTrue(skipped.passed)
        self.assertFalse(self.result(0, "Ran 4 tests in 0.01s\n\nOK (skipped=4)\n").complete)
        self.assertFalse(self.result(1, "unittest.loader._FailedTest\nRan 1 test in 0s\n\nFAILED (errors=1)\n").complete)
        self.assertFalse(self.result(1, "Ran 2 tests in 0s\n\nFAILED (failures=3)\n").complete)


if __name__ == "__main__":
    unittest.main()
