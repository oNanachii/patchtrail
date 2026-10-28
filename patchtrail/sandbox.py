"""Fail-closed Docker/Podman checks with bounded output and explicit limits."""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import time

from .tasks import CHECK_COMMAND
from .workspace import RUN_ID, WorkspaceError, tree_hash


OUTPUT_LIMIT = 64 * 1024


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

    @property
    def tests_run(self) -> int:
        matches = re.findall(r"(?m)^Ran ([1-9][0-9]{0,5}) tests? in [^\n]+$", self.output)
        return int(matches[-1]) if matches else 0

    @property
    def passed(self) -> bool:
        # This confirms a finite unittest run, not adversarial-proof Python correctness.
        return (self.exit_code == 0 and not self.timed_out and not self.truncated
                and self.tests_run > 0 and self.output.rstrip().splitlines()[-1:] == ["OK"])

    def to_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed, "tests_run": self.tests_run}


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
        if not isinstance(command, tuple) or command != CHECK_COMMAND:
            raise ValueError("Only the catalog's fixed unittest check command is allowed")
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
        argv = [self.binary, "run", "--name", name, "--rm", "--pull=never",
                "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--cpus", "0.5", "--memory", "128m",
                "--ulimit", "nofile=128:128", "--ulimit", "fsize=1048576:1048576",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
                "--mount", f"type=bind,source={workspace},target=/workspace,readonly",
                "--workdir", "/workspace", "--env", "HOME=/tmp",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1",
                self.image, *command]
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
            return CheckResult(process.returncode, output.decode("utf-8", errors="replace"),
                               int((time.monotonic() - started) * 1000), timed_out,
                               truncated, runner=self.runner)
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
