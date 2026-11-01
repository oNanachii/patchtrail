"""Fixed in-container completion recorder for the two allowed test frameworks.

The controller passes this source as a fixed ``python -c`` program. A framed
completion record is emitted only after normal framework return. This prevents
the observed stdout-summary + os._exit shortcut; it is not an authenticated
attestation against arbitrary hostile code in the same Python interpreter.
"""
import json
import os
import sys


def _flush_output():
    sys.stdout.flush()
    sys.stderr.flush()


def _pytest(arguments):
    import pytest

    class Recorder:
        def __init__(self):
            self.collected = 0
            self.nodes = set()
            self.counts = {name: 0 for name in ("passed", "failed", "skipped", "xfailed", "xpassed", "errors")}
            self.finished = False
            self.exit_status = None

        def pytest_collection_finish(self, session):
            self.collected = len(session.items)

        def pytest_collectreport(self, report):
            if report.failed:
                self.counts["errors"] += 1

        def pytest_runtest_logreport(self, report):
            self.nodes.add(report.nodeid)
            if report.when == "call":
                if report.passed:
                    self.counts["xpassed" if hasattr(report, "wasxfail") else "passed"] += 1
                elif report.failed:
                    self.counts["failed"] += 1
                elif report.skipped:
                    self.counts["xfailed" if hasattr(report, "wasxfail") else "skipped"] += 1
            elif report.failed:
                self.counts["errors"] += 1
            elif report.skipped:
                self.counts["skipped"] += 1

        def pytest_sessionfinish(self, session, exitstatus):
            self.finished = True
            self.exit_status = int(exitstatus)

    recorder = Recorder()
    code = int(pytest.main(arguments, plugins=[recorder]))
    counts = recorder.counts
    tests = sum(counts[name] for name in ("passed", "failed", "skipped", "xfailed", "xpassed"))
    complete = (recorder.finished and recorder.exit_status == code and code in (0, 1)
        and counts["errors"] == 0 and counts["passed"] + counts["failed"] > 0
        and tests == recorder.collected == len(recorder.nodes))
    return code, {"framework": "pytest", "tests": tests, "valid": complete,
        "passed": code == 0 and counts["passed"] > 0 and counts["failed"] == 0,
        "failed": counts["failed"], "skipped": counts["skipped"] + counts["xfailed"],
        "errors": counts["errors"], "framework_returned": True,
        "session_finished": recorder.finished, "exit_code": code,
        "collected": recorder.collected, "reported_nodes": len(recorder.nodes)}


def _unittest(arguments):
    import unittest

    class RecorderResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = set()
            self.stop_count = 0
            self.top_skipped = set()

        def startTest(self, test):
            self.started.add(test.id())
            super().startTest(test)

        def stopTest(self, test):
            self.stop_count += 1
            super().stopTest(test)

        def addSkip(self, test, reason):
            if test.id() in self.started:
                self.top_skipped.add(test.id())
            super().addSkip(test, reason)

        def addExpectedFailure(self, test, err):
            self.top_skipped.add(test.id())
            super().addExpectedFailure(test, err)

    class RecorderRunner(unittest.TextTestRunner):
        resultclass = RecorderResult

    program = unittest.main(module=None, argv=["unittest", *arguments],
                            testRunner=RecorderRunner, exit=False)
    result = program.result
    code = 0 if result.wasSuccessful() else 1
    failed = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    complete = (result.testsRun > 0 and result.stop_count == result.testsRun
        and len(result.top_skipped) < result.testsRun)
    return code, {"framework": "unittest", "tests": result.testsRun, "valid": complete,
        "passed": result.wasSuccessful(), "failed": failed,
        "skipped": len(result.skipped) + len(result.expectedFailures),
        "errors": len(result.errors), "framework_returned": True,
        "session_finished": True, "exit_code": code,
        "started": len(result.started), "stopped": result.stop_count,
        "top_skipped": len(result.top_skipped)}


def main():
    profile, marker, *command = sys.argv[1:]
    if command[:5] != ["python", "-I", "-B", "-m", profile]:
        raise ValueError("Launcher requires the validated fixed profile command")
    arguments = command[5:]
    # OCI CLIs may multiplex stdout/stderr out of order. Unittest reports to
    # stderr while the footer uses stdout; join their file descriptors before
    # any framework output so normal completion remains the final record.
    _flush_output()
    os.dup2(1, 2)
    # Task code sees the framework argv rather than the launcher's own options.
    # The marker is still observable through process metadata/introspection;
    # this normalization does not make the completion record authenticated.
    sys.argv = [profile, *arguments]
    if profile == "pytest":
        code, record = _pytest(arguments)
    elif profile == "unittest":
        code, record = _unittest(arguments)
    else:
        raise ValueError("Unsupported fixed test profile")
    record["schema_version"] = 1
    serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
    # Framework writers can leave Python-level terminal output buffered even
    # after their normal return. Flush it before the unbuffered footer syscall.
    _flush_output()
    os.write(1, ("\n" + marker + serialized + "\n").encode("utf-8"))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
