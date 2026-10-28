"""Actual OCI import/bundle regression on authored fixtures; no provider or model."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchtrail.config import runtime_code_hash
from patchtrail.projects import ProjectCatalog
from patchtrail.sandbox import DockerRunner
from patchtrail.workspace import (WorkspaceError, apply_proposal, build_patch_bundle,
                                 prepare_workspace, tree_hash)


SOURCE = {
    "left.py": "def add_one(value):\n    return value\n",
    "right.py": "def double(value):\n    return value\n",
    "tests/__init__.py": "",
    "tests/test_values.py": (
        "import unittest\nfrom left import add_one\nfrom right import double\n\n"
        "class Values(unittest.TestCase):\n"
        "    def test_add_positive(self): self.assertEqual(add_one(3), 4)\n"
        "    def test_add_negative(self): self.assertEqual(add_one(-2), -1)\n"
        "    def test_double_positive(self): self.assertEqual(double(3), 6)\n"
        "    def test_double_zero(self): self.assertEqual(double(0), 0)\n"
    ),
}
REPLACEMENTS = [
    {"path": "left.py", "content": "def add_one(value):\n    return value + 1\n"},
    {"path": "right.py", "content": "def double(value):\n    return value * 2\n"},
]


def test_hashes(directory):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in SOURCE if name.startswith("tests/")}


def verify(runner, record):
    with TemporaryDirectory(prefix="patchtrail-real-imported-bundle-") as temporary:
        root = Path(temporary)
        original = root / "original"
        for name, content in SOURCE.items():
            path = original / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        source_hash = tree_hash(original)
        tests = test_hashes(original)
        catalog = ProjectCatalog(root / "catalog")
        imported = catalog.import_project(original, {
            "schema_version": 1, "project_name": "authored-bundle-regression",
            "title": "Two-file imported bundle runtime check",
            "description": "Fix authored add-one and double functions; keep the fixed tests unchanged.",
            "files": sorted(SOURCE), "editable_files": ["left.py", "right.py"],
            "check_profile": "unittest", "test_paths": ["tests"],
            "provenance": "Self-authored curated runtime regression; not a real-model task or external benchmark",
        })
        original.rename(root / "original-moved")
        moved = root / "original-moved"
        task = catalog.get_task(imported["id"])
        assert not original.exists() and tree_hash(moved) == source_hash
        assert tree_hash(task.source_dir) == source_hash
        record.update({"task_id": task.id, "snapshot_id": task.snapshot_id,
                       "source_hash": source_hash, "original_path_moved": True,
                       "catalog_resolves_without_original_path": True})
        workspace = prepare_workspace(root / "investigation", "bundle-baseline", task)
        baseline = runner.run(workspace, task.check_command, "imported-bundle-baseline")
        record["baseline"] = baseline.to_dict()
        assert baseline.complete and not baseline.passed and baseline.exit_code == 1
        assert baseline.tests_run == 4 and tree_hash(workspace) == source_hash
        proposal = build_patch_bundle(task, workspace, REPLACEMENTS)
        record["proposal"] = {"digest": proposal["digest"], "source_hash": proposal["source_hash"],
                              "files": [{key: entry[key] for key in ("path", "old_sha256", "new_sha256")}
                                        for entry in proposal["files"]], "diff": proposal["diff"]}
        record["tamper_checks"] = []
        for index, entry in enumerate(proposal["files"]):
            for field in ("old_sha256", "new_sha256"):
                target = prepare_workspace(root / "tamper", f"bundle-{index}-{field}", task)
                before = tree_hash(target)
                altered = deepcopy(proposal)
                altered["files"][index][field] = "0" * 64
                try:
                    apply_proposal(task, target, altered)
                except WorkspaceError:
                    pass
                else:
                    raise AssertionError("A tampered per-file digest was accepted")
                assert tree_hash(target) == before and test_hashes(target) == tests
                record["tamper_checks"].append({"path": entry["path"], "field": field,
                                                "rejected": True, "no_partial_application": True})
        verified = prepare_workspace(root / "verification", "bundle-fresh", task)
        assert verified != workspace and tree_hash(verified) == source_hash
        apply_proposal(task, verified, proposal)
        assert all((verified / entry["path"]).read_text(encoding="utf-8") == entry["content"]
                   for entry in REPLACEMENTS)
        outcome = runner.run(verified, task.check_command, "imported-bundle-verification")
        record["verification"] = outcome.to_dict()
        assert outcome.complete and outcome.passed and outcome.tests_run == 4
        assert tree_hash(moved) == tree_hash(task.source_dir) == tree_hash(workspace) == source_hash
        assert test_hashes(moved) == test_hashes(task.source_dir) == test_hashes(verified) == tests
        record.update({"original_source_unchanged": True, "snapshot_source_unchanged": True,
                       "investigation_unchanged": True, "immutable_tests_unchanged": True,
                       "verification_used_fresh_snapshot_copy": True, "test_hashes": tests})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="podman")
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    record = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "status": "failed",
              "scope": "curated imported snapshot and atomic two-file bundle through actual OCI checks",
              "fixture_origin": "self-authored small Python runtime regression",
              "model_called": False, "provider_used": False, "model_effectiveness_evaluated": False,
              "binary": args.binary, "image": args.image, "runtime_code_hash": runtime_code_hash()}
    try:
        verify(DockerRunner(args.binary, args.image), record)
        record["status"] = "passed"
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": record["status"], "model_called": False,
                      "actual_container_checks": int("baseline" in record) + int("verification" in record),
                      "tamper_rejections": len(record.get("tamper_checks", [])),
                      **({"error": record["error"]} if "error" in record else {})}, ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
