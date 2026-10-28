"""Loopback design harness for frontend work: real API/service, labelled fake runner.

This is not a container integration and not a model run. The runner below never starts a
container and never imports or executes fixture code; it reads test names as text and returns
a canned unittest-shaped result so the browser can exercise every state. Live mode keeps the
real DeepSeek provider but with no API key, so live creation returns the real 503 path and no
model request can be made. /api/health gains a `design_harness` field that the UI shows as a
warning banner; production never sets it.
"""
import argparse
import json
from pathlib import Path
import re
import secrets
import sys
import time
from unittest.mock import patch

import uvicorn
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from patchtrail.api import create_app
from patchtrail.config import ROOT, Settings
from patchtrail.providers import ScriptedProvider
from patchtrail.sandbox import CheckResult, SandboxUnavailable
from patchtrail.service import RunService
from patchtrail.tasks import get_task

LABEL = "design-harness-fake"


class HarnessRunner:
    """Inspection-only fake. No subprocess, no container, no fixture imports."""

    def __init__(self, online, verify, check_seconds):
        self.online, self.verify, self.check_seconds = online, verify, check_seconds

    def available(self):
        return self.online

    def run(self, workspace, command, run_id):
        time.sleep(self.check_seconds)
        verifying = workspace.parent.name == "verification"
        if verifying and self.verify == "unavailable":
            raise SandboxUnavailable("design harness: runner offline during verification")
        names = []
        for test_file in sorted((workspace / "tests").glob("test_*.py")):
            names += [(test_file.stem, name) for name in
                      re.findall(r"(?m)^    def (test_\w+)\(", test_file.read_text(encoding="utf-8"))]
        changed = self._changed(workspace, run_id)
        passed = changed and not (verifying and self.verify == "fail")
        lines = ["[design harness] Fake runner: no container started, no fixture code executed."]
        for index, (module, name) in enumerate(names):
            status = "ok" if passed or index else "FAIL"
            lines.append(f"{name} (tests.{module}.Harness.{name}) ... {status}")
        if not passed:
            lines += ["", "=" * 70, f"FAIL: {names[0][1]} (tests.{names[0][0]}.Harness.{names[0][1]})",
                      "-" * 70, "AssertionError: canned design-harness failure (not a real assertion)"]
        lines += ["", "-" * 70, f"Ran {len(names)} tests in 0.000s", "",
                  "OK" if passed else "FAILED (failures=1)", ""]
        return CheckResult(0 if passed else 1, "\n".join(lines), int(self.check_seconds * 1000),
                           False, False, LABEL)

    @staticmethod
    def _changed(workspace, run_id):
        for task_dir in (ROOT / "patchtrail" / "fixtures").iterdir():
            task = get_task(task_dir.name)
            target = workspace / task.editable_files[0]
            if target.is_file():
                return target.read_text(encoding="utf-8") != (task.source_dir / task.editable_files[0]).read_text(encoding="utf-8")
        return False


class SlowScripted(ScriptedProvider):
    def __init__(self, seconds):
        self.seconds = seconds

    def complete(self, *args):
        time.sleep(self.seconds)
        return super().complete(*args)


def build(args):
    settings = Settings(data_dir=Path(args.data_dir), api_key="", admin_token=args.operator_token,
                        public_demo=not args.private)
    runner = HarnessRunner(args.runner == "online", args.verify, args.check_seconds)
    service = RunService(settings, runner=runner)
    service.demo_provider = SlowScripted(args.provider_seconds)
    with patch("patchtrail.api.load_local_env"):  # Never read the user's .env credentials.
        app = create_app(service)

    @app.middleware("http")
    async def label_health(request, call_next):
        response = await call_next(request)
        if request.url.path != "/api/health" or response.status_code != 200:
            return response
        body = b"".join([part async for part in response.body_iterator])
        data = json.loads(body)
        data["design_harness"] = ("Local design harness: fake runner (no container, no code executed); "
                                  "live mode has no model key. Results here are not evidence.")
        payload = json.dumps(data).encode()
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(payload, status_code=200, headers=headers, media_type="application/json")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8875)
    parser.add_argument("--data-dir", default=str(ROOT / "var" / "design-harness"))
    parser.add_argument("--operator-token", default="")
    parser.add_argument("--runner", choices=("online", "offline"), default="online")
    parser.add_argument("--verify", choices=("pass", "fail", "unavailable"), default="pass")
    parser.add_argument("--check-seconds", type=float, default=1.5)
    parser.add_argument("--provider-seconds", type=float, default=0.6)
    parser.add_argument("--private", action="store_true", help="disable the public demo")
    args = parser.parse_args()
    if not args.operator_token:
        args.operator_token = "harness-" + secrets.token_hex(12)
        print(f"Harness operator token (local test value): {args.operator_token}", flush=True)
    print(f"Design harness on http://127.0.0.1:{args.port} - fake runner, not evidence", flush=True)
    uvicorn.run(build(args), host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
