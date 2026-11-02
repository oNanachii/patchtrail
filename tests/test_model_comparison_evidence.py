"""Offline frozen-cohort and cost evidence checks; never dispatch a model."""
import copy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.check_model_comparison import (BASELINE_NAME, BASELINE_SHA256, ENDPOINT, LIMITS,
    MODEL, actual_oci, normalize_usage, pricing_rates, verify)


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "artifacts" / BASELINE_NAME


def preparation_record():
    seed = json.loads(BASELINE.read_text())
    return {"state": "preparing", "baseline_reference": {"artifact": BASELINE_NAME,
        "sha256": BASELINE_SHA256}, "calls_artifact": "comparison-calls.json", "cap_yuan": 30,
        "dataset": copy.deepcopy(seed["dataset"]),
        "environment": {"model": MODEL, "endpoint": ENDPOINT, "image": seed["environment"]["image"],
                        "limits": dict(LIMITS)},
        "cases": [{"name": case["name"], "eligible": case["eligible"],
                   "seed_source_hash": case["source_hash"], "source_hash": case["source_hash"]}
                  for case in seed["cases"]]}


def audit_copy(record, ledger=None, baseline_bytes=None, filename="model-comparison-test.json"):
    with TemporaryDirectory(prefix="comparison-evidence-") as temporary:
        directory = Path(temporary)
        path = directory / filename
        path.write_text(json.dumps(record))
        (directory / BASELINE_NAME).write_bytes(BASELINE.read_bytes() if baseline_bytes is None else baseline_bytes)
        if ledger is not None:
            (directory / record["calls_artifact"]).write_text(json.dumps(ledger))
        try:
            return verify(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
            return {"passed": False, "errors": [type(exc).__name__ + ": " + str(exc)]}


class ModelComparisonEvidenceUnitTests(unittest.TestCase):
    def test_zero_dispatch_preparation_keeps_exact_p1_cohort_and_exclusions(self):
        result = audit_copy(preparation_record())
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual((result["checked_tasks"], result["frozen_eligible_tasks"], result["checked_calls"]),
                         (40, 34, 0))
        self.assertTrue(result["dry_run_only"])

    def test_cohort_reference_and_readonly_data_cannot_be_reselected(self):
        original = preparation_record()
        modified = []
        record = copy.deepcopy(original)
        record["cases"][0]["eligible"] = True
        record["cases"][1]["eligible"] = False
        modified.append(record)
        record = copy.deepcopy(original)
        record["cases"].pop(0)
        modified.append(record)
        record = copy.deepcopy(original)
        task = record["dataset"]["tasks"][1]
        relative = next(path for path in task["files_sha256"] if "test_" in path)
        task["files_sha256"][relative] = "0" * 64
        modified.append(record)
        record = copy.deepcopy(original)
        record["baseline_reference"]["sha256"] = "1" * 64
        modified.append(record)
        for record in modified:
            self.assertFalse(audit_copy(record)["passed"])
        self.assertFalse(audit_copy(original, baseline_bytes=BASELINE.read_bytes() + b"\n")["passed"])

    def test_preparation_cannot_claim_a_paid_attempt_without_ledger(self):
        for change in ({"state": "completed"}, {"state": "dry_run_only"}, {"cost": {"dispatched_calls": 1}}):
            record = preparation_record()
            record.update(change)
            self.assertFalse(audit_copy(record)["passed"])
        record = preparation_record()
        record["cases"][1]["agent"] = {"model_calls": 1}
        self.assertFalse(audit_copy(record)["passed"])

    def test_model_budget_image_and_endpoint_are_frozen(self):
        for field, value in (("image", "a" * 64), ("model", "another-model"),
                             ("endpoint", "https://example.invalid/chat/completions"),
                             ("limits", {**LIMITS, "max_calls": 9})):
            record = preparation_record()
            record["environment"][field] = value
            self.assertFalse(audit_copy(record)["passed"])
        record = preparation_record()
        record["cap_yuan"] = 30.01
        self.assertFalse(audit_copy(record)["passed"])

    def test_official_pricing_tier_edges_and_cached_token_accounting(self):
        self.assertEqual(pricing_rates(32767, 199),
                         {"cache_hit": Decimal("0.4"), "cache_miss": Decimal(2), "output": Decimal(8)})
        self.assertEqual(pricing_rates(32767, 200)["output"], Decimal(14))
        self.assertEqual(pricing_rates(32768, 1)["output"], Decimal(16))
        usage = normalize_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 10,
            "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 20}}})
        self.assertEqual((usage["prompt_cache_hit_tokens"], usage["prompt_cache_miss_tokens"]), (20, 80))
        for details in (None, {}, {"cached_tokens": None}):
            raw = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                   "prompt_tokens_details": details, "prompt_cache_hit_tokens": None,
                   "prompt_cache_miss_tokens": None}
            self.assertEqual(normalize_usage({"usage": raw})["prompt_cache_hit_tokens"], 0)
        raw["prompt_tokens_details"] = {"cached_tokens": 20}
        self.assertEqual(normalize_usage({"usage": raw})["prompt_cache_hit_tokens"], 20)
        for raw in ({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 109},
                    {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                     "prompt_tokens_details": {"cached_tokens": True}},
                    {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                     "prompt_cache_hit_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}},
                    {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                     "prompt_cache_hit_tokens": None, "prompt_tokens_details": [],
                     "prompt_cache_miss_tokens": None},
                    {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                     "prompt_tokens_details": {"cached_tokens": 20}, "prompt_cache_miss_tokens": 100}):
            with self.assertRaises(ValueError):
                normalize_usage({"usage": raw})
        self.assertFalse(actual_oci({"runner": "explicit-fake-podman"}))


class SavedModelComparisonEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = None
        for path in sorted((ROOT / "artifacts").glob("*.json"), reverse=True):
            if not any(fragment in path.name for fragment in ("comparison", "zhipu", "glm")):
                continue
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (record.get("state") == "completed" and "baseline_reference" in record
                    and record.get("environment", {}).get("model") == MODEL):
                cls.record, cls.path = record, path
                break
        if cls.record is None:
            raise unittest.SkipTest("Real second-model comparison has not yet been collected")
        cls.ledger = json.loads((cls.path.parent / cls.record["calls_artifact"]).read_text())

    def audit(self, record=None, ledger=None):
        return audit_copy(copy.deepcopy(self.record if record is None else record),
                          copy.deepcopy(self.ledger if ledger is None else ledger), filename=self.path.name)

    def test_saved_run_reconciles_without_dispatch_or_task_execution(self):
        result = self.audit()
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual((result["checked_tasks"], result["frozen_eligible_tasks"]), (40, 34))

    def test_usage_tariff_and_provider_identity_cannot_be_invented(self):
        for field, value in (("tariff_yuan", 0), ("conservative_peak_yuan", 0)):
            ledger = copy.deepcopy(self.ledger)
            ledger["calls"][0][field] = value
            self.assertFalse(self.audit(ledger=ledger)["passed"])
        ledger = copy.deepcopy(self.ledger)
        ledger["calls"][0]["response"]["id"] = "changed-provider-identity"
        self.assertFalse(self.audit(ledger=ledger)["passed"])

    def test_call_ownership_and_unresolved_budget_cannot_disappear(self):
        record = copy.deepcopy(self.record)
        entry = next(case["agent"] for case in record["cases"] if case.get("agent", {}).get("call_indexes"))
        entry["call_indexes"].pop()
        self.assertFalse(self.audit(record=record)["passed"])
        ledger = copy.deepcopy(self.ledger)
        ledger["calls"][0]["status"] = "unknown"
        self.assertFalse(self.audit(ledger=ledger)["passed"])

    def test_exact_approval_and_actual_test_completion_are_required(self):
        approved = next((case["name"] for case in self.record["cases"]
                         if case.get("agent", {}).get("approval")), None)
        if approved is None:
            self.skipTest("No approved proposal occurred in the real comparison")
        for alteration in ("digest", "version", "verifier"):
            record = copy.deepcopy(self.record)
            entry = next(case["agent"] for case in record["cases"] if case["name"] == approved)
            if alteration == "digest":
                entry["approval"]["proposal_digest"] = "0" * 64
            elif alteration == "version":
                entry["approval"]["expected_version"] -= 1
            else:
                entry["run"]["state"] = entry["status"] = "succeeded"
                entry["run"]["result"] = {"passed": True, "complete": False}
            self.assertFalse(self.audit(record=record)["passed"])

    def test_source_and_readonly_workspace_hashes_are_checked(self):
        record = copy.deepcopy(self.record)
        case = next(case for case in record["cases"] if case.get("agent"))
        mapping = case["agent"]["immutability"]["snapshot"]
        relative = next(path for path in mapping if "test_" in path)
        mapping[relative] = "0" * 64
        self.assertFalse(self.audit(record=record)["passed"])


if __name__ == "__main__":
    unittest.main()
