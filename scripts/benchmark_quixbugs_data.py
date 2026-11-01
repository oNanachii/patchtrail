"""Download pinned QuixBugs inputs and convert them to private import manifests.

This module only reads/writes data. It never imports benchmark programs, executes
their tests, contacts a model, or writes to a PatchTrail catalog/database.
"""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import urllib.request


UPSTREAM_REPOSITORY = "https://github.com/jkoppel/QuixBugs"
UPSTREAM_COMMIT = "4257f44b0ff1181dedaedee6a447e133219fcebf"
ARCHIVE_URL = f"https://codeload.github.com/jkoppel/QuixBugs/tar.gz/{UPSTREAM_COMMIT}"
ARCHIVE_SHA256 = "b9f87db002c152e579f9fab860417b122ad9d7e6240f311dbf49082965151b1f"
INPUT_SHA256 = "fe687d888bf9b9d56848f4eaacbfd840dd4226c6dbd2e49753d8b379f0c49a8f"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
INPUT_DIRECTORIES = ("python_programs", "python_testcases", "json_testcases")
TASK_NAMES = tuple("""bitcount breadth_first_search bucketsort depth_first_search
detect_cycle find_first_in_sorted find_in_sorted flatten gcd get_factors hanoi
is_valid_parenthesization kheapsort knapsack kth lcs_length levenshtein lis
longest_common_subsequence max_sublist_sum mergesort minimum_spanning_tree
next_palindrome next_permutation pascal possible_change powerset quicksort
reverse_linked_list rpn_eval shortest_path_length shortest_path_lengths
shortest_paths shunting_yard sieve sqrt subsequences to_base topological_ordering
wrap""".split())


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _safe_directory(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Benchmark paths cannot contain symlinks")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_once(path, content, *, editable=False):
    _safe_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Benchmark files cannot be symlinks")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"Existing benchmark content differs: {path.name}")
    else:
        with path.open("xb") as output:
            output.write(content)
    path.chmod(0o644 if editable else 0o444)


def _is_input(relative):
    return (relative in {"LICENSE", "legal_notes.txt"}
            or len(PurePosixPath(relative).parts) == 2
            and PurePosixPath(relative).parts[0] in INPUT_DIRECTORIES)


def _input_inventory(upstream):
    result = {}
    for directory in INPUT_DIRECTORIES:
        root = upstream / directory
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Pinned QuixBugs input directory is missing")
        for path in sorted(root.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Unexpected entry in QuixBugs inputs")
            result[path.relative_to(upstream).as_posix()] = _sha(path.read_bytes())
    for name in ("LICENSE", "legal_notes.txt"):
        path = upstream / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("QuixBugs license or legal notes are missing")
        result[name] = _sha(path.read_bytes())
    if _sha(_canonical(result)) != INPUT_SHA256:
        raise ValueError("QuixBugs inputs differ from the pinned revision")
    return result


def download_upstream(data_dir):
    """Verify the archive before selectively unpacking input files (no gold code)."""
    directory = _safe_directory(Path(data_dir) / "quixbugs" / "upstream")
    archive = directory / f"{UPSTREAM_COMMIT}.tar.gz"
    if archive.is_symlink():
        raise ValueError("Archive cannot be a symlink")
    if archive.exists():
        content = archive.read_bytes()
    else:
        request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "PatchTrail-benchmark"})
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise ValueError("QuixBugs archive exceeds the download limit")
    if _sha(content) != ARCHIVE_SHA256:
        raise ValueError("QuixBugs archive SHA256 mismatch")
    _write_once(archive, content)
    root = _safe_directory(directory / f"QuixBugs-{UPSTREAM_COMMIT}")
    prefix = f"QuixBugs-{UPSTREAM_COMMIT}/"
    total = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
        for member in bundle:
            if member.name == prefix.rstrip("/") and member.isdir():
                continue
            if not member.name.startswith(prefix):
                raise ValueError("Unexpected QuixBugs archive root")
            relative = member.name[len(prefix):]
            if not _is_input(relative):
                continue
            parts = PurePosixPath(relative).parts
            if (not member.isfile() or any(part in {"", ".", ".."} for part in parts)
                    or PurePosixPath(relative).is_absolute()):
                raise ValueError("Unexpected QuixBugs input member")
            total += member.size
            if member.size > 128 * 1024 or total > MAX_ARCHIVE_BYTES:
                raise ValueError("QuixBugs input size exceeds its limit")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Missing QuixBugs input member")
            _write_once(root / relative, source.read())
    _input_inventory(root)
    return root


def adapt_test(content):
    """Retain upstream assertions/data while selecting only the buggy namespace."""
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)
    selections = [node for node in tree.body if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "pytest.use_correct"]
    if len(selections) != 1 or len(selections[0].orelse) != 1:
        raise ValueError("Expected exactly one upstream correct/buggy import switch")
    selection = selections[0]
    branch = selection.orelse[0]
    if not isinstance(branch, ast.ImportFrom) or not branch.module.startswith("python_programs."):
        raise ValueError("Unexpected upstream program import")
    replacement = "".join(line[4:] if line.startswith("    ") else line
                          for line in lines[branch.lineno - 1:branch.end_lineno])
    lines[selection.lineno - 1:selection.end_lineno] = [replacement]
    converted = "".join(lines)
    converted = converted.replace("from load_testdata import ", "from python_testcases.load_testdata import ")
    converted = converted.replace("from node import Node", "from python_testcases.node import Node")
    converted = converted.replace("pytest.run_slow", "False")
    if "correct_python_programs" in converted or "pytest.use_correct" in converted:
        raise ValueError("Correct-program import survived test conversion")
    ast.parse(converted)
    return converted


