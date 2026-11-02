"""Offline audit of the frozen P1 cohort and the Zhipu Agent-only comparison.

No task code, provider request, approval, or container is executed here. This is
an evidence-consistency check, not an attestation against hostile Python.
"""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path


BASELINE_NAME = "benchmark-quixbugs-20261101T170923Z-99c250d0.json"
BASELINE_SHA256 = "a4ea58ce0803ce6e8f901ae3f8d2f70d66d415572ababd2acb6dbbc1aba58de0"
MODEL = "glm-4.7"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
TOOL_SCHEMA_SHA256 = "afbc2d394a0bfbd2687e7fa2aa566093f4d97d9595b01525a35de892239700aa"
SYSTEM_MESSAGE_SHA256 = "d66dbbd56867afd9dcedd4f72ccb9132bc437186d6193d0c2c610411980e0fc7"
USAGE = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
         "prompt_cache_miss_tokens")
LIMITS = {"max_calls": 8, "max_steps": 12, "max_seconds": 120,
          "max_input_chars": 64000, "max_output_tokens": 4096}
MILLION = Decimal(1000000)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def pricing_rates(prompt_tokens, completion_tokens):
    """Official GLM-4.7 CNY tiers, with the documented 0.2K read as 200 tokens."""
    if prompt_tokens >= 32768:
        rates = ("0.8", "4", "16")
    elif completion_tokens >= 200:
        rates = ("0.6", "3", "14")
    else:
        rates = ("0.4", "2", "8")
    return dict(zip(("cache_hit", "cache_miss", "output"), map(Decimal, rates)))


