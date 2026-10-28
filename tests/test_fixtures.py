"""Trusted authored fixture checks on the development host, not container acceptance."""
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from patchtrail.sandbox import CheckResult
from patchtrail.tasks import get_task, list_tasks
from patchtrail.workspace import apply_proposal, build_proposal, prepare_workspace, tree_hash


class AuthoredFixtureTests(unittest.TestCase):
    def test_public_catalog_does_not_expose_scripted_solutions(self):
        catalog = list_tasks()
        self.assertEqual(len(catalog), 3)
        for metadata in catalog:
            self.assertEqual(metadata["check_id"], "tests")
            self.assertEqual(len(metadata["source_hash"]), 64)
            self.assertNotIn("demo_content", metadata)
            self.assertNotIn("demo_path", metadata)
            task = get_task(metadata["id"])
            self.assertEqual(len(task.editable_files), 1)
            self.assertNotEqual((task.source_dir / task.demo_path).read_text(), task.demo_content)
        with self.assertRaises(KeyError):
            get_task("not-in-catalog")

    def test_all_originals_fail_and_their_authored_replacements_pass(self):
        with TemporaryDirectory(prefix="patchtrail-trusted-fixtures-") as temporary:
            for metadata in list_tasks():
                with self.subTest(task_id=metadata["id"]):
                    task = get_task(metadata["id"])
                    original_hash = tree_hash(task.source_dir)
                    workspace = prepare_workspace(Path(temporary), task.id, task)
                    command = (sys.executable, *task.check_command[1:])
                    before = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(before.returncode, 0, before.stdout + before.stderr)
                    self.assertIn("FAILED", before.stderr)
                    self.assertIn("Ran 4 tests", before.stderr)
                    proposal = build_proposal(task, workspace, task.demo_path, task.demo_content)
                    apply_proposal(task, workspace, proposal)
                    after = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=10)
                    output = after.stdout + after.stderr
                    result = CheckResult(after.returncode, output, 0, False, False, runner="trusted-development-host")
                    self.assertTrue(result.passed, output)
                    self.assertEqual(result.tests_run, 4)
                    self.assertEqual(tree_hash(task.source_dir), original_hash)


if __name__ == "__main__":
    unittest.main()