def _external_dependencies(contents):
    allowed = set(sys.stdlib_module_names) | {"pytest", "python_programs", "python_testcases"}
    imports = set()
    for content in contents:
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    return sorted(imports - allowed)


def prepare_dataset(data_dir, upstream=None):
    """Return deterministic task records with absolute source/manifest paths.

    An offline ``upstream`` directory is accepted only if its relevant-file
    inventory matches the pinned revision. Every task still needs a container
    preflight; conversion does not claim its original tests fail.
    """
    data_dir = _safe_directory(data_dir)
    upstream = download_upstream(data_dir) if upstream is None else Path(upstream).resolve()
    inventory = _input_inventory(upstream)
    base = _safe_directory(data_dir / "quixbugs")
    tasks, skipped = [], []
    for name in TASK_NAMES:
        original_test = f"python_testcases/test_{name}.py"
        original_source = f"python_programs/{name}.py"
        original_test_content = (upstream / original_test).read_text(encoding="utf-8")
        test_content = adapt_test(original_test_content)
        source_content = (upstream / original_source).read_text(encoding="utf-8")
        adaptations = ["select python_programs import; remove correct-program branch",
                       "qualify local helper imports for the fixed pytest importlib profile"]
        if "pytest.run_slow" in original_test_content:
            adaptations.append("replace pytest.run_slow with False, preserving default slow-test skip")
        files = {original_source: source_content.encode(), original_test: test_content.encode(),
                 "python_programs/__init__.py": b"", "python_testcases/__init__.py": b"",
                 "LICENSE": (upstream / "LICENSE").read_bytes(),
                 "LEGAL_NOTES.txt": (upstream / "legal_notes.txt").read_bytes()}
        if "python_testcases.load_testdata" in test_content:
            files["python_testcases/load_testdata.py"] = (upstream / "python_testcases/load_testdata.py").read_bytes()
            files[f"json_testcases/{name}.json"] = (upstream / f"json_testcases/{name}.json").read_bytes()
        if "python_testcases.node" in test_content:
            files["python_testcases/node.py"] = (upstream / "python_testcases/node.py").read_bytes()
        dependencies = _external_dependencies([content.decode("utf-8")
                                               for relative, content in files.items()
                                               if relative.endswith(".py")])
        if dependencies:
            skipped.append({"name": name, "reason": "unsupported_dependency",
                            "dependencies": dependencies})
            continue
        task_dir = _safe_directory(base / "tasks" / name)
        source_dir = _safe_directory(task_dir / "source")
        for relative, content in files.items():
            _write_once(source_dir / relative, content, editable=relative == original_source)
        manifest = {"schema_version": 1, "project_name": "quixbugs-" + name.replace("_", "-"),
                    "title": f"QuixBugs Python: {name}",
                    "description": "Repair the defect in the editable Python program so that the existing read-only upstream tests pass. Preserve the function interface and do not modify tests or support files.",
                    "files": sorted(files), "editable_files": [original_source],
                    "check_profile": "pytest", "test_paths": [original_test],
                    "tags": ["benchmark", "quixbugs", "python"],
                    "provenance": f"{UPSTREAM_REPOSITORY}/tree/{UPSTREAM_COMMIT}; MIT, Copyright 2017-2019 James Koppel; upstream legal_notes.txt retained; original test assertions/data unchanged; no corrected program supplied."}
        manifest_path = task_dir / "manifest.json"
        _write_once(manifest_path, _canonical(manifest) + b"\n")
        task_inventory = {relative: _sha(content) for relative, content in sorted(files.items())}
        tasks.append({"name": name, "source_dir": str(source_dir), "manifest_path": str(manifest_path),
                      "manifest": manifest, "adaptations": adaptations, "files_sha256": task_inventory,
                      "upstream_source_sha256": inventory[original_source],
                      "upstream_test_sha256": inventory[original_test]})
    result = {"schema_version": 1, "upstream": {"repository": UPSTREAM_REPOSITORY,
              "commit": UPSTREAM_COMMIT, "archive_url": ARCHIVE_URL, "archive_sha256": ARCHIVE_SHA256,
              "input_inventory_sha256": INPUT_SHA256, "license": "MIT",
              "license_url": f"{UPSTREAM_REPOSITORY}/blob/{UPSTREAM_COMMIT}/LICENSE",
              "legal_notes_url": f"{UPSTREAM_REPOSITORY}/blob/{UPSTREAM_COMMIT}/legal_notes.txt"},
              "tasks": tasks, "skipped": skipped,
              "policy": "Upstream default tests; no corrected programs; container preflight required; conversion executes no source"}
    _write_once(base / "dataset.json", _canonical(result) + b"\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, help="Offline pinned upstream tree, verified before use")
    args = parser.parse_args(argv)
    result = prepare_dataset(args.data_dir, args.upstream)
    print(json.dumps({"commit": UPSTREAM_COMMIT, "converted": len(result["tasks"]),
                      "skipped": result["skipped"], "dataset": str(args.data_dir / "quixbugs/dataset.json")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
