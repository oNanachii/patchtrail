# PatchTrail project rules

Build a controlled repository debugging and patch-verification workbench for Agent/Python-backend and full-stack job applications. Read docs/IMPLEMENTATION_CONTRACT.md and PROJECT_STATUS.md before continuing.

- Preserve the user's existing projects. This is a new independent project.
- The user personally operates Claude Code. Prepare a frontend handoff; do not launch, log into, or submit work to Claude.
- No arbitrary public repositories, uploaded code, shell strings, or user-provided commands in v0.1. Only the catalog's task fixtures and check IDs.
- The user's later request to simplify imports authorizes v0.3 operator-only browser file imports: bounded explicit text files, private preview, manual commit, the existing immutable catalog and fixed test profiles. Anonymous uploads, archives, remote Git URLs, arbitrary commands, uploaded images/models and automatic dependency installation remain outside scope. Import must never start a model run; live execution still needs its separate user confirmation.
- The user subsequently authorized v0.4 invitation-only BYOK, isolated member workspaces, preflight checks before model calls, and verified replacement-file downloads. Preserve the legacy operator namespace; no platform billing or public sign-up. Keys stay memory-only and remote credential traffic requires HTTPS.
- All runtime checks execute in a container with network disabled and resource limits. If the container runner is unavailable, report that state; never fall back to the host.
- Model credentials stay on the server and out of workspaces, traces, browser state and Git. Public demo uses a clearly labelled scripted provider; live runs require operator authentication.
- Model claims cannot mark a run successful. A proposal requires approval against its exact digest and workspace version, followed by actual verifier results.
- Checkpoint recovery is explicit; do not claim exactly-once execution or universal sandbox security.
- Tests of the runtime, container integration, real-model effectiveness, user acceptance and interview mastery are separate evidence.
- Reference upstream designs honestly. Do not copy upstream code without attribution and the applicable license. Do not reuse upstream benchmark scores.
- Keep scope small: one agent, SQLite, a bounded tool loop, immutable task fixtures, run events, patch approval and deterministic verification. No speculative multi-agent framework or vector database.

## Ownership during this turn

- Root: service/providers/API/web/deployment/docs/integration.
- Storage agent: patchtrail/storage.py and tests/test_storage.py.
- Workspace agent: patchtrail/tasks.py, workspace.py, sandbox.py, fixtures/ and corresponding tests.

Coordinate interface changes before changing another owner's files.
