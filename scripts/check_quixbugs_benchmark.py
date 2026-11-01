"""Independently reconcile a saved QuixBugs result and its request ledger offline."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


NAMES = set("""bitcount breadth_first_search bucketsort depth_first_search detect_cycle
find_first_in_sorted find_in_sorted flatten gcd get_factors hanoi
is_valid_parenthesization kheapsort knapsack kth lcs_length levenshtein lis
longest_common_subsequence max_sublist_sum mergesort minimum_spanning_tree
next_palindrome next_permutation pascal possible_change powerset quicksort
reverse_linked_list rpn_eval shortest_path_length shortest_path_lengths
shortest_paths shunting_yard sieve sqrt subsequences to_base topological_ordering wrap""".split())
METHODS = ("baseline", "agent")
PEAK = {"cache_hit": Decimal("0.04"), "cache_miss": Decimal(2), "output": Decimal(8)}
OFFPEAK = {key: value / 2 for key, value in PEAK.items()}
USAGE = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
         "prompt_cache_miss_tokens")


def verify(path):
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    def equal(actual, expected, label):
        if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
            try:
                matches = abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal("0.000000001")
            except (ValueError, TypeError, ArithmeticError):
                matches = False
            require(matches, f"{label}: {actual!r} != {expected!r}")
        else:
            require(actual == expected, f"{label}: {actual!r} != {expected!r}")

    def cost(usage, rates):
        return (usage["prompt_cache_hit_tokens"] * rates["cache_hit"]
                + usage["prompt_cache_miss_tokens"] * rates["cache_miss"]
                + usage["completion_tokens"] * rates["output"]) / Decimal(1000000)

    def passed(check):
        return bool(check and check.get("passed") is True and check.get("complete") is True
                    and check.get("exit_code") == 0 and check.get("tests_run", 0) > 0
                    and not check.get("timed_out") and not check.get("truncated"))

    record = json.loads(path.read_text())
    calls_name = record["calls_artifact"]
    if Path(calls_name).name != calls_name:
        raise ValueError("calls_artifact must identify a sibling file")
    ledger = json.loads((path.parent / calls_name).read_text())
    calls = ledger["calls"]
    cases = record["cases"]
    names = [case["name"] for case in cases]
    require(len(names) == 40 and len(set(names)) == 40 and set(names) == NAMES,
            "Expected each of the 40 pinned Python tasks exactly once")
    equal(ledger.get("model"), "deepseek-flash", "ledger model")
    equal(record["environment"]["model"], ledger.get("model"), "configured model")
    cap = Decimal(str(ledger["cap_yuan"]))
    require(0 < cap <= 8, "Cap must be positive and at most CNY 8")
    equal(record["cap_yuan"], cap, "record cap")
    independent_calls, peak_total, reserved, tariff_total = {}, Decimal(0), Decimal(0), Decimal(0)
    unknown_count = 0
    for number, call in enumerate(calls, 1):
        label = f"call {number}"
        equal(call["index"], number, label + " index")
        require(not unknown_count, label + " dispatched after an unresolved request")
        request = call["request"]
        equal(request.get("model"), "deepseek-flash", label + " requested model")
        output_limit = request["max_tokens"]
        require(type(output_limit) is int and 0 < output_limit <= 4096, label + " output limit")
        serialized = json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        equal(call["request_sha256"], hashlib.sha256(serialized).hexdigest(), label + " request hash")
        reserve = ((len(serialized) + 4096) * PEAK["cache_miss"]
                   + output_limit * PEAK["output"]) / Decimal(1000000)
        equal(call["estimate"]["reservation_peak_yuan"], reserve, label + " reservation")
        require(peak_total + reserved + reserve <= cap, label + " exceeded cap at dispatch")
        status = call.get("status")
        require(status in {"confirmed", "unknown", "pending"}, label + " unknown status")
        usage, tariff = Counter(), Decimal(0)
        if status == "confirmed":
            raw = call["response"]["usage"]
            require(all(type(raw.get(key)) is int and raw[key] >= 0 for key in USAGE[:-1]),
                    label + " response usage is incomplete")
            usage.update({key: raw[key] for key in USAGE[:-1]})
            usage["prompt_cache_miss_tokens"] = usage["prompt_tokens"] - usage["prompt_cache_hit_tokens"]
            require(usage["prompt_cache_miss_tokens"] >= 0, label + " negative uncached tokens")
            equal(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"],
                  label + " usage arithmetic")
            require(usage["completion_tokens"] <= output_limit, label + " exceeded output allowance")
            if "prompt_cache_miss_tokens" in raw:
                equal(raw["prompt_cache_miss_tokens"], usage["prompt_cache_miss_tokens"], label + " raw cache miss")
            for key in USAGE:
                equal(call["usage"].get(key), usage[key], label + " " + key)
            local = datetime.fromisoformat(call["started_at_utc"]).astimezone(ZoneInfo("Asia/Shanghai"))
            rates = PEAK if local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18) else OFFPEAK
            for key, rate in rates.items():
                equal(call["tariff_rates_yuan_per_million"].get(key), rate, label + " rate " + key)
            peak, tariff = cost(usage, PEAK), cost(usage, rates)
            equal(call["conservative_peak_yuan"], peak, label + " peak cost")
            equal(call["tariff_yuan"], tariff, label + " tariff cost")
            require(peak <= reserve, label + " actual cost exceeded reservation")
            peak_total += peak
            tariff_total += tariff
        else:
            unknown_count += 1
            reserved += reserve
            require(record["state"] != "completed", label + " unresolved outcome in completed batch")
        independent_calls[number] = {"usage": usage, "tariff": tariff, "confirmed": status == "confirmed"}

    expected_cost = {"dispatched_calls": len(calls), "confirmed_calls": len(calls) - unknown_count,
                     "unknown_calls": unknown_count, "tariff_yuan": tariff_total,
                     "conservative_peak_yuan": peak_total, "unresolved_reserved_yuan": reserved,
                     "cap_liability_yuan": peak_total + reserved, "cap_yuan": cap,
                     "cost_is_tariff_estimate": True}
    require(peak_total + reserved <= cap, "Final liability exceeds cap")
    for owner, summary in (("ledger", ledger["summary"]), ("record", record["cost"])):
        for key, value in expected_cost.items():
            equal(summary.get(key), value, owner + " cost " + key)
    if unknown_count:
        require(ledger["summary"].get("stopped") is True and record["cost"].get("stopped") is True,
                "Unresolved request did not stop dispatch")

    eligible, seen, rows = [], [], {method: [] for method in METHODS}
    outcomes = {}
    for case in cases:
        name = case["name"]
        if "preflight" in case:
            equal(case["eligible"], case["preflight"]["eligible"], name + " preflight eligibility")
        if not case.get("eligible"):
            require(not any(method in case for method in METHODS), name + " excluded but attempted")
            require(bool(case.get("skip_reason") or case.get("preflight", {}).get("status")),
                    name + " missing exclusion reason")
            continue
        eligible.append(case)
        preflight = case["preflight"]
        check = preflight["check"]
        require(preflight.get("eligible") is True and preflight.get("status") == "ready"
                and check.get("complete") is True and check.get("exit_code") == 1
                and check.get("passed") is False and check.get("tests_run", 0) > 0,
                name + " not a complete failing preflight")
        equal(preflight["source_hash"], case["source_hash"], name + " preflight source hash")
        for method in METHODS:
            if method not in case:
                require(record["state"] != "completed", name + " missing method in completed batch")
                continue
            row = case[method]
            label = name + "/" + method
            equal(row.get("method"), method, label + " method")
            indexes = row["call_indexes"]
            require(len(indexes) == len(set(indexes)), label + " duplicate request membership")
            seen.extend(indexes)
            usage, tariff, unknown = Counter(), Decimal(0), 0
            for ordinal, index in enumerate(indexes, 1):
                require(index in independent_calls, label + " nonexistent request index")
                if index not in independent_calls:
                    continue
                call, actual = calls[index - 1], independent_calls[index]
                equal(call["context"].get("task"), "QuixBugs Python: " + name, label + " call task")
                equal(call["context"].get("method"), method, label + " call method")
                equal(call["context"].get("call"), ordinal, label + " call ordinal")
                if method == "baseline":
                    require(not call["request"].get("tools"), label + " baseline exposed tools")
                usage.update(actual["usage"])
                tariff += actual["tariff"]
                unknown += not actual["confirmed"]
            equal(row["model_calls"], len(indexes), label + " call count")
            equal(row.get("usage_complete"), unknown == 0, label + " usage completeness")
            equal(row["unknown_calls"], unknown, label + " unknown count")
            for key in set(usage) | set(row["usage"]):
                equal(row["usage"].get(key, 0), usage[key], label + " usage " + key)
            equal(row["tariff_yuan"], tariff, label + " cost")
            require(row["elapsed_seconds"] >= 0, label + " negative elapsed time")
            if method == "agent":
                run = row["run"]
                fixed = run["state"] == "succeeded" and passed(run.get("result"))
                equal(row["status"], run["state"], label + " run status")
                approval = row.get("approval")
                if approval:
                    proposal, checkpoint = run["proposal"], run["checkpoint"]["approval"]
                    for key, value in {"actor": "脚本自动批准", "human_review": False,
                        "endpoint": "POST /api/runs/{run_id}/approve", "http_status": 200,
                        "proposal_digest": proposal["digest"], "expected_version": checkpoint["from_version"]}.items():
                        equal(approval.get(key), value, label + " approval " + key)
                    equal(checkpoint["digest"], proposal["digest"], label + " approved digest")
                require(not fixed or bool(approval), label + " successful without approval")
                require(run["limits"]["max_calls"] == 8 and run["limits"]["max_steps"] == 12
                        and run["limits"]["max_seconds"] == 120, label + " unexpected Agent budget")
                require(run["checkpoint"].get("calls", 0) <= 8
                        and run["checkpoint"].get("steps", 0) <= 12, label + " Agent exceeded call/step budget")
            else:
                fixed = passed(row.get("verification"))
                require(len(indexes) <= 1, label + " baseline iterated")
            equal(row["status"] == "succeeded", fixed, label + " verifier success")
            require(not fixed or row.get("failure_category") is None, label + " success has failure category")
            require(fixed or bool(row.get("failure_category")), label + " failure has no category")
            require(row.get("original_snapshot_unchanged", not fixed) is True, label + " source changed")
            outcomes[name, method] = fixed
            rows[method].append({"fixed": fixed, "calls": len(indexes), "usage": usage,
                "tariff": tariff, "unknown": unknown, "seconds": Decimal(str(row["elapsed_seconds"])),
                "failure": row.get("failure_category")})
    require(sorted(seen) == list(range(1, len(calls) + 1)), "Ledger requests are not uniquely/exhaustively assigned")
    for method in METHODS:
        items, actual = rows[method], record["summary"][method]
        count = len(items)
        successes = sum(item["fixed"] for item in items)
        tokens = sum(item["usage"]["total_tokens"] for item in items)
        unknown = sum(item["unknown"] for item in items)
        expected = {"eligible_tasks": len(eligible), "attempted_tasks": count,
            "not_run_tasks": len(eligible) - count, "fixed_tasks": successes,
            "repair_rate": successes / len(eligible) if eligible else None,
            "mean_model_calls": sum(item["calls"] for item in items) / count if count else None,
            "mean_total_tokens": tokens / count if count and not unknown else None,
            "total_tokens": tokens, "tokens_complete": unknown == 0, "unknown_calls": unknown,
            "total_tariff_yuan": sum((item["tariff"] for item in items), Decimal(0)),
            "mean_elapsed_seconds": sum((item["seconds"] for item in items), Decimal(0)) / count if count else None,
            "failures": dict(Counter(item["failure"] for item in items if item["failure"]))}
        for key, value in expected.items():
            equal(actual.get(key), value, method + " summary " + key)
    paired = [case["name"] for case in eligible if all(method in case for method in METHODS)]
    expected_pair = {"tasks": len(paired), "both_fixed": 0, "baseline_only": 0,
                     "agent_only": 0, "neither_fixed": 0}
    for name in paired:
        b, a = outcomes[name, "baseline"], outcomes[name, "agent"]
        expected_pair["both_fixed" if a and b else "baseline_only" if b else "agent_only" if a else "neither_fixed"] += 1
    equal(record["summary"]["paired"], expected_pair, "paired summary")
    return {"artifact": path.name, "checked_tasks": len(cases), "eligible_tasks": len(eligible),
            "paired_tasks": len(paired), "checked_calls": len(calls), "unknown_calls": unknown_count,
            "tariff_yuan": float(tariff_total), "cap_liability_yuan": float(peak_total + reserved),
            "errors": errors, "passed": not errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.artifact)
    except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
        result = {"passed": False, "errors": [f"Invalid or incomplete artifact: {type(exc).__name__}: {exc}"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