def normalize_usage(body):
    raw = body["usage"]
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(raw, dict) or any(type(raw.get(key)) is not int or raw[key] < 0 for key in fields):
        raise ValueError("Response usage is incomplete")
    details = raw.get("prompt_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise ValueError("Invalid prompt token details")
    details = details if details is not None else {}
    candidates = [value for value in (raw.get("prompt_cache_hit_tokens"), details.get("cached_tokens"))
                  if value is not None]
    if any(type(value) is not int or not 0 <= value <= raw["prompt_tokens"] for value in candidates):
        raise ValueError("Invalid cached token count")
    if len(set(candidates)) > 1:
        raise ValueError("Cached usage fields disagree")
    hit = candidates[0] if candidates else 0
    if type(hit) is not int or not 0 <= hit <= raw["prompt_tokens"]:
        raise ValueError("Invalid cached token count")
    if raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]:
        raise ValueError("Usage does not add up")
    miss = raw["prompt_tokens"] - hit
    if raw.get("prompt_cache_miss_tokens") is not None and (
            type(raw["prompt_cache_miss_tokens"]) is not int or raw["prompt_cache_miss_tokens"] != miss):
        raise ValueError("Cache-miss usage fields disagree")
    return {**{key: raw[key] for key in fields}, "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss}


def actual_oci(check):
    runner = str((check or {}).get("runner", "")).lower()
    return (any(name in runner for name in ("podman", "docker"))
            and not any(name in runner for name in ("fake", "mock", "inspection")))


def complete_check(check, success):
    if not isinstance(check, dict):
        return False
    footer = check.get("completion_report") or {}
    return bool(actual_oci(check) and check.get("complete") is True
        and check.get("passed") is success and check.get("exit_code") == (0 if success else 1)
        and type(check.get("tests_run")) is int and check["tests_run"] > 0
        and not check.get("timed_out") and not check.get("truncated")
        and footer.get("status") == "complete" and footer.get("schema_version") == 1
        and footer.get("framework_returned") is True and footer.get("session_finished") is True
        and footer.get("valid") is True and footer.get("passed") is success
        and footer.get("framework") == check.get("framework") == "pytest"
        and all(type(footer.get(key)) is int and footer[key] >= 0 for key in ("tests", "failed", "skipped"))
        and (footer["failed"] == 0 if success else footer["failed"] > 0)
        and footer.get("tests") == check["tests_run"] and footer.get("exit_code") == check["exit_code"])


def verify(path):
    path = Path(path)
    record = json.loads(path.read_text())
    errors = []

    def require(condition, label):
        if not condition:
            errors.append(label)

    def equal(actual, expected, label):
        if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
            try:
                valid = abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal("0.000000001")
            except (ValueError, TypeError, ArithmeticError):
                valid = False
        else:
            valid = actual == expected
        require(valid, label)

    def sibling(name):
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Evidence references must be sibling filenames")
        return path.parent / name

    reference = record["baseline_reference"]
    baseline_path = sibling(reference["artifact"])
    equal(reference["artifact"], BASELINE_NAME, "Reference is not the frozen P1 run")
    equal(reference["sha256"], BASELINE_SHA256, "Reference digest is not the frozen P1 digest")
    equal(hashlib.sha256(baseline_path.read_bytes()).hexdigest(), BASELINE_SHA256,
          "Frozen P1 file bytes changed")
    baseline = json.loads(baseline_path.read_text())
    seeds = {case["name"]: case for case in baseline["cases"]}
    seed_tasks = {task["name"]: task for task in baseline["dataset"]["tasks"]}
    cohort = [case["name"] for case in baseline["cases"] if case["eligible"]]
    equal(len(cohort), 34, "P1 cohort is not 34 tasks")
    equal(record["dataset"]["upstream"], baseline["dataset"]["upstream"], "Upstream pin changed")
    tasks = record["dataset"]["tasks"]
    cases = record["cases"]
    equal(len(tasks), 40, "Dataset lost an original task")
    equal({task["name"] for task in tasks}, set(seed_tasks), "Dataset task names changed")
    equal(len(cases), 40, "Case records lost an original task")
    equal([case["name"] for case in cases], [case["name"] for case in baseline["cases"]],
          "Case order or frozen task identities changed")
    for task in tasks:
        seed = seed_tasks[task["name"]]
        for key in ("manifest", "files_sha256", "upstream_source_sha256", "upstream_test_sha256"):
            equal(task[key], seed[key], task["name"] + " dataset " + key)
    if "frozen_eligible_names" in record:
        equal(record["frozen_eligible_names"], cohort, "Frozen cohort changed")
    environment = record["environment"]
    equal(environment["model"], MODEL, "Unexpected model")
    equal(environment["endpoint"], ENDPOINT, "Unexpected official API endpoint")
    equal(environment["image"], baseline["environment"]["image"], "P1 container image changed")
    equal(environment["limits"], LIMITS, "P1 Agent budget changed")
    cap = Decimal(str(record["cap_yuan"]))
    require(cap.is_finite() and 0 < cap <= 30, "Spending cap exceeds CNY 30")
    for case in cases:
        name = case["name"]
        equal(case["eligible"], seeds[name]["eligible"], name + " eligibility reselected")
        equal(case["seed_source_hash"], seeds[name]["source_hash"], name + " seed source changed")
        equal(case["source_hash"], seeds[name]["source_hash"], name + " source differs from P1")
        require("baseline" not in case, name + " unexpectedly reran the single-shot baseline")
        if not case["eligible"]:
            require("agent" not in case, name + " attempted a frozen P1 exclusion")
        elif record["state"] == "dry_run_only":
            preflight = case.get("preflight") or {}
            require(bool(preflight), name + " missing fresh dry-run preflight")
            if case.get("not_run_reason"):
                require(not preflight.get("eligible") or
                        preflight.get("check", {}).get("tests_run") != seeds[name]["preflight"]["check"]["tests_run"],
                        name + " unsupported dry-run exclusion")
            else:
                require(preflight.get("eligible") is True and preflight.get("status") == "ready"
                        and complete_check(preflight.get("check"), False), name + " invalid dry-run failure evidence")
                equal(preflight.get("source_hash"), seeds[name]["source_hash"], name + " dry-run source changed")
                equal(preflight.get("check", {}).get("tests_run"), seeds[name]["preflight"]["check"]["tests_run"],
                      name + " dry-run test count changed")
    calls_path = sibling(record["calls_artifact"])
    if not calls_path.exists():
        require(record["state"] in {"dry_run_only", "preparing", "aborted"}, "Paid ledger is missing")
        require(not any("agent" in case for case in cases), "Agent attempts exist without a ledger")
        require((record.get("cost") or {}).get("dispatched_calls", 0) == 0, "Missing ledger hid paid calls")
        return {"passed": not errors, "errors": errors, "artifact": path.name,
                "checked_tasks": len(cases), "frozen_eligible_tasks": len(cohort),
                "agent_attempts": 0, "checked_calls": 0, "dry_run_only": True,
                "scope": "Offline frozen dataset and zero-dispatch preparation audit"}

    ledger = json.loads(calls_path.read_text())
    equal(ledger["model"], MODEL, "Ledger model differs")
    equal(ledger["endpoint"], ENDPOINT, "Ledger endpoint differs")
    equal(ledger["cap_yuan"], cap, "Record and ledger cap differ")
    equal(ledger["maximum_cap_yuan"], 30, "Explicit maximum authorization changed")
    configuration = ledger["configuration"]
    equal(sha(configuration), ledger["configuration_sha256"], "Ledger profile fingerprint changed")
    equal(configuration.get("model"), MODEL, "Profile model changed")
    equal(configuration.get("endpoint"), ENDPOINT, "Profile endpoint changed")
    equal(configuration.get("cap_yuan"), str(cap.normalize()), "Profile cap changed")
    equal(configuration.get("thinking"), {"type": "disabled"}, "Profile thinking mode changed")
    equal(configuration.get("maximum_output_tokens"), 4096, "Profile output allowance changed")
    equal(configuration.get("pricing"), ledger["pricing"], "Profile pricing differs from ledger")
    equal(record["pricing"], ledger["pricing"], "Record pricing differs from ledger")
    pricing = ledger["pricing"]
    equal(pricing.get("source"), "https://docs.bigmodel.cn/cn/guide/start/pricing", "Unexpected pricing source")
    equal(pricing.get("currency"), "CNY", "Unexpected pricing currency")
    equal(pricing.get("prompt_threshold_tokens"), 32768, "Prompt price tier changed")
    equal(pricing.get("short_output_threshold_tokens"), 200, "Output price tier changed")
    expected_tiers = [
        {"prompt_lt": 32768, "completion_lt": 200, "cache_hit": "0.4", "cache_miss": "2", "output": "8"},
        {"prompt_lt": 32768, "completion_ge": 200, "cache_hit": "0.6", "cache_miss": "3", "output": "14"},
        {"prompt_ge": 32768, "cache_hit": "0.8", "cache_miss": "4", "output": "16"}]
    equal(pricing.get("tiers"), expected_tiers, "Documented price tiers changed")
    calls = ledger["calls"]
    amounts, response_ids = {}, set()
    tariff_total, conservative, reserved, unknown = Decimal(0), Decimal(0), Decimal(0), 0
    for index, call in enumerate(calls, 1):
        label = f"call {index}"
        equal(call["index"], index, label + " index")
        require(not unknown, label + " dispatched after an unresolved request")
        request = call["request"]
        equal(request["model"], MODEL, label + " model")
        require(type(request.get("max_tokens")) is int and 0 < request["max_tokens"] <= 4096,
                label + " output allowance")
        equal(sha(request), call["request_sha256"], label + " request digest")
        equal(sha(request["tools"]), TOOL_SCHEMA_SHA256, label + " tool schema changed from P1")
        equal(sha(request["messages"][0]), SYSTEM_MESSAGE_SHA256, label + " system prompt changed from P1")
        equal(request.get("tool_choice"), "auto", label + " tool choice changed")
        equal(request.get("stream"), False, label + " streaming changed")
        estimate = ((len(canonical(request)) + 4096) * Decimal(4)
                    + request["max_tokens"] * Decimal(16)) / MILLION
        equal(call["estimate"]["reservation_peak_yuan"], estimate, label + " reservation")
        require(conservative + reserved + estimate <= cap, label + " reservation exceeded cap")
        status = call["status"]
        require(status in {"confirmed", "unknown", "pending"}, label + " invalid status")
        usage, tariff = Counter(), Decimal(0)
        if status == "confirmed":
            body = call["response"]
            response_id = body.get("id")
            require(isinstance(response_id, str) and response_id and response_id not in response_ids,
                    label + " missing or duplicate provider response identity")
            response_ids.add(response_id)
            usage.update(normalize_usage(body))
            require(usage["completion_tokens"] <= request["max_tokens"], label + " output exceeded cap")
            rates = pricing_rates(usage["prompt_tokens"], usage["completion_tokens"])
            tariff = (usage["prompt_cache_hit_tokens"] * rates["cache_hit"]
                      + usage["prompt_cache_miss_tokens"] * rates["cache_miss"]
                      + usage["completion_tokens"] * rates["output"]) / MILLION
            worst = (usage["prompt_tokens"] * Decimal(4)
                     + usage["completion_tokens"] * Decimal(16)) / MILLION
            for key in USAGE:
                equal(call["usage"].get(key), usage[key], label + " usage " + key)
            equal(call["tariff_yuan"], tariff, label + " tariff")
            equal(call["conservative_peak_yuan"], worst, label + " conservative charge")
            require(worst <= estimate, label + " usage exceeded reservation")
            if "tariff_rates_yuan_per_million" in call:
                for key in rates:
                    equal(call["tariff_rates_yuan_per_million"].get(key), rates[key], label + " tier " + key)
            tariff_total += tariff
            conservative += worst
        else:
            unknown += 1
            reserved += estimate
        amounts[index] = {"usage": usage, "tariff": tariff, "unknown": status != "confirmed"}
    expected_cost = {"dispatched_calls": len(calls), "confirmed_calls": len(calls) - unknown,
        "unknown_calls": unknown, "tariff_yuan": tariff_total, "conservative_peak_yuan": conservative,
        "unresolved_reserved_yuan": reserved, "cap_liability_yuan": conservative + reserved,
        "cap_yuan": cap, "cost_is_tariff_estimate": True}
    for owner, summary in (("ledger", ledger["summary"]), ("record", record["cost"])):
        for key, value in expected_cost.items():
            equal(summary.get(key), value, owner + " cost " + key)
    require(conservative + reserved <= cap, "Final cost liability exceeded cap")
    if unknown:
        require(record["state"] != "completed" and ledger["summary"].get("stopped") is True,
                "Unresolved request did not stop the batch")

    seen, run_ids, rows = [], set(), []
    for case in cases:
        name = case["name"]
        if not case["eligible"]:
            continue
        if "agent" not in case:
            require(record["state"] != "completed" or bool(case.get("not_run_reason")),
                    name + " silently missing an Agent result")
            continue
        entry, seed = case["agent"], seeds[name]
        run, events = entry["run"], entry["events"]
        label = name + "/agent"
        require(run["id"] not in run_ids, label + " duplicated run attempt")
        run_ids.add(run["id"])
        equal(entry.get("method"), "agent", label + " method")
        equal(run["mode"], "live", label + " not a real model run")
        equal(entry["status"], run["state"], label + " status")
        equal(run["limits"], LIMITS, label + " budget")
        checkpoint = run["checkpoint"]
        require(type(checkpoint.get("calls")) is int and type(checkpoint.get("steps")) is int
                and 0 <= checkpoint["calls"] <= 8 and 0 <= checkpoint["steps"] <= 12,
                label + " exceeded call/step budget")
        preflight = case["preflight"]
        require(preflight.get("eligible") is True and preflight.get("status") == "ready"
                and complete_check(preflight.get("check"), False), label + " missing actual failing baseline")
        equal(preflight["source_hash"], seed["source_hash"], label + " baseline source changed")
        equal(preflight["check"]["tests_run"], seed["preflight"]["check"]["tests_run"], label + " baseline test count changed")
        indexes = entry["call_indexes"]
        require(len(indexes) == len(set(indexes)), label + " repeated request ownership")
        seen.extend(indexes)
        usage, tariff, unknown_for_run = Counter(), Decimal(0), 0
        responses = {event["payload"]["call"]: event["payload"] for event in events
                     if event["kind"] == "model_response"}
        requested = [event["payload"]["call"] for event in events if event["kind"] == "model_requested"]
        equal(requested, list(range(1, checkpoint.get("calls", 0) + 1)), label + " model request events")
        assistants = [message for message in checkpoint.get("messages", []) if message.get("role") == "assistant"]
        equal(len(assistants), len(responses), label + " assistant trace differs from events")
        for ordinal, index in enumerate(indexes, 1):
            require(index in amounts, label + " nonexistent ledger request")
            if index not in amounts:
                continue
            call, actual = calls[index - 1], amounts[index]
            equal(call["context"].get("task"), "QuixBugs Python: " + name, label + " request task")
            equal(call["context"].get("method"), "agent", label + " request method")
            equal(call["context"].get("call"), ordinal, label + " request ordinal")
            if ordinal in responses and call["status"] == "confirmed":
                observed, raw = responses[ordinal], call["response"]
                equal(observed.get("response_id"), raw.get("id"), label + " response identity")
                equal(observed.get("reported_model"), raw.get("model"), label + " reported model")
                raw_message = raw["choices"][0]["message"]
                assistant_index = sorted(responses).index(ordinal)
                if assistant_index < len(assistants):
                    stored = assistants[assistant_index]
                    expected_message = {"role": "assistant", "content": raw_message.get("content")}
                    for key in ("tool_calls", "reasoning_content"):
                        if key in stored:
                            expected_message[key] = raw_message.get(key)
                    equal(stored, expected_message, label + " assistant message changed")
            usage.update(actual["usage"])
            tariff += actual["tariff"]
            unknown_for_run += actual["unknown"]
        equal(entry["model_calls"], len(indexes), label + " call count")
        equal(entry["usage_complete"], unknown_for_run == 0, label + " usage completeness")
        equal(entry["unknown_calls"], unknown_for_run, label + " unknown calls")
        for key in set(usage) | set(entry["usage"]):
            equal(entry["usage"].get(key, 0), usage[key], label + " total usage " + key)
        equal(entry["tariff_yuan"], tariff, label + " total cost")
        require(isinstance(entry["elapsed_seconds"], (int, float))
                and math.isfinite(entry["elapsed_seconds"]) and entry["elapsed_seconds"] >= 0,
                label + " invalid elapsed time")
        fixed = run["state"] == "succeeded" and complete_check(run.get("result"), True)
        equal(run["state"] == "succeeded", fixed, label + " claimed success without actual verifier")
        proposal, approval = run.get("proposal"), entry.get("approval")
        if approval:
            require(bool(proposal), label + " approval without proposal")
            if proposal:
                payload = {key: value for key, value in proposal.items() if key not in {"digest", "explanation"}}
                equal(sha(payload), proposal["digest"], label + " proposal digest")
                equal(proposal["source_hash"], seed["source_hash"], label + " proposal source")
                equal(proposal["base_workspace_hash"], seed["source_hash"], label + " proposal workspace")
                equal(proposal["task_id"], run["task_id"], label + " proposal identity")
                files = proposal.get("files", [proposal])
                require(1 <= len(files) <= 3 and len({item["path"] for item in files}) == len(files),
                        label + " invalid patch file count")
                for item in files:
                    require(item["path"] in seed_tasks[name]["manifest"]["editable_files"], label + " changed a read-only file")
                    equal(item["old_sha256"], seed_tasks[name]["files_sha256"].get(item["path"]), label + " original file digest")
                    equal(item["new_sha256"], hashlib.sha256(item["content"].encode()).hexdigest(), label + " replacement digest")
                approved = checkpoint.get("approval") or {}
                for key, expected in {"actor": "脚本自动批准", "human_review": False,
                    "endpoint": "POST /api/runs/{run_id}/approve", "http_status": 200,
                    "proposal_digest": proposal["digest"], "expected_version": approved.get("from_version")}.items():
                    equal(approval.get(key), expected, label + " approval " + key)
                equal(approved.get("digest"), proposal["digest"], label + " checkpoint digest")
                transitions = [event for event in events if event["kind"] == "state_changed"
                               and event["payload"].get("from") == "awaiting_approval"
                               and event["payload"].get("to") == "verifying"]
                require(len(transitions) == 1 and transitions[0]["payload"].get("version") == approved.get("from_version", -2) + 1,
                        label + " missing exact approval version transition")
        require(not fixed or bool(approval), label + " successful without approval")
        if fixed:
            equal(run["result"]["tests_run"], seed["preflight"]["check"]["tests_run"], label + " verification test count changed")
        require(entry.get("original_snapshot_unchanged") is True, label + " original snapshot changed")
        immutability = entry["immutability"]
        readonly = {key: value for key, value in seed_tasks[name]["files_sha256"].items()
                    if key not in seed_tasks[name]["manifest"]["editable_files"]}
        maps = {"snapshot": immutability["snapshot"], **immutability["workspaces"]}
        require(bool(immutability["workspaces"]), label + " missing workspace file hashes")
        for workspace_name, hashes in maps.items():
            for relative, digest in readonly.items():
                equal(hashes.get(relative), digest, label + " read-only file drift " + workspace_name + "/" + relative)
        verification = [event["payload"] for event in events if event["kind"] == "verification_started"]
        expected_hashes = sorted({event["workspace_hash"] for event in verification})
        equal(sorted(immutability["matched_verification_workspace_hashes"]), expected_hashes,
              label + " verification workspace evidence missing")
        require(not fixed or len(verification) == 1, label + " success without one actual verification")
        for event in verification:
            equal(event.get("proposal_digest"), (proposal or {}).get("digest"), label + " verified different patch")
        require(fixed or bool(entry.get("failure_category")), label + " missing failure classification")
        rows.append({"fixed": fixed, "calls": len(indexes), "usage": usage, "tariff": tariff,
                     "unknown": unknown_for_run, "elapsed": Decimal(str(entry["elapsed_seconds"])),
                     "failure": entry.get("failure_category")})
    equal(sorted(seen), list(range(1, len(calls) + 1)), "Ledger requests not uniquely and exhaustively owned")
    total_tokens = sum(row["usage"]["total_tokens"] for row in rows)
    expected_summary = {"eligible_tasks": len(cohort), "attempted_tasks": len(rows),
        "not_run_tasks": len(cohort) - len(rows), "fixed_tasks": sum(row["fixed"] for row in rows),
        "repair_rate": sum(row["fixed"] for row in rows) / len(cohort),
        "mean_model_calls": sum(row["calls"] for row in rows) / len(rows) if rows else None,
        "mean_total_tokens": total_tokens / len(rows) if rows and not unknown else None,
        "total_tokens": total_tokens, "tokens_complete": unknown == 0, "unknown_calls": unknown,
        "total_tariff_yuan": sum((row["tariff"] for row in rows), Decimal(0)),
        "mean_elapsed_seconds": sum((row["elapsed"] for row in rows), Decimal(0)) / len(rows) if rows else None,
        "failures": dict(Counter(row["failure"] for row in rows if row["failure"]))}
    for key, expected in expected_summary.items():
        equal(record["summary"]["agent"].get(key), expected, "Agent summary " + key)
    return {"passed": not errors, "errors": errors, "artifact": path.name,
            "checked_tasks": len(cases), "frozen_eligible_tasks": len(cohort), "agent_attempts": len(rows),
            "checked_calls": len(calls), "unknown_calls": unknown, "tariff_yuan": float(tariff_total),
            "cap_liability_yuan": float(conservative + reserved),
            "scope": "Offline frozen-P1 data, call ownership, cost, approval and verifier consistency"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.artifact)
    except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
        result = {"passed": False, "errors": ["Invalid/incomplete artifact: " + type(exc).__name__ + ": " + str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
