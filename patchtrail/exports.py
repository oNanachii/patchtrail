"""Verified replacement files; never derive archive paths from request input."""
import io
import json
from pathlib import Path
import tempfile
import uuid
import zipfile

from .service import ServiceError
from .workspace import apply_proposal, prepare_workspace, tree_hash


def repair_zip(service, run_id):
    row = service.store.get_run(run_id)
    if row["state"] != "succeeded" or not row.get("proposal") or not row.get("result", {}).get("passed"):
        raise ServiceError("files_not_available", "Replacement files require a successful verifier result", 409)
    if (row.get("checkpoint", {}).get("approval", {}).get("digest") != row["proposal"]["digest"]):
        raise ServiceError("files_not_available", "Verified approval no longer matches the proposal", 409)
    task = service.resolve_task(row["task_id"])
    proposal = {k: v for k, v in row["proposal"].items() if k != "explanation"}
    # apply_proposal rechecks the original source hash, exact digest, every old
    # hash, and all path/editability constraints in an entirely fresh copy.
    with tempfile.TemporaryDirectory(prefix="export-", dir=service.settings.data_dir) as directory:
        workspace = prepare_workspace(Path(directory), uuid.uuid4().hex, task)
        apply_proposal(task, workspace, proposal)
        files = proposal.get("files") or [proposal]
        manifest = {"run_id": run_id, "task_id": task.id, "source_hash": tree_hash(task.source_dir),
                    "proposal_digest": proposal["digest"], "files": [{key: f[key] for key in
                    ("path", "old_sha256", "new_sha256")} for f in files]}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in files:
                archive.writestr(entry["path"], (workspace / entry["path"]).read_bytes())
            archive.writestr("patchtrail-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("PATCHTRAIL_APPLY.md", "# Apply verified replacement files\n\n"
                "1. Back up your project or commit its current state.\n"
                "2. Check each original file's SHA-256 against old_sha256 in patchtrail-manifest.json. "
                "If any differs, stop: these files belong to another source version.\n"
                "3. Copy only the listed source files into the same relative paths in your project.\n"
                "4. Rerun your project's tests and review the diff. Tests/configuration are not replaced.\n\n"
                "Verification covers the recorded fixed tests only, not all possible behavior.\n")
        return output.getvalue()
