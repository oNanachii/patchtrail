"""Fail-closed Docker/Podman checks with bounded output and explicit limits."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import time

from .check_profiles import parse_check_command
from .workspace import RUN_ID, WorkspaceError, tree_hash


OUTPUT_LIMIT = 64 * 1024
COMPLETION_LIMIT = 16 * 1024
_LAUNCHER_SOURCE = Path(__file__).with_name("test_launcher.py").read_text(encoding="utf-8")


def _extract_completion(output: str, marker: str) -> tuple[str, dict]:
    """Separate the normal-return recorder footer from human framework output.

    The marker avoids accidental collisions; it is visible to the container
    process and is not a secret or an authenticated attestation.
    """
    lines = output.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith(marker)]
    if not matches:
        return output, {"status": "missing"}
    if len(matches) != 1:
        return output, {"status": "ambiguous"}
    index = matches[0]
    payload = lines[index][len(marker):].strip()
    if any(line.strip() for line in lines[index + 1:]) or len(payload.encode()) > COMPLETION_LIMIT:
        return output, {"status": "invalid"}
    try:
        record = json.loads(payload)
    except (ValueError, RecursionError):
        return output, {"status": "invalid"}
    if not isinstance(record, dict):
        return output, {"status": "invalid"}
    human = "".join(lines[:index] + lines[index + 1:]).rstrip() + "\n"
    return human, {**record, "status": "complete"}


def _unittest_subtest_skips(lines: list[str]) -> int:
    """Count verbose nested skips separately from top-level skipped methods.

    unittest's ``Ran N tests`` counts methods, while ``skipped=`` also counts
    individual subTests. A parent can therefore run successfully with more
    nested skips than N. Only attribute a nested line to a previously observed
    top-level method; summary-only ambiguous output stays conservative.
    """
    observed = set()
    pending_subtest = False
    skipped = 0
    identity = re.compile(r"^(\S+ \([^)]+\))(?: .*)?$")
    for line in lines:
        top = identity.fullmatch(line)
        if top:
            observed.add(top.group(1))
            pending_subtest = False
        nested = identity.fullmatch(line[2:]) if line.startswith("  ") else None
        if nested and nested.group(1) in observed:
            pending_subtest = True
        if pending_subtest:
            tail = re.search(r" \.\.\. (ok|FAIL|ERROR|skipped(?: .*)?|expected failure|unexpected success)$", line)
            if tail:
                skipped += tail.group(1).startswith("skipped")
                pending_subtest = False
    return skipped


class SandboxUnavailable(RuntimeError):
    """The configured container CLI or daemon is unavailable; no host fallback exists."""


@dataclass(frozen=True)
class CheckResult:
    exit_code: int | None
    output: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    runner: str = "docker"
    check_profile: str = ""
    completion_report: dict | None = None

    def _summary(self) -> dict:
        # Human output is cross-checked with actual-run completion evidence.
        # Neither mechanism independently attests arbitrary hostile Python in
        # the same interpreter as the test framework.
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", self.output)
        lines = text.rstrip().splitlines()
        last = lines[-1].strip() if lines else ""
        if self.check_profile in {"", "unittest"}:
            runs = re.findall(r"(?m)^Ran ([0-9]{1,6}) tests? in [^\n]+$", text)
            terminal = re.fullmatch(r"(OK|FAILED)(?: \(([^\n]+)\))?", last)
            if runs and terminal:
                count = int(runs[-1])
                counts = {}
                detail = terminal.group(2)
                if detail:
                    for part in detail.split(", "):
                        match = re.fullmatch(r"(failures|errors|skipped|expected failures|unexpected successes)=([0-9]{1,6})", part)
                        if not match or match.group(1) in counts:
                            return {"framework": "unittest", "tests": count, "valid": False}
                        counts[match.group(1)] = int(match.group(2))
                failed = counts.get("failures", 0) + counts.get("errors", 0) + counts.get("unexpected successes", 0)
                skipped = counts.get("skipped", 0) + counts.get("expected failures", 0)
                nested_skipped = _unittest_subtest_skips(lines)
                top_skipped = skipped - nested_skipped
                # Failure/error entries include subTests and lifecycle phases,
                # so their count is not bounded by the number of test methods.
                valid = (count > 0 and 0 <= nested_skipped <= counts.get("skipped", 0)
                         and 0 <= top_skipped < count
                         and ((terminal.group(1) == "OK" and self.exit_code == 0 and failed == 0)
                              or (terminal.group(1) == "FAILED" and self.exit_code == 1 and failed > 0))
                         and "unittest.loader._FailedTest" not in text)
                return {"framework": "unittest", "tests": count, "valid": valid,
                        "passed": terminal.group(1) == "OK", "skipped": skipped,
                        "failed": failed}
        if self.check_profile in {"", "pytest"}:
            terminal = re.fullmatch(r"=+ (.+) in [0-9]+(?:\.[0-9]+)?(?:s| seconds)(?: \([0-9:]+\))? =+", last)
            if terminal:
                counts = {}
                for part in terminal.group(1).split(", "):
                    match = re.fullmatch(r"([0-9]{1,6}) (passed|failed|skipped|xfailed|xpassed|error|errors|deselected|warning|warnings)", part)
                    if not match:
                        return {"framework": "pytest", "tests": 0, "valid": False}
                    key = {"errors": "error", "warnings": "warning"}.get(match.group(2), match.group(2))
                    if key in counts:
                        return {"framework": "pytest", "tests": 0, "valid": False}
                    counts[key] = int(match.group(1))
                count = sum(counts.get(key, 0) for key in ("passed", "failed", "skipped", "xfailed", "xpassed"))
                executed = counts.get("passed", 0) + counts.get("failed", 0)
                valid = (executed > 0 and counts.get("error", 0) == 0
                         and ((self.exit_code == 0 and counts.get("failed", 0) == 0)
                              or (self.exit_code == 1 and counts.get("failed", 0) > 0)))
                collected = re.findall(r"(?m)^(?:collecting \.\.\. )?collected ([0-9]{1,6}) items?(?: / ([^\n]+))?$", text)
                if collected:
                    total, selection = collected[-1]
                    selected = re.search(r"(?:^| / )([0-9]{1,6}) selected(?:$| / )", selection)
                    expected = int(selected.group(1)) if selected else int(total) - counts.get("deselected", 0)
                    valid = valid and expected == count
                return {"framework": "pytest", "tests": count, "valid": valid,
                        "passed": counts.get("passed", 0) > 0 and counts.get("failed", 0) == 0,
                        "skipped": counts.get("skipped", 0) + counts.get("xfailed", 0),
                        "failed": counts.get("failed", 0)}
        return {"framework": self.check_profile or "unknown", "tests": 0, "valid": False}

    @property
    def tests_run(self) -> int:
        return self._summary()["tests"]

    @property
    def framework(self) -> str:
        return self._summary()["framework"]

    @property
    def complete(self) -> bool:
        summary = self._summary()
        if self.timed_out or self.truncated or not summary["valid"]:
            return False
        if self.completion_report is None:
            return True  # Legacy captures and explicitly injected unit-test runners only.
        report = self.completion_report
        integer_fields = ("tests", "failed", "skipped", "exit_code", "schema_version")
        if (not isinstance(report, dict) or report.get("status") != "complete"
                or any(type(report.get(key)) is not int for key in integer_fields)
                or report.get("schema_version") != 1 or report.get("framework_returned") is not True
                or report.get("session_finished") is not True or report.get("valid") is not True
                or report.get("exit_code") != self.exit_code
                or report.get("framework") != summary["framework"]
                or report.get("passed") is not summary.get("passed")):
            return False
        return all(report[key] == summary[key] for key in ("tests", "failed", "skipped"))

    @property
    def passed(self) -> bool:
        return self.complete and self.exit_code == 0 and bool(self._summary()["passed"])

    @property
    def test_outcomes(self) -> list[dict]:
        outcomes = []
        framework = self.framework
        for line in self.output.splitlines():
            match = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)", line)
            if match and framework == "pytest":
                state = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error",
                         "SKIPPED": "skipped", "XFAIL": "xfailed", "XPASS": "xpassed"}[match.group(2)]
                outcomes.append({"name": match.group(1), "state": state})
            elif framework == "unittest":
                match = re.match(r"^(.+ \([^)]+\)) \.\.\. (ok|FAIL|ERROR|skipped(?: .*)?|expected failure|unexpected success)$", line)
                if match:
                    state = {"ok": "passed", "FAIL": "failed", "ERROR": "error",
                             "expected failure": "xfailed", "unexpected success": "xpassed"}.get(match.group(2), "skipped")
                    outcomes.append({"name": match.group(1), "state": state})
            if len(outcomes) >= 1000:
                break
        return outcomes

    def to_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed, "tests_run": self.tests_run,
                "complete": self.complete, "framework": self.framework,
                "test_outcomes": self.test_outcomes}


class DockerRunner:
    """Use a compatible OCI CLI; source and tests are mounted read-only."""

    def __init__(self, binary: str = "docker", image: str = "python:3.12-slim", timeout: int = 20):
        if not isinstance(binary, str) or not binary or "\x00" in binary or "\n" in binary:
            raise ValueError("A container CLI executable is required")
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_@-]{0,255}", image):
            raise ValueError("Invalid container image reference")
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError("Container timeout must be between 1 and 120 seconds")
        self.binary = binary
        self.image = image
        self.timeout = timeout
        self.runner = Path(binary).name

    @staticmethod
    def _environment() -> dict:
        # Container environment is separately and explicitly defined in argv below.
        allowed = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY",
                   "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR", "CONTAINER_HOST", "CONTAINER_CONNECTION")
        result = {key: os.environ[key] for key in allowed if key in os.environ}
        result.setdefault("PATH", os.defpath)
        return result

    def available(self) -> bool:
        try:
            # No Docker-specific formatting templates: this works with rootless Podman too.
            result = subprocess.run([self.binary, "info"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=min(3, self.timeout), env=self._environment(), check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _cleanup(self, name: str) -> None:
        try:
            subprocess.run([self.binary, "rm", "--force", name], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3, env=self._environment(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            # The unique name is retained in the CLI invocation for operational diagnosis.
            pass

    @staticmethod
    def _kill_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    def run(self, workspace: Path, command: tuple[str, ...], run_id: str) -> CheckResult:
        profile, _ = parse_check_command(command)
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run ID")
        original = Path(workspace)
        if original.is_symlink():
            raise WorkspaceError("Container workspace cannot be a symlink")
        workspace = original.resolve(strict=True)
        if "," in str(workspace) or "\n" in str(workspace):
            raise WorkspaceError("Workspace path cannot be encoded as a safe OCI mount")
        tree_hash(workspace)
        started = time.monotonic()
        if not self.available():
            raise SandboxUnavailable("Container runner is unavailable; host execution is disabled")
        remaining = self.timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise SandboxUnavailable("Container availability probe exhausted the execution budget")
        name = "patchtrail-" + secrets.token_hex(12)
        marker = "PATCHTRAIL_COMPLETION_" + secrets.token_hex(24) + ":"
        # The caller still supplies only a validated fixed profile command.
        # An owned launcher records normal framework return before success is
        # accepted; arbitrary stdout and a zero process exit are insufficient.
        launch = ("python", "-I", "-B", "-c", _LAUNCHER_SOURCE, profile, marker, *command)
        argv = [self.binary, "run", "--name", name, "--rm", "--pull=never",
                "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--cpus", "0.5", "--memory", "128m",
                "--ulimit", "nofile=128:128", "--ulimit", "fsize=1048576:1048576",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
                "--mount", f"type=bind,source={workspace},target=/workspace,readonly",
                "--workdir", "/workspace", "--env", "HOME=/tmp",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1",
                "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "PYTEST_ADDOPTS=",
                self.image, *launch]
        process = None
        output = bytearray()
        truncated = False
        timed_out = False
        selector = selectors.DefaultSelector()
        try:
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, env=self._environment(),
                                           start_new_session=True)
            except OSError as exc:
                raise SandboxUnavailable("Could not launch the configured container CLI") from exc
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = started + self.timeout
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._cleanup(name)
                    self._kill_process(process)
                    break
                events = selector.select(timeout=min(0.1, remaining))
                for key, _ in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    capacity = OUTPUT_LIMIT - len(output)
                    output.extend(chunk[:capacity])
                    if len(chunk) > capacity:
                        truncated = True
            if process.poll() is None:
                process.wait(timeout=max(0.05, deadline - time.monotonic()))
            human_output, completion = _extract_completion(output.decode("utf-8", errors="replace"), marker)
            return CheckResult(process.returncode, human_output,
                               int((time.monotonic() - started) * 1000), timed_out,
                               truncated, runner=self.runner, check_profile=profile,
                               completion_report=completion)
        except BaseException:
            self._cleanup(name)
            if process is not None:
                self._kill_process(process)
            raise
        finally:
            selector.close()
            if process is not None and process.stdout is not None:
                process.stdout.close()
            self._cleanup(name)
