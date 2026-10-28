"""Actual OCI integration. Scripted agent actions are not model-effectiveness data."""
import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from patchtrail.config import Settings
from patchtrail.service import RunService
from patchtrail.tasks import list_tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    base = Path(os.environ.get("PATCHTRAIL_DATA_DIR", "/tmp"))
    base.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory(prefix="patchtrail-oci-", dir=base) as temporary:
        settings = Settings(data_dir=Path(temporary), api_key="", admin_token="")
        service = RunService(settings, dispatch=False)
        try:
            if not service.runner.available():
                raise RuntimeError("OCI runtime unavailable; no host fallback")
            for task in list_tasks():
                row = service.create(task["id"], "demo", "oci-check-" + task["id"])
                service.diagnose(row["id"])
                row = service.store.get_run(row["id"])
                record = {"task_id": task["id"], "task_source_hash": task["source_hash"],
                          "mode": "scripted demonstration", "model_called": False,
                          "diagnosis_state": row["state"]}
                if row["state"] == "awaiting_approval":
                    baseline = row["checkpoint"]["baseline_check"]
                    record["baseline"] = baseline
                    service.approve(row["id"], row["proposal"]["digest"], row["version"])
                    service.verify(row["id"])
                    row = service.store.get_run(row["id"])
                else:
                    record["events"] = service.store.events(row["id"])
                record["final_state"] = row["state"]
                record["result"] = row["result"]
                results.append(record)
        finally:
            service.close()
    passed = bool(results) and all(row["final_state"] == "succeeded" and
              row.get("baseline", {}).get("exit_code") != 0 and
              row.get("baseline", {}).get("tests_run", 0) > 0 for row in results)
    report = {"status": "passed" if passed else "failed",
              "scope": "scripted provider through real OCI baseline, approval and fresh verification",
              "model_effectiveness_evaluated": False, "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
