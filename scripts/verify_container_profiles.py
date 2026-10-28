"""Actual OCI checks for fixed pytest profiles; no model or credential access."""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchtrail.check_profiles import build_check_command
from patchtrail.config import runtime_code_hash
from patchtrail.sandbox import DockerRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--binary", default="podman")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    runner = DockerRunner(args.binary, args.image)
    results = []
    cases = [
        ("passed", "def test_one(): assert 2 + 2 == 4\ndef test_two(): assert 'a'.upper() == 'A'\n", True, True, 2),
        ("failed", "def test_failure(): assert 2 + 2 == 5\n", True, False, 1),
        ("zero", "# intentionally no test functions\n", False, False, 0),
        ("all_skipped", "import pytest\n@pytest.mark.skip(reason='profile evidence')\ndef test_skipped(): pass\n", False, False, 1),
    ]
    with TemporaryDirectory(prefix="patchtrail-real-profiles-") as temporary:
        for name, source, complete, passed, count in cases:
            directory = Path(temporary) / name
            (directory / "tests").mkdir(parents=True)
            (directory / "tests/test_cases.py").write_text(source)
            directory.chmod(0o755)
            result = runner.run(directory, build_check_command("pytest", ["tests"]), "profile-" + name).to_dict()
            if (result["complete"], result["passed"], result["tests_run"]) != (complete, passed, count):
                raise RuntimeError("Actual profile result differs for " + name + ": " + json.dumps(result))
            results.append({"case": name, **result})
    record = {"status": "passed", "scope": "actual rootless OCI pytest execution", "model_called": False,
              "image": args.image, "runtime_code_hash": runtime_code_hash(), "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "actual_container_cases": len(results), "model_called": False}))


if __name__ == "__main__":
    main()
