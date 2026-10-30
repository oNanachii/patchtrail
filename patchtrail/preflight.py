"""Fixed-container baseline checks and short-lived, source-bound receipts."""
import hashlib
import json
import tempfile
import time
import uuid
from pathlib import Path

from .sandbox import SandboxUnavailable
from .workspace import prepare_workspace, tree_hash


def binding(service, task):
    return hashlib.sha256(json.dumps({"source_hash": tree_hash(task.source_dir),
        "command": task.check_command, "image": service.settings.pytest_image
        if getattr(task, "check_profile", "unittest") == "pytest" else service.settings.container_image,
        "binary": service.settings.container_binary, "timeout": getattr(service.runner, "timeout", None)}, sort_keys=True).encode()).hexdigest()


def baseline(service, task):
    profile = getattr(task, "check_profile", "unittest")
    result = {"task_id": task.id, "source_hash": tree_hash(task.source_dir), "check_profile": profile,
              "binding": binding(service, task), "expires_at": int(time.time() + 900)}
    try:
        runner = service._runner_for(task)
        if not runner.available():
            raise SandboxUnavailable()
        with tempfile.TemporaryDirectory(prefix="preflight-", dir=service.settings.data_dir) as directory:
            workspace = prepare_workspace(Path(directory), uuid.uuid4().hex, task)
            check = runner.run(workspace, task.check_command, uuid.uuid4().hex)
        status = ("timed_out" if check.timed_out else "unconfirmed" if not check.complete
                  else "already_passed" if check.passed else "ready")
        if "ModuleNotFoundError:" in check.output or "ImportError:" in check.output:
            status = "environment_missing"
        result["check"] = check.to_dict()
    except SandboxUnavailable:
        status = "environment_missing"
        result["check"] = {"error": "Fixed container test environment is unavailable; no host fallback"}
    result.update(status=status, eligible=status == "ready", preflight_id=uuid.uuid4().hex)
    return result
