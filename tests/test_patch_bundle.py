"""Atomic replacement bundles and independently applicable exported diffs."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from patchtrail.tasks import get_task
from patchtrail.workspace import (WorkspaceError, apply_proposal, build_patch_bundle, build_proposal,
                                 prepare_workspace, tree_hash)


class PatchBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="patchtrail-bundle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        (self.source / "tests").mkdir(parents=True)
        (self.source / "one.py").write_text("ONE = 1\n")
        (self.source / "two.py").write_text("TWO = 2")
        (self.source / "tests/test_values.py").write_text("# immutable test bytes\n")
        self.task = replace(get_task("quantity-shortfall"), id="project-" + "a" * 56,
                            source_dir=self.source, editable_files=("one.py", "two.py"), private=True,
                            snapshot_id="b" * 64, source_hash=tree_hash(self.source))
        self.workspace = prepare_workspace(self.base / "workspaces", "bundle-run", self.task)
        self.files = [{"path": "one.py", "content": "ONE = 10\n"}, {"path": "two.py", "content": "TWO = 20\n"}]

    def test_bundle_is_order_independent_and_applies_all_files_with_tests_and_source_unchanged(self):
        before = tree_hash(self.source)
        proposal = build_patch_bundle(self.task, self.workspace, self.files)
        self.assertEqual(proposal, build_patch_bundle(self.task, self.workspace, list(reversed(self.files))))
        apply_proposal(self.task, self.workspace, proposal)
        self.assertTrue(self.workspace.is_dir())
        for entry in self.files:
            self.assertEqual((self.workspace / entry["path"]).read_text(), entry["content"])
        self.assertEqual((self.workspace / "tests/test_values.py").read_bytes(), b"# immutable test bytes\n")
        self.assertEqual(tree_hash(self.source), before)
        self.assertFalse([path for path in self.workspace.parent.glob(".bundle-*") if path.is_dir()])
        with self.assertRaises(WorkspaceError):
            apply_proposal(self.task, self.workspace, proposal)

    def test_forged_content_digest_or_second_file_cannot_partially_change_first_file(self):
        original = tree_hash(self.workspace)
        good = build_patch_bundle(self.task, self.workspace, self.files)
        forged = deepcopy(good)
        forged["files"][1]["content"] = "TWO = 999\n"
        for proposal in (forged, {**good, "digest": "0" * 64}):
            with self.assertRaises(WorkspaceError):
                apply_proposal(self.task, self.workspace, proposal)
            self.assertEqual(tree_hash(self.workspace), original)
        with patch("patchtrail.workspace._exchange_directories", side_effect=WorkspaceError("simulated prepublication failure")):
            with self.assertRaises(WorkspaceError):
                apply_proposal(self.task, self.workspace, good)
        self.assertEqual(tree_hash(self.workspace), original)
        self.assertFalse([path for path in self.workspace.parent.glob(".bundle-*") if path.is_dir()])

    def test_duplicate_test_path_empty_change_and_more_than_three_files_are_rejected(self):
        for files in ([], self.files * 2, [self.files[0], self.files[0]],
                      [{"path": "tests/test_values.py", "content": "forged test"}],
                      [{"path": "one.py", "content": "ONE = 1\n"}],
                      [{"path": "one.py", "content": "x" * (128 * 1024 + 1)}]):
            with self.subTest(files=repr(files)[:100]), self.assertRaises(WorkspaceError):
                build_patch_bundle(self.task, self.workspace, files)

    def test_source_or_workspace_drift_refuses_all_replacements(self):
        good = build_patch_bundle(self.task, self.workspace, self.files)
        (self.workspace / "two.py").write_text("TWO = -1")
        before = tree_hash(self.workspace)
        with self.assertRaises(WorkspaceError):
            apply_proposal(self.task, self.workspace, good)
        self.assertEqual(tree_hash(self.workspace), before)
        (self.source / "one.py").write_text("ONE = -1\n")
        with self.assertRaises(WorkspaceError):
            build_patch_bundle(self.task, self.workspace, self.files)

    def test_transient_copy_drift_in_an_unproposed_editable_file_is_not_published(self):
        proposal = build_patch_bundle(self.task, self.workspace, [self.files[0]])
        before = tree_hash(self.workspace)
        original = Path.read_bytes
        reads = 0

        def transient_read(path):
            nonlocal reads
            if path == self.workspace / "two.py":
                reads += 1
                # First read is approval revalidation; the second is the copy.
                # Model a filesystem edit that reverts before the final root hash.
                if reads == 2:
                    return b"TWO = 999\n"
            return original(path)

        with patch.object(Path, "read_bytes", transient_read):
            with self.assertRaisesRegex(WorkspaceError, "publication baseline"):
                apply_proposal(self.task, self.workspace, proposal)
        self.assertEqual(tree_hash(self.workspace), before)

    def test_cleanup_failure_after_exchange_does_not_misreport_the_complete_commit(self):
        proposal = build_patch_bundle(self.task, self.workspace, self.files)
        with patch("patchtrail.workspace.shutil.rmtree", side_effect=OSError("simulated old-tree cleanup failure")):
            apply_proposal(self.task, self.workspace, proposal)
        for entry in self.files:
            self.assertEqual((self.workspace / entry["path"]).read_text(), entry["content"])
        self.assertEqual((self.workspace / "tests/test_values.py").read_bytes(), b"# immutable test bytes\n")

    @unittest.skipUnless(shutil.which("git"), "Git is needed only to validate exported patch syntax")
    def test_exported_bundle_and_single_file_diff_apply_with_missing_final_newline(self):
        for bundled in (True, False):
            with self.subTest(bundled=bundled):
                proposal = (build_patch_bundle(self.task, self.workspace, self.files) if bundled
                            else build_proposal(self.task, self.workspace, "two.py", "TWO = 20\n"))
                self.assertIn("\\ No newline at end of file", proposal["diff"])
                copy = self.base / ("reapply-bundle" if bundled else "reapply-single")
                shutil.copytree(self.source, copy)
                check = subprocess.run(["git", "apply", "--check", "-"], cwd=copy,
                                       input=proposal["diff"], text=True, capture_output=True)
                self.assertEqual(check.returncode, 0, check.stderr)
                applied = subprocess.run(["git", "apply", "-"], cwd=copy,
                                         input=proposal["diff"], text=True, capture_output=True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual((copy / "two.py").read_text(), "TWO = 20\n")
                if bundled:
                    self.assertEqual((copy / "one.py").read_text(), "ONE = 10\n")


if __name__ == "__main__":
    unittest.main()
