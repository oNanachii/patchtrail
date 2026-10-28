"""One bounded agent, explicit approval, and verifier-owned completion states."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from .config import Settings, runtime_code_hash
from .providers import DeepSeekProvider, ProviderError, ScriptedProvider, initial_messages
from .projects import ProjectCatalog
from .sandbox import DockerRunner, SandboxUnavailable
from .storage import Store, StoreConflict
from .tasks import get_task, list_tasks
from .workspace import (WorkspaceError, apply_proposal, build_proposal, build_patch_bundle, prepare_workspace,
                        read_file, search_code, tree_hash)

TERMINAL = {"succeeded", "failed", "cancelled"}


class ServiceError(RuntimeError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class BudgetExceeded(RuntimeError):
    pass


class CheckUnconfirmed(RuntimeError):
    pass


def public_run(row):
    result = copy.deepcopy(row)
    checkpoint = result.get("checkpoint") or {}
    result["checkpoint"] = {key: checkpoint.get(key) for key in
                            ("calls", "steps", "usage", "elapsed_ms", "last_tool") if key in checkpoint}
    result["checkpoint"]["unconfirmed_model_request"] = bool(checkpoint.get("pending_model_request"))
    if result.get("proposal"):
        result["proposal"].pop("content", None)
        for file in result["proposal"].get("files", []):
            file.pop("content", None)
    result["private"] = row["task_id"].startswith("project-")
    result["provider_label"] = ScriptedProvider.label if row["mode"] == "demo" else "DeepSeek live model"
    result["configured_model"] = checkpoint.get("configured_model")
    result["observed_models"] = sorted({m["reported_model"] for m in checkpoint.get("provider_metadata", [])
                                       if isinstance(m.get("reported_model"), str)})
    return result


class RunService:
    def __init__(self, settings: Settings, runner=None, provider=None, dispatch=True):
        self.settings = settings
        self.settings.data_dir = settings.data_dir.resolve()
        self.store = Store(settings.data_dir / "runs.sqlite3")
        self.catalog = ProjectCatalog(settings.data_dir / "projects")
        self.runner = runner or DockerRunner(settings.container_binary, settings.container_image)
        self.provider = provider or DeepSeekProvider(settings)
        self.demo_provider = ScriptedProvider()
        self.dispatch = dispatch
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="patchtrail")
        self.lock = threading.Lock()
        self.submitted = set()
        self.closed = False
        self.store.recover_active()
        if dispatch:
            for row in self.store.list_runs(100):
                if row["state"] == "queued":
                    self._submit(row["id"], self.diagnose)

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)

    def resolve_task(self, task_id):
        try:
            return get_task(task_id)
        except KeyError:
            try:
                return self.catalog.get_task(task_id)
            except WorkspaceError:
                raise ServiceError("project_unavailable", "Imported project snapshot validation failed", 503) from None

    def task_list(self, private=False):
        return list_tasks() + (self.catalog.list_tasks() if private else [])

    def _runner_for(self, task, remaining=None):
        if not isinstance(self.runner, DockerRunner):
            return self.runner  # Explicitly injected test runner only.
        image = self.settings.pytest_image if getattr(task, "check_profile", "unittest") == "pytest" else self.settings.container_image
        if not image:
            raise SandboxUnavailable("The fixed pytest image is not configured")
        if remaining is not None and remaining < 1:
            raise BudgetExceeded()
        timeout = min(self.runner.timeout, math.floor(remaining)) if remaining is not None else self.runner.timeout
        return DockerRunner(self.runner.binary, image, timeout=timeout)

    def _submit(self, run_id, operation):
        if not self.dispatch:
            return
        key = (run_id, operation.__name__)
        with self.lock:
            if key in self.submitted:
                return
            self.submitted.add(key)
        def execute():
            try:
                operation(run_id)
            finally:
                with self.lock:
                    self.submitted.discard(key)
        self.executor.submit(execute)

    def create(self, task_id, mode, request_key):
        try:
            task = self.resolve_task(task_id)
        except KeyError:
            raise ServiceError("unknown_task", "Unknown task", 404) from None
        if mode not in {"demo", "live"}:
            raise ServiceError("invalid_mode", "Mode must be demo or live")
        if getattr(task, "private", False) and mode != "live":
            raise ServiceError("private_task_requires_live", "Imported project tasks require an authenticated live run")
        previous = self.store.find_request(request_key)
        if previous:
            if (previous["task_id"], previous["mode"], previous["limits"]) != (task_id, mode, self.settings.limits):
                raise StoreConflict("Idempotency key was already used for a different request")
            return previous
        if mode == "live" and not self.settings.api_key:
            raise ServiceError("model_not_configured", "Configure DEEPSEEK_API_KEY locally before a live run", 503)
        try:
            runner = self._runner_for(task)
        except SandboxUnavailable:
            raise ServiceError("sandbox_unavailable", "The fixed test-profile container image is not configured", 503) from None
        if not runner.available():
            raise ServiceError("sandbox_unavailable", "Container runner is unavailable; host execution is disabled", 503)
        # Single-process queue by design. Serialize capacity check and creation.
        with self.lock:
            previous = self.store.find_request(request_key)
            if previous:
                if (previous["task_id"], previous["mode"], previous["limits"]) != (task_id, mode, self.settings.limits):
                    raise StoreConflict("Idempotency key was already used for a different request")
                return previous
            active = [r for r in self.store.list_runs(100) if r["state"] not in TERMINAL]
            if len(active) >= 6 or len(self.submitted) >= 6:
                raise ServiceError("queue_full", "The small preview queue is full", 429)
            row = self.store.create_run(task_id, mode, request_key, self.settings.limits)
        if row["created"]:
            self.store.append_event(row["id"], "run_created", {"mode": mode,
                "provider": self.demo_provider.label if mode == "demo" else self.settings.model,
                "runtime_code_hash": runtime_code_hash(),
                "task_version": tree_hash(task.source_dir),
                "project_id": getattr(task, "project_id", ""),
                "snapshot_id": getattr(task, "snapshot_id", ""),
                "check_profile": getattr(task, "check_profile", "unittest")})
        if row["state"] == "queued":
            self._submit(row["id"], self.diagnose)
        return self.store.get_run(row["id"])

    def _checkpoint(self, run_id, checkpoint):
        return self.store.transition(run_id, {"diagnosing"}, "diagnosing", {"checkpoint": checkpoint})

    def _fail(self, run_id, reason, message):
        row = self.store.get_run(run_id)
        if row["state"] in TERMINAL or row["state"] == "interrupted":
            return
        try:
            self.store.transition(run_id, {row["state"]}, "failed",
                                  {"stop_reason": reason, "result": {"error": message}}, row["version"])
        except StoreConflict:
            pass  # Cancellation or a concurrent state change won.

    def diagnose(self, run_id):
        start = time.monotonic()
        row = self.store.get_run(run_id)
        try:
            row = self.store.transition(run_id, {"queued"}, "diagnosing", expected_version=row["version"])
            task = self.resolve_task(row["task_id"])
            workspace = prepare_workspace(self.settings.data_dir / "workspaces", run_id, task)
            cp = row["checkpoint"] or {"messages": initial_messages(task), "calls": 0, "steps": 0,
                                      "seen": [], "usage": {}, "elapsed_ms": 0}
            cp.setdefault("configured_model", self.settings.model if row["mode"] == "live" else "scripted")
            cp.setdefault("provider_metadata", [])
            prior_elapsed = cp.get("elapsed_ms", 0)
            if cp.get("pending_model_request"):
                self.store.append_event(run_id, "explicit_resume_after_unknown_model_outcome",
                                        {"previous_request": cp["pending_model_request"]})
                cp.pop("pending_model_request", None)
            provider = self.demo_provider if row["mode"] == "demo" else self.provider
            while not self.closed:
                if self.store.get_run(run_id)["state"] != "diagnosing":
                    return
                elapsed = prior_elapsed + int((time.monotonic() - start) * 1000)
                remaining = self.settings.max_seconds - elapsed / 1000
                if remaining <= 0 or cp["calls"] >= self.settings.max_calls or cp["steps"] >= self.settings.max_steps:
                    self._fail(run_id, "budget_exhausted", "The configured execution budget was exhausted")
                    return
                if len(json.dumps(cp["messages"], ensure_ascii=False)) > self.settings.max_input_chars:
                    self._fail(run_id, "context_budget", "The input context limit was reached; no silent truncation")
                    return
                cp["calls"] += 1
                cp["pending_model_request"] = uuid.uuid4().hex
                cp["elapsed_ms"] = elapsed
                self._checkpoint(run_id, cp)
                self.store.append_event(run_id, "model_requested", {"call": cp["calls"], "provider": provider.label})
                message, usage = provider.complete(cp["messages"], task, cp["calls"], remaining)
                if self.store.get_run(run_id)["state"] != "diagnosing":
                    return
                cp.pop("pending_model_request", None)
                for key, value in usage.items():
                    cp["usage"][key] = cp["usage"].get(key, 0) + value
                if not isinstance(message, dict):
                    raise ProviderError("Invalid model message")
                calls = message.get("tool_calls") or []
                if (not isinstance(calls, list) or len(calls) > 8
                        or any(not isinstance(c, dict) or not isinstance(c.get("function"), dict) for c in calls)
                        or len({c.get("id") for c in calls}) != len(calls)):
                    raise ProviderError("Invalid tool-call batch")
                metadata = getattr(provider, "last_completion_metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata = {key: value[:256] for key, value in metadata.items()
                            if key in {"reported_model", "response_id", "finish_reason"} and isinstance(value, str)}
                cp["provider_metadata"].append({"call": cp["calls"], **metadata})
                content = message.get("content") or ""
                self.store.append_event(run_id, "model_response", {"call": cp["calls"],
                    "content": content[:2000], "content_truncated": len(content) > 2000,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "tool_names": [c["function"].get("name") for c in calls], **metadata})
                cp["messages"].append(message)
                cp["elapsed_ms"] = prior_elapsed + int((time.monotonic() - start) * 1000)
                self._checkpoint(run_id, cp)
                if not calls:
                    self._fail(run_id, "no_patch_proposal", "A model answer cannot complete a run without a verified proposal")
                    return
                proposal = None
                for call in calls:
                    remaining = self.settings.max_seconds - (prior_elapsed / 1000 + time.monotonic() - start)
                    if remaining <= 0:
                        raise BudgetExceeded()
                    call_id = call.get("id")
                    event_call_id = f"{cp['calls']}:{call_id}"
                    fn = call.get("function", {})
                    if not isinstance(call_id, str) or not call_id or call.get("type") != "function":
                        raise ProviderError("Invalid tool-call identity")
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", ""))
                        if not isinstance(args, dict):
                            raise ValueError("Tool arguments must be an object")
                        if proposal:
                            result = {"error": "not_executed_pending_approval"}
                        elif cp["steps"] >= self.settings.max_steps:
                            result = {"error": "tool_budget_exhausted"}
                        else:
                            signature = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
                            if signature in cp["seen"]:
                                raise ValueError("Repeated identical tool call; use another source or propose a patch")
                            cp["seen"].append(signature)
                            cp["steps"] += 1
                            self.store.append_event(run_id, "tool_requested", {"name": name, "arguments":
                                {k: (f"<{len(v)} characters>" if k == "content" and isinstance(v, str) else
                                     [{"path": f.get("path"), "content": f"<{len(f.get('content', ''))} characters>"} for f in v if isinstance(f, dict)]
                                     if k == "files" and isinstance(v, list) else v) for k, v in args.items()},
                                "provider_call_id": call_id, "model_call": cp["calls"]}, event_call_id)
                            result, proposal = self._tool(name, args, task, workspace, cp, run_id, remaining)
                        self.store.append_event(run_id, "tool_result", {"name": name, "result": result,
                            "provider_call_id": call_id, "model_call": cp["calls"]}, event_call_id)
                    except (ValueError, TypeError, KeyError) as exc:
                        result = {"error": str(exc)[:300]}
                        self.store.append_event(run_id, "tool_rejected", {"name": name, "error": result["error"],
                            "provider_call_id": call_id, "model_call": cp["calls"]}, event_call_id)
                    cp["last_tool"] = name
                    context_result = copy.deepcopy(result)
                    if isinstance(context_result.get("output"), str) and len(context_result["output"]) > 8000:
                        context_result["output"] = context_result["output"][:8000]
                        context_result["output_truncated_for_context"] = True
                    cp["messages"].append({"role": "tool", "tool_call_id": call_id,
                                           "content": json.dumps(context_result, ensure_ascii=False)})
                cp["elapsed_ms"] = prior_elapsed + int((time.monotonic() - start) * 1000)
                if cp["elapsed_ms"] >= self.settings.max_seconds * 1000:
                    raise BudgetExceeded()
                if proposal:
                    self.store.transition(run_id, {"diagnosing"}, "awaiting_approval",
                                          {"checkpoint": cp, "proposal": proposal})
                    return
                self._checkpoint(run_id, cp)
        except StoreConflict:
            return
        except SandboxUnavailable:
            self._fail(run_id, "sandbox_unavailable", "Container execution became unavailable; no host fallback")
        except BudgetExceeded:
            self._fail(run_id, "time_budget", "The execution time budget expired; no proposal was accepted")
        except CheckUnconfirmed:
            self._fail(run_id, "baseline_unconfirmed", "No complete baseline test run was confirmed; inspect the container check event")
        except ServiceError:
            self._fail(run_id, "runtime_error", "The imported project snapshot could not be validated")
        except ProviderError as exc:
            if not exc.uncertain:
                cp.pop("pending_model_request", None)
                try:
                    self._checkpoint(run_id, cp)
                except StoreConflict:
                    pass
            self._fail(run_id, "model_error", str(exc))
        except (WorkspaceError, OSError, ValueError, KeyError, TypeError):
            self._fail(run_id, "runtime_error", "The task or runtime contract failed; inspect the recorded events")

    def _tool(self, name, args, task, workspace, cp, run_id, remaining=None):
        allowed = {"read_file": {"path", "start", "end"}, "search_code": {"query"},
                   "run_check": {"check_id"}, "propose_patch": {"path", "content", "explanation"},
                   "propose_patch_bundle": {"files", "explanation"}}
        if name not in allowed or set(args) - allowed[name]:
            raise ValueError("Unknown tool or unsupported argument")
        if name == "read_file":
            return read_file(workspace, **args), None
        if name == "search_code":
            return search_code(workspace, **args), None
        if name == "run_check":
            if args != {"check_id": "tests"}:
                raise ValueError("Only the configured tests check is allowed")
            runner = self._runner_for(task, remaining)
            outcome = runner.run(workspace, task.check_command, run_id)
            check = outcome.to_dict()
            if not outcome.complete:
                self.store.append_event(run_id, "check_unconfirmed", check)
                raise CheckUnconfirmed()
            cp["baseline_check"] = check
            if outcome.passed:
                self._fail(run_id, "baseline_passed", "The selected baseline tests already pass; no patch was accepted")
            return check, None
        baseline = cp.get("baseline_check")
        if (not baseline or baseline.get("exit_code") in (None, 0) or baseline.get("timed_out")
                or baseline.get("truncated") or baseline.get("tests_run", 0) <= 0):
            raise ValueError("First reproduce a failing baseline check")
        if not isinstance(args.get("explanation"), str) or not args["explanation"].strip() or len(args["explanation"]) > 2000:
            raise ValueError("A concise explanation is required")
        proposal = (build_patch_bundle(task, workspace, args["files"]) if name == "propose_patch_bundle"
                    else build_proposal(task, workspace, args["path"], args["content"]))
        proposal["explanation"] = args["explanation"]  # informational, not an executable/digest field
        return {"proposal_digest": proposal["digest"], "status": "awaiting_approval"}, proposal

    def approve(self, run_id, digest, expected_version):
        row = self.store.get_run(run_id)
        approval = row["checkpoint"].get("approval") or {}
        if approval == {"digest": digest, "from_version": expected_version}:
            return row  # Lost HTTP response: the exact approval already committed.
        if row["state"] != "awaiting_approval" or not row["proposal"] or row["proposal"]["digest"] != digest:
            raise StoreConflict("The proposal is no longer pending or its digest changed")
        workspace = self.settings.data_dir / "workspaces" / run_id
        if tree_hash(workspace) != row["proposal"]["base_workspace_hash"]:
            raise StoreConflict("Workspace changed after the proposal; approval refused")
        checkpoint = copy.deepcopy(row["checkpoint"])
        checkpoint["approval"] = {"digest": digest, "from_version": expected_version}
        row = self.store.transition(run_id, {"awaiting_approval"}, "verifying",
                                    {"checkpoint": checkpoint}, expected_version=expected_version)
        self._submit(run_id, self.verify)
        return row

    def verify(self, run_id):
        try:
            row = self.store.get_run(run_id)
            if row["state"] != "verifying":
                return
            task = self.resolve_task(row["task_id"])
            workspace = prepare_workspace(self.settings.data_dir / "verification", uuid.uuid4().hex, task)
            proposal = {key: value for key, value in row["proposal"].items() if key != "explanation"}
            apply_proposal(task, workspace, proposal)
            self.store.append_event(run_id, "verification_started", {"proposal_digest": proposal["digest"],
                                    "workspace_hash": tree_hash(workspace), "runner": "OCI container"})
            check = self._runner_for(task).run(workspace, task.check_command, run_id)
            if self.store.get_run(run_id)["state"] != "verifying":
                return
            confirmed = check.complete
            reason = "verifier_passed" if check.passed else "verifier_failed" if confirmed else "verification_unconfirmed"
            self.store.transition(run_id, {"verifying"}, "succeeded" if check.passed else "failed",
                                  {"result": check.to_dict(), "stop_reason": reason})
        except StoreConflict:
            return
        except SandboxUnavailable:
            self._fail(run_id, "sandbox_unavailable", "Container unavailable; approved code was not executed on the host")
        except ServiceError:
            self._fail(run_id, "verification_contract", "The imported project snapshot could not be validated")
        except (WorkspaceError, OSError, ValueError, KeyError, TypeError):
            self._fail(run_id, "verification_contract", "The approved patch or immutable task no longer matches")

    def cancel(self, run_id):
        row = self.store.get_run(run_id)
        return self.store.transition(run_id, {row["state"]}, "cancelled", {"stop_reason": "operator_cancelled"}, row["version"])

    def resume(self, run_id, expected_version):
        row = self.store.get_run(run_id)
        state = "verifying" if row["proposal"] else "queued"
        row = self.store.transition(run_id, {"interrupted"}, state, {"stop_reason": None}, expected_version)
        self._submit(run_id, self.verify if state == "verifying" else self.diagnose)
        return row
