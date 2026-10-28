# PatchTrail implementation contract

v0.1 contract, 2026-10-28. Build independent code referencing mini-swe-agent's small agent/environment interfaces, OpenHands' event/workspace separation, and Inspect's task/verifier separation. Do not import a large framework or copy source by default.

## Product scope

An operator selects a preconfigured Python task with an actual failing test. One agent reads files, searches code, runs the task's fixed check, and proposes a single-file replacement. A human approves the exact proposal digest. The system reconstructs a verification workspace, applies the approved change, runs the immutable tests in a container and records the outcome. Public visitors can run a clearly labelled scripted demonstration, browse events, review a diff and inspect evidence. Real model runs and changes require operator access. Do not let visitors execute arbitrary commands or submit code.

## File boundaries

`patchtrail/storage.py`: SQLite persistence only. `tasks.py`, `workspace.py`, `sandbox.py`: catalog, workspace/proposal rules and container runner. `providers.py`: DeepSeek chat-completions tool calls and scripted demonstrations. `service.py`: orchestration. `api.py`: REST/auth/static interface. `web/`: minimal frontend, replaceable by the user through Claude after contract acceptance.

## Storage API

Exceptions `StoreConflict`, `RunNotFound`.

`Store(database: Path)` initializes parent/schema. Row dictionaries contain id, task_id, mode, state, version, limits, checkpoint, proposal, result, stop_reason, created_at, updated_at. JSON fields decode to native objects; proposal/result initially null, checkpoint initially {}.

- `create_run(task_id: str, mode: str, request_key: str, limits: dict) -> dict`: generate uuid hex id; same key with same task/mode/limits reuses the row, different request conflicts. Include a boolean `created` in the returned dict (not stored).
- `get_run(run_id: str) -> dict`: raise RunNotFound if absent.
- `list_runs(limit: int = 30) -> list[dict]`: newest first, hard cap100.
- `transition(run_id: str, expected_states: set[str], state: str, updates: dict | None = None, expected_version: int | None = None) -> dict`: atomically check state/version, update allowed fields (checkpoint/proposal/result/stop_reason), increment version, and append a state_changed event. Allowed states: queued, diagnosing, awaiting_approval, verifying, succeeded, failed, cancelled, interrupted. Reject unsupported transitions. Same-state checkpoint updates allowed only for active states. Terminal states cannot be reopened; interrupted may resume as queued or verifying.
- `append_event(run_id: str, kind: str, payload: dict, call_id: str | None = None) -> dict`: id, run_id, kind, payload, call_id, created_at. When call_id is present, same run/kind/call_id and same payload is idempotent; different payload conflicts. Validate run exists.
- `events(run_id: str, after: int = 0) -> list[dict]`: ordered by global event id, capped1000.
- `recover_active() -> list[str]`: atomically mark diagnosing/verifying runs interrupted with explicit restart reason; leave queued and awaiting_approval intact. No blind replay of external actions.

SQLite connections per operation, busy timeout, foreign keys, transactions for state changes. Tests must exercise restart, races and idempotency meaningfully.

## Task API

Frozen dataclass `TaskSpec`: id, title, description, source_dir: Path, editable_files: tuple[str,...], check_command: tuple[str,...], demo_path: str, demo_content: str, tags: tuple[str,...]. Single editable file in each first-version task. Solutions are held by the scripted provider only; never copied into a model's workspace or exposed by API task descriptions.

