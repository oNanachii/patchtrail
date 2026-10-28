from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from patchtrail.tasks import get_task
from patchtrail.workspace import (WorkspaceError, apply_proposal, build_proposal, prepare_workspace,
                                 read_file, search_code, tree_hash)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="patchtrail-workspace-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.task = get_task("quantity-shortfall")
        self.workspace = prepare_workspace(self.base / "workspaces", "run-001", self.task)

    def test_hash_is_stable_and_excludes_python_caches(self):
        before = tree_hash(self.workspace)
        (self.workspace / "__pycache__").mkdir()
        (self.workspace / "__pycache__/anything.pyc").write_bytes(b"not source")
        self.assertEqual(tree_hash(self.workspace), before)
        source = self.workspace / self.task.demo_path
        source.write_text(source.read_text() + "\n# changed\n")
        self.assertNotEqual(tree_hash(self.workspace), before)

    def test_symlink_and_oversized_files_are_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside workspace")
        link = self.workspace / "link.txt"
        link.symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            tree_hash(self.workspace)
        with self.assertRaises(WorkspaceError):
            read_file(self.workspace, "link.txt")
        link.unlink()
        (self.workspace / "large.txt").write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(WorkspaceError):
            tree_hash(self.workspace)

    def test_safe_existing_workspace_is_retained_not_reset(self):
        path = self.workspace / self.task.demo_path
        changed = path.read_text() + "\n# preserved local source modification\n"
        path.write_text(changed)
        same = prepare_workspace(self.base / "workspaces", "run-001", self.task)
        self.assertEqual(same, self.workspace)
        self.assertEqual(path.read_text(), changed)
        (self.workspace / "tests/test_quantities.py").write_text("# altered tests\n")
        with self.assertRaisesRegex(WorkspaceError, "immutable"):
            prepare_workspace(self.base / "workspaces", "run-001", self.task)

    def test_concurrent_prepare_does_not_overwrite_or_leave_partial_copies(self):
        def prepare(_):
            return prepare_workspace(self.base / "parallel", "same-run", self.task)
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(prepare, range(4)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(tree_hash(paths[0]), tree_hash(self.task.source_dir))

    def test_invalid_run_ids_and_path_traversal_are_rejected(self):
        for identity in ["../escape", "", "/absolute", ".", "x" * 97]:
            with self.subTest(identity=identity):
                with self.assertRaises(WorkspaceError):
                    prepare_workspace(self.base / "workspaces", identity, self.task)
        for path in ["../outside.txt", "/etc/passwd", "src/../ORIGIN.md", "./ORIGIN.md", "src\\quantities.py"]:
            with self.subTest(path=path):
                with self.assertRaises(WorkspaceError):
                    read_file(self.workspace, path)

    def test_bounded_read_and_literal_search(self):
        result = read_file(self.workspace, self.task.demo_path, 1, 3)
        self.assertIn("1:", result["text"])
        self.assertEqual(result["end"], 3)
        self.assertEqual(len(result["content_sha256"]), 64)
        with self.assertRaises(WorkspaceError):
            read_file(self.workspace, self.task.demo_path, 1, 401)
        with self.assertRaises(WorkspaceError):
            read_file(self.workspace, self.task.demo_path, True, 10)
        self.assertTrue(search_code(self.workspace, "shopping_shortfall")["hits"])
        self.assertEqual(search_code(self.workspace, "[not a regex]")["hits"], [])
        with self.assertRaises(WorkspaceError):
            search_code(self.workspace, "")
        (self.workspace / "repeated.txt").write_text("needle\n" * 40)
        results = search_code(self.workspace, "needle")
        self.assertEqual(len(results["hits"]), 30)
        self.assertTrue(results["truncated"])

    def test_proposal_rejects_test_changes_empty_changes_and_large_content(self):
        original = (self.workspace / self.task.demo_path).read_text()
        for path, content in [("tests/test_quantities.py", "bad test"),
                              (self.task.demo_path, original), (self.task.demo_path, "x" * 16385)]:
            with self.subTest(path=path, content_length=len(content)):
                with self.assertRaises(WorkspaceError):
                    build_proposal(self.task, self.workspace, path, content)

    def test_exact_proposal_applies_without_changing_original_fixture_or_tests(self):
        original_hash = tree_hash(self.task.source_dir)
        tests = (self.workspace / "tests/test_quantities.py").read_bytes()
        proposal = build_proposal(self.task, self.workspace, self.task.demo_path, self.task.demo_content)
        self.assertIn("--- a/src/quantities.py", proposal["diff"])
        apply_proposal(self.task, self.workspace, proposal)
        self.assertEqual((self.workspace / self.task.demo_path).read_text(), self.task.demo_content)
        self.assertEqual((self.workspace / "tests/test_quantities.py").read_bytes(), tests)
        self.assertEqual(tree_hash(self.task.source_dir), original_hash)
        with self.assertRaises(WorkspaceError):
            apply_proposal(self.task, self.workspace, proposal)

    def test_content_digest_workspace_and_task_drift_are_rejected(self):
        proposal = build_proposal(self.task, self.workspace, self.task.demo_path, self.task.demo_content)
        forged = {**proposal, "content": proposal["content"] + "\n# unapproved change\n"}
        with self.assertRaises(WorkspaceError):
            apply_proposal(self.task, self.workspace, forged)
        with self.assertRaises(WorkspaceError):
            apply_proposal(get_task("batch-expiry"), self.workspace, proposal)
        (self.workspace / self.task.demo_path).write_text("# workspace drift\n")
        with self.assertRaises(WorkspaceError):
            apply_proposal(self.task, self.workspace, proposal)

    def test_source_fixture_version_drift_is_rejected(self):
        fixture_copy = prepare_workspace(self.base / "private-fixture", "copy", self.task)
        copied_task = replace(self.task, source_dir=fixture_copy)
        workspace = prepare_workspace(self.base / "new-workspaces", "source-version", copied_task)
        proposal = build_proposal(copied_task, workspace, copied_task.demo_path, copied_task.demo_content)
        source_file = fixture_copy / copied_task.demo_path
        source_file.write_text(source_file.read_text() + "\n# new source version\n")
        with self.assertRaises(WorkspaceError):
            apply_proposal(copied_task, workspace, proposal)


if __name__ == "__main__":
    unittest.main()
