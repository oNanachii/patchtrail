"""Explicit local imports, immutable identities and source-review boundaries."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from patchtrail.import_project import main
from patchtrail.projects import ProjectCatalog, load_manifest, normalize_manifest, preview_project
from patchtrail.workspace import WorkspaceError, prepare_workspace, tree_hash


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="patchtrail-project-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "repository"
        (self.source / "tests").mkdir(parents=True)
        (self.source / "app.py").write_text('def result():\n    return "old"\n')
        (self.source / "tests/__init__.py").write_text("")
        (self.source / "tests/test_app.py").write_text('import unittest\nfrom app import result\nclass Test(unittest.TestCase):\n    def test_result(self):\n        self.assertEqual(result(), "new")\n')
        self.manifest = {"schema_version": 1, "project_name": "owned-python", "title": "Owned Python task",
                         "description": "Fix result while preserving its test.",
                         "files": ["app.py", "tests/__init__.py", "tests/test_app.py"],
                         "editable_files": ["app.py"], "check_profile": "unittest", "test_paths": ["tests"],
                         "provenance": "Trusted local authored test; no external benchmark claim"}
        self.catalog = ProjectCatalog(self.base / "private" / "projects")

    def test_preview_does_not_execute_or_publish_and_lists_exact_reviewed_files(self):
        before = tree_hash(self.source)
        (self.source / ".env").write_text("PRIVATE_KEY=excluded-by-default\n")
        preview = preview_project(self.source, self.manifest)
        self.assertFalse(preview["execution_performed"])
        self.assertEqual(preview["file_count"], 3)
        self.assertEqual({row["path"] for row in preview["files"]}, set(self.manifest["files"]))
        self.assertNotIn("excluded-by-default", json.dumps(preview))
        self.assertFalse(self.catalog.directory.exists())
        (self.source / ".env").unlink()
        self.assertEqual(before, tree_hash(self.source))

    def test_import_is_stable_and_works_after_the_original_repository_moves(self):
        original = tree_hash(self.source)
        result = self.catalog.import_project(self.source, self.manifest)
        repeated = self.catalog.import_project(self.source, self.manifest)
        self.assertTrue(result["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(result["id"], repeated["id"])
        self.assertEqual(len(result["id"]), 64)
        self.assertEqual(tree_hash(self.source), original)
        moved = self.base / "moved-original"
        self.source.rename(moved)
        task = self.catalog.get_task(result["id"])
        self.assertTrue(task.private)
        self.assertEqual(task.source_hash, original)
        workspace = prepare_workspace(self.base / "workspaces", "private-run", task)
        self.assertEqual(tree_hash(workspace), original)
        metadata = self.catalog.list_tasks()
        self.assertNotIn(str(self.base), json.dumps(metadata))
        self.assertEqual(metadata[0]["snapshot_id"], result["snapshot_id"])
        self.assertEqual(self.catalog.list_projects()[0]["snapshot_count"], 1)

    def test_source_or_manifest_change_creates_a_new_bound_task_identity(self):
        first = self.catalog.import_project(self.source, self.manifest)
        (self.source / "app.py").write_text('def result():\n    return "different"\n')
        second = self.catalog.import_project(self.source, self.manifest)
        changed = deepcopy(self.manifest)
        changed["description"] = "Different task on the same source bytes"
        third = self.catalog.import_project(self.source, changed)
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        self.assertEqual(len(self.catalog.list_tasks()), 3)

    def test_concurrent_identical_imports_publish_one_complete_snapshot(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.catalog.import_project(self.source, self.manifest), range(4)))
        self.assertEqual(len({row["id"] for row in results}), 1)
        self.assertEqual(sum(row["created"] for row in results), 1)
        self.assertEqual(len(self.catalog.list_tasks()), 1)

    def test_preexisting_empty_destination_is_refused_and_preserved(self):
        imported = self.catalog.import_project(self.source, self.manifest)
        other = ProjectCatalog(self.base / "other-catalog")
        destination = other.directory / imported["snapshot_id"]
        destination.mkdir(parents=True)
        with self.assertRaises(WorkspaceError):
            other.import_project(self.source, self.manifest)
        self.assertEqual(list(destination.iterdir()), [])

    def test_unknown_task_and_builtin_task_are_not_private_catalog_entries(self):
        for task_id in ("quantity-shortfall", "../escape", "project-" + "a" * 56):
            with self.assertRaises(KeyError):
                self.catalog.get_task(task_id)
        self.assertEqual(self.catalog.list_tasks(), [])

    def test_snapshot_byte_drift_is_rejected_at_lookup_and_with_already_loaded_task(self):
        result = self.catalog.import_project(self.source, self.manifest)
        task = self.catalog.get_task(result["id"])
        path = task.source_dir / "app.py"
        path.chmod(0o644)
        path.write_text("# altered snapshot\n")
        with self.assertRaisesRegex(WorkspaceError, "registration"):
            self.catalog.get_task(result["id"])
        with self.assertRaisesRegex(WorkspaceError, "snapshot changed"):
            prepare_workspace(self.base / "workspaces", "changed-snapshot", task)

    def test_metadata_tamper_and_untracked_cache_source_are_rejected(self):
        result = self.catalog.import_project(self.source, self.manifest)
        task = self.catalog.get_task(result["id"])
        (task.source_dir / "__pycache__").mkdir()
        (task.source_dir / "__pycache__/extra.py").write_text("# not registered\n")
        with self.assertRaisesRegex(WorkspaceError, "unexpected directory"):
            self.catalog.list_tasks()
        (task.source_dir / "__pycache__/extra.py").unlink()
        (task.source_dir / "__pycache__").rmdir()
        metadata = task.source_dir.parent / "metadata.json"
        metadata.chmod(0o600)
        value = json.loads(metadata.read_text())
        value["manifest"]["editable_files"] = ["tests/test_app.py"]
        metadata.write_text(json.dumps(value))
        with self.assertRaises(WorkspaceError):
            self.catalog.get_task(result["id"])

    def test_missing_files_invalid_paths_symlinks_and_oversize_are_rejected(self):
        bad_names = ("../app.py", "/tmp/app.py", "./app.py", "app\\module.py", ".env", ".git/config", "model.joblib", "credentials.json")
        for name in bad_names:
            value = deepcopy(self.manifest)
            value["files"].append(name)
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                preview_project(self.source, value)
        link = self.source / "link.py"
        link.symlink_to(self.source / "app.py")
        value = deepcopy(self.manifest)
        value["files"].append("link.py")
        with self.assertRaisesRegex(WorkspaceError, "symlink"):
            preview_project(self.source, value)
        value["files"][-1] = "missing.py"
        with self.assertRaisesRegex(WorkspaceError, "missing"):
            preview_project(self.source, value)
        (self.source / "app.py").write_bytes(b"x" * (128 * 1024 + 1))
        with self.assertRaises(WorkspaceError):
            preview_project(self.source, self.manifest)

    def test_directory_symlinks_and_root_symlinks_are_rejected(self):
        (self.source / "linked").symlink_to(self.source / "tests", target_is_directory=True)
        value = deepcopy(self.manifest)
        value["files"].append("linked/test_app.py")
        with self.assertRaises(WorkspaceError):
            preview_project(self.source, value)
        link = self.base / "root-link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            preview_project(link, self.manifest)
        with self.assertRaises(WorkspaceError):
            ProjectCatalog(link)

    def test_configuration_test_and_arbitrary_commands_cannot_be_editable(self):
        for field, replacement in (("editable_files", ["tests/test_app.py"]), ("check_profile", "shell"),
                                    ("test_paths", ["tests;touch-marker"]), ("test_paths", [{"bad": "type"}])):
            value = deepcopy(self.manifest)
            value[field] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaises(WorkspaceError):
                normalize_manifest(value)
        for name in ("command", "container_image", "shell"):
            value = deepcopy(self.manifest)
            value[name] = "not allowed"
            with self.assertRaises(WorkspaceError):
                normalize_manifest(value)
        (self.source / "settings.py").write_text("SETTING = True\n")
        value = deepcopy(self.manifest)
        value["files"].append("settings.py")
        value["editable_files"] = ["settings.py"]
        with self.assertRaises(WorkspaceError):
            normalize_manifest(value)

    def test_control_characters_whitespace_and_config_variants_are_rejected_for_editing(self):
        for name in ("bad\nname.py", "bad\tname.py", "bad name.py", "bad;name.py", "module\x7f.py"):
            value = deepcopy(self.manifest)
            value["files"].append(name)
            with self.subTest(name=repr(name)), self.assertRaises(WorkspaceError):
                normalize_manifest(value)
        for name in ("config_dev.py", "settings_local.py", "app_config.py", "configs/helper.py", "configuration/runtime.py"):
            value = deepcopy(self.manifest)
            value["files"].append(name)
            # Read-only configuration may be included; it cannot be proposed.
            self.assertIn(name, normalize_manifest(value)["files"])
            value["editable_files"] = [name]
            with self.subTest(name=name), self.assertRaises(WorkspaceError):
                normalize_manifest(value)

    def test_pytest_selection_is_fixed_and_must_be_included(self):
        value = deepcopy(self.manifest)
        value["check_profile"] = "pytest"
        value["test_paths"] = ["tests/test_app.py::Test::test_result"]
        task = self.catalog.get_task(self.catalog.import_project(self.source, value)["id"])
        self.assertEqual(task.check_profile, "pytest")
        self.assertIn("tests/test_app.py::Test::test_result", task.check_command)
        value["test_paths"] = ["absent/test_missing.py"]
        with self.assertRaisesRegex(WorkspaceError, "inventory"):
            normalize_manifest(value)

    def test_inventory_changed_after_review_cannot_be_committed(self):
        preview = preview_project(self.source, self.manifest)
        (self.source / "app.py").write_text("# source changed\n")
        with self.assertRaisesRegex(WorkspaceError, "reviewed"):
            self.catalog.import_project(self.source, self.manifest, expected_inventory=preview["files"])
        self.assertFalse(self.catalog.directory.exists())

    def test_total_tree_file_count_binary_and_manifest_schema_limits(self):
        value = deepcopy(self.manifest)
        for i in range(17):
            name = f"extra{i}.txt"
            (self.source / name).write_bytes(b"x" * (128 * 1024))
            value["files"].append(name)
        with self.assertRaisesRegex(WorkspaceError, "2 MiB"):
            preview_project(self.source, value)
        value["files"] = [f"file{i}.py" for i in range(201)]
        with self.assertRaises(WorkspaceError):
            normalize_manifest(value)
        (self.source / "app.py").write_bytes(b"hello\x00binary")
        with self.assertRaisesRegex(WorkspaceError, "Binary"):
            preview_project(self.source, self.manifest)
        for version in (True, 2, "1"):
            value = deepcopy(self.manifest)
            value["schema_version"] = version
            with self.assertRaises(WorkspaceError):
                normalize_manifest(value)

    def test_cli_defaults_to_preview_and_commits_only_with_explicit_flag(self):
        path = self.base / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        args = [str(self.source), "--manifest", str(path), "--data-dir", str(self.base / "cli-data")]
        with patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(main(args), 0)
            self.assertFalse(json.loads(output.getvalue())["execution_performed"])
        self.assertFalse((self.base / "cli-data").exists())
        with patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(main(args + ["--commit"]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["created"])
            self.assertNotIn(str(self.base), output.getvalue())
        self.assertEqual(load_manifest(path)["project_name"], "owned-python")
        path.write_text("not JSON")
        with patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(main(args), 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "import_rejected")
            self.assertNotIn(str(self.base), output.getvalue())


if __name__ == "__main__":
    unittest.main()
