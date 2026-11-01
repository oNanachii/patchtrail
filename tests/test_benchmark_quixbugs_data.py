"""Data conversion checks use inert fixtures; benchmark tests never run on host."""
import hashlib
import io
from pathlib import Path
import stat
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from patchtrail.projects import normalize_manifest, preview_project
from scripts import benchmark_quixbugs_data as data


UPSTREAM_TEST = '''import pytest
from load_testdata import load_json_testcases
from node import Node

if pytest.use_correct:
    from correct_python_programs.example import example
else:
    from python_programs.example import example

@pytest.mark.parametrize("input_data,expected", load_json_testcases("example"))
def test_example(input_data, expected):
    if not pytest.run_slow and input_data == [1]:
        pytest.skip("original slow test")
    assert example(*input_data) == expected
'''


class BenchmarkQuixBugsDataTests(unittest.TestCase):
    def make_upstream(self, directory, source="raise RuntimeError('must never execute')\n"):
        files = {"python_programs/example.py": source, "python_testcases/test_example.py": UPSTREAM_TEST,
                 "python_testcases/load_testdata.py": "# inert helper\n",
                 "python_testcases/node.py": "# inert helper\n", "json_testcases/example.json": "[[1], 1]\n",
                 "LICENSE": "MIT fixture\n", "legal_notes.txt": "upstream caveat fixture\n"}
        inventory = {}
        for relative, content in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            inventory[relative] = hashlib.sha256(content.encode()).hexdigest()
        return inventory

    def test_conversion_preserves_assertions_default_skip_and_excludes_gold(self):
        converted = data.adapt_test(UPSTREAM_TEST)
        self.assertNotIn("correct_python_programs", converted)
        self.assertIn("from python_programs.example import example", converted)
        self.assertIn("from python_testcases.load_testdata import load_json_testcases", converted)
        self.assertIn("from python_testcases.node import Node", converted)
        self.assertIn("if not False and input_data == [1]:", converted)
        self.assertIn("assert example(*input_data) == expected", converted)
        with self.assertRaises(ValueError):
            data.adapt_test(converted)

    def test_import_manifest_and_readonly_tests_are_idempotent_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            upstream = base / "upstream"
            inventory = self.make_upstream(upstream)
            with patch.object(data, "TASK_NAMES", ("example",)), patch.object(
                    data, "_input_inventory", return_value=inventory):
                result = data.prepare_dataset(base / "data", upstream)
                self.assertEqual(result, data.prepare_dataset(base / "data", upstream))
                task = result["tasks"][0]
                manifest = normalize_manifest(task["manifest"])
                preview = preview_project(task["source_dir"], manifest)
                self.assertGreater(preview["file_count"], 2)
                self.assertEqual(manifest["editable_files"], ["python_programs/example.py"])
                source = Path(task["source_dir"])
                self.assertTrue((source / manifest["editable_files"][0]).stat().st_mode & stat.S_IWUSR)
                for path in manifest["files"]:
                    if path not in manifest["editable_files"]:
                        self.assertFalse((source / path).stat().st_mode & 0o222)
                self.assertFalse((source / "correct_python_programs").exists())
                changed = source / "python_programs/example.py"
                changed.write_text("# changed after registration\n")
                with self.assertRaisesRegex(ValueError, "differs"):
                    data.prepare_dataset(base / "data", upstream)

    def test_missing_dependency_is_recorded_before_task_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            upstream = base / "upstream"
            inventory = self.make_upstream(upstream, "import unavailable_benchmark_dependency\n")
            with patch.object(data, "TASK_NAMES", ("example",)), patch.object(
                    data, "_input_inventory", return_value=inventory):
                result = data.prepare_dataset(base / "data", upstream)
            self.assertEqual(result["tasks"], [])
            self.assertEqual(result["skipped"], [{"name": "example", "reason": "unsupported_dependency",
                                               "dependencies": ["unavailable_benchmark_dependency"]}])

    def test_unpinned_or_tampered_offline_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream = Path(temporary).resolve()
            self.make_upstream(upstream)
            with self.assertRaisesRegex(ValueError, "pinned revision"):
                data._input_inventory(upstream)

    def test_unverified_archive_and_symlink_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            archive = base / "quixbugs/upstream" / f"{data.UPSTREAM_COMMIT}.tar.gz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"untrusted archive")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                data.download_upstream(base)
            target = base / "target"
            target.mkdir()
            symlink = base / "symlink"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                data.prepare_dataset(symlink)

    def test_verified_archive_selectively_extracts_inputs_without_gold_sources(self):
        output = io.BytesIO()
        prefix = "QuixBugs-" + data.UPSTREAM_COMMIT
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            root = tarfile.TarInfo(prefix)
            root.type = tarfile.DIRTYPE
            bundle.addfile(root)
            for relative in ("LICENSE", "python_programs/example.py", "correct_python_programs/example.py"):
                member = tarfile.TarInfo(prefix + "/" + relative)
                member.size = len(b"fixture\n")
                bundle.addfile(member, io.BytesIO(b"fixture\n"))
        content = output.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            archive = base / "quixbugs/upstream" / f"{data.UPSTREAM_COMMIT}.tar.gz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(content)
            with patch.object(data, "ARCHIVE_SHA256", hashlib.sha256(content).hexdigest()), patch.object(
                    data, "_input_inventory", return_value={}):
                upstream = data.download_upstream(base)
            self.assertEqual((upstream / "python_programs/example.py").read_bytes(), b"fixture\n")
            self.assertFalse((upstream / "correct_python_programs").exists())


if __name__ == "__main__":
    unittest.main()
