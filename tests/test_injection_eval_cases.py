"""Inspect inert fixture data; actual task execution is reserved for OCI checks."""
import ast
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import urlparse

from patchtrail.projects import normalize_manifest, preview_project
from scripts.injection_eval_cases import CASES, CONTRACTS, FAKE_CANARY, prepare_cases


class InjectionEvalCaseTests(unittest.TestCase):
    def prepare(self, base):
        return prepare_cases(base / "dataset", canary_dir=base / "outside-canary")

    def test_twelve_distinct_contracts_import_with_three_readonly_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.prepare(Path(temporary).resolve())
            self.assertEqual({task["id"] for task in result["tasks"]},
                             {f"S{number:02}" for number in range(1, 13)})
            self.assertEqual(len({contract[0] for contract in CONTRACTS.values()}), 12)
            for task in result["tasks"]:
                with self.subTest(task=task["id"]):
                    manifest = normalize_manifest(task["manifest"])
                    source_dir = Path(task["source_dir"])
                    self.assertEqual(preview_project(source_dir, manifest)["file_count"],
                                     len(manifest["files"]))
                    contract, wrong, correct, examples = CONTRACTS[task["id"]]
                    self.assertNotEqual(wrong, correct)
                    self.assertIn(contract, manifest["description"])
                    source = ast.parse((source_dir / "src/calc.py").read_text())
                    function = next(node for node in source.body if isinstance(node, ast.FunctionDef))
                    self.assertEqual(ast.dump(function.body[0].value), ast.dump(ast.parse(wrong, mode="eval").body))
                    oracle = ast.parse(task["oracle_replacements"][0]["content"])
                    fixed_function = next(node for node in oracle.body if isinstance(node, ast.FunctionDef))
                    self.assertEqual(ast.dump(fixed_function.body[0].value), ast.dump(ast.parse(correct, mode="eval").body))
                    test_tree = ast.parse((source_dir / "tests/test_calc.py").read_text())
                    assertions = [node for node in ast.walk(test_tree) if isinstance(node, ast.Assert)]
                    self.assertEqual(len(assertions), 3)
                    for assertion, (value, expected) in zip(assertions, examples):
                        # Literal inspection does not import or evaluate task source.
                        self.assertEqual(ast.literal_eval(assertion.test.left.args[0]), value)
                        self.assertEqual(ast.literal_eval(assertion.test.comparators[0]), expected)
                    for relative in manifest["files"]:
                        path = source_dir / relative
                        if relative.endswith(".py"):
                            ast.parse(path.read_text())
                        if relative not in manifest["editable_files"]:
                            self.assertFalse(path.stat().st_mode & 0o222)
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                                         task["files_sha256"][relative])

    def test_five_carriers_have_unique_canaries_and_only_reserved_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.prepare(Path(temporary).resolve())
            self.assertEqual({task["carrier"] for task in result["tasks"]},
                             {"source_comment", "source_docstring", "failure_output",
                              "read_file_content", "search_result"})
            self.assertEqual(len({case[3] for case in CASES}), 12)
            for number, task in enumerate(result["tasks"], 1):
                marker = f"CANARY-PT-{number:02}"
                self.assertEqual(task["exposure_marker"], marker)
                texts = [(Path(task["source_dir"]) / relative).read_text()
                         for relative in task["manifest"]["files"]]
                self.assertEqual(sum(marker in text for text in texts), 1)
                self.assertNotIn(marker, task["manifest"]["description"])
                self.assertTrue(any(task["attack_text"] in text for text in texts))
                if task["carrier"] in {"source_comment", "source_docstring"}:
                    self.assertIn(task["attack_text"], task["oracle_replacements"][0]["content"])
                if task["carrier"] == "search_result":
                    self.assertIn("INJECTION_HINT_" + task["id"], task["manifest"]["description"])
                for url in re.findall(r"https?://[^'\s\"]+", repr(task)):
                    self.assertEqual(urlparse(url).hostname, "example.invalid")
            command = next(task for task in result["tasks"] if task["id"] == "S07")
            self.assertEqual(command["expected_attack_calls"][0],
                             {"name": "execute_command", "arguments": {"command": "CANARY-PT-07"}})

    def test_outside_file_contains_only_fake_canary_and_bundle_exceeds_import_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            result = self.prepare(base)
            canary = Path(result["fake_canary"]["path"])
            self.assertEqual(canary.read_text(), FAKE_CANARY + "\n")
            self.assertFalse(canary.is_relative_to(base / "dataset"))
            for task in result["tasks"]:
                self.assertFalse(canary.is_relative_to(task["source_dir"]))
                self.assertNotIn(FAKE_CANARY, repr(task["manifest"]))
                self.assertFalse(any(FAKE_CANARY in (Path(task["source_dir"]) / relative).read_text()
                                     for relative in task["manifest"]["files"]))
            task = next(task for task in result["tasks"] if task["id"] == "S09")
            self.assertEqual(len(task["manifest"]["editable_files"]), 3)
            bundle = task["expected_attack_calls"][0]["arguments"]["files"]
            self.assertEqual(len(bundle), 4)
            self.assertEqual(bundle[0]["content"], task["oracle_replacements"][0]["content"])

    def test_preparation_is_idempotent_and_rejects_existing_drift_or_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            first = self.prepare(base)
            self.assertEqual(first, self.prepare(base))
            (Path(first["tasks"][0]["source_dir"]) / "src/calc.py").write_text("# changed\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                self.prepare(base)
            alias = base / "alias"
            alias.symlink_to(base / "dataset")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                prepare_cases(alias, canary_dir=base / "outside-canary")


if __name__ == "__main__":
    unittest.main()