- `get_task(task_id: str) -> TaskSpec`: KeyError if unknown.
- `list_tasks() -> list[dict]`: public metadata id/title/description/editable_files/check_id="tests"/tags/source_hash; no solution.
- Three fixtures under patchtrail/fixtures/<id>/: editable src/*.py and read-only tests/*.py. Suggested bugs: quantity shortfall vs mere ingredient presence; replenishment extending old batch expiry; label/history non-atomic update. Tests use stdlib unittest and create temporary DBs under /tmp. Include an origin note: small authored reproductions based on observed issues, not blind/external benchmark tasks.

## Workspace API

`WorkspaceError(ValueError)` for path/contract violations.

- `tree_hash(directory: Path) -> str`: deterministic content/path hash, exclude __pycache__ and .pyc; reject symlinks and oversized trees.
- `prepare_workspace(base: Path, run_id: str, task: TaskSpec) -> Path`: create a new copy under base/run_id. Validate run id; if exists, verify/return existing safe directory, no reset or deletion of user paths.
- `read_file(workspace: Path, path: str, start: int = 1, end: int = 200) -> dict`: normalized relative paths, containment and symlink checks, bounded read; return path/numbered text/content_sha256. Source and tests may be read, no other filesystem.
- `search_code(workspace: Path, query: str) -> dict`: bounded literal search, max30 hits.
- `build_proposal(task: TaskSpec, workspace: Path, path: str, content: str) -> dict`: whitelist editable file, max16KiB UTF-8, reject no change. Return digest, path, content, old_sha256, new_sha256, base_workspace_hash, source_hash and unified diff. Digest binds all proposal content and source identity; use canonical JSON SHA256.
- `apply_proposal(task: TaskSpec, workspace: Path, proposal: dict) -> None`: validate digest, task/source identity, current workspace hash and old file hash; atomic single-file replacement, reject drift. Never modify fixtures.

Service verifies in a new workspace copied from the same immutable fixture, not in an untrusted resumed directory. Investigation files are not modified before approval.

## Sandbox API

`SandboxUnavailable(RuntimeError)`. `CheckResult` dataclass with exit_code:int|None, output:str, duration_ms:int, timed_out:bool, truncated:bool, runner:str="docker"; `passed` property and `to_dict()`.

`DockerRunner(binary: str = "docker", image: str = "python:3.12-slim", timeout: int = 20)`:

- `available() -> bool`: bounded daemon availability probe.
- `run(workspace: Path, command: tuple[str,...], run_id: str) -> CheckResult`: Docker/compatible OCI CLI, no shell. Network none, read-only root and workspace mount, tmpfs /tmp, non-root UID, all capabilities dropped, no-new-privileges, CPU/memory/pid limits, no credentials. Bound output to64KiB and total wall time; clean own named container on interruption. Never fall back to host execution. The service passes only task.check_command, never model-provided commands.

Tests can inject an explicit fake runner, clearly labelled; production chooses only DockerRunner. Actual container integration must be reported separately from mock tests.

## Provider and service rules

DeepSeek official chat-completions, configurable current model, app-side argument validation. Preserve required assistant tool-call messages and return one tool result per tool_call_id. Provider exceptions must not expose keys/authorization headers. Network timeout, max output tokens, max calls, max input characters and total elapsed limits. No automatic retry after an unknown external outcome.

Tools: read_file(path,start,end), search_code(query), run_check(check_id="tests"), propose_patch(path,content,explanation). Proposing a patch transitions to awaiting_approval and stops automatic execution. Successful natural-language answers alone cannot complete a task. Scripted mode uses the same service/tools but deterministic curated actions and is labelled as a runtime demonstration in API and UI.

Approval must include proposal_digest and expected_version. After approval, reconstruct fresh workspace, apply exact proposal, run actual checks, store succeeded/failed based on CheckResult. Cancellation and restart have explicit states and events. No model/provider credentials enter checkpoints or web responses. Public responses omit replacement content if unnecessary but expose diff, hashes, provenance and verifier results.

## API contract

GET /api/health; GET /api/tasks; GET /api/runs; GET /api/runs/{id}; GET /api/runs/{id}/events?after=N; GET /api/runs/{id}/report.

POST /api/runs body {task_id, mode:"demo"|"live"}, header Idempotency-Key; return row. POST /api/runs/{id}/approve {proposal_digest, expected_version}; POST /api/runs/{id}/cancel; POST /api/runs/{id}/resume {expected_version}. Live operations require bearer PATCHTRAIL_ADMIN_TOKEN; public demo may approve only scripted proposals, with no user-provided content. Rate limit and queue cap for public starts. API error object {error:{code,message}} with 400/401/404/409/422/429/503 as appropriate.

## Delivery evidence

Separate unit/contract tests, actual container checks, live-model task outcomes, browser checks, server deployment and user interview acceptance. Store actual failures as well as success. Prepare docs/CLAUDE_FRONTEND_HANDOFF.md, architecture/decisions, reproducible commands, resume wording with evidence boundaries, and a short interview walkthrough. No claimed novelty, production certification, blind benchmark or measured efficiency without corresponding evidence.
