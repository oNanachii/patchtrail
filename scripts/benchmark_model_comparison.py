"""Agent-only official Zhipu comparison on the exact frozen P1 QuixBugs cohort."""
import argparse
import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from patchtrail.api import create_app
from patchtrail.config import ROOT, Settings, runtime_code_hash
from patchtrail.providers import OpenAICompatibleProvider, ProviderError, initial_messages
from patchtrail.service import RunService
from patchtrail.workspace import tree_hash
from scripts.benchmark_quixbugs import run_agent, save, summarize, utcnow
from scripts.check_model_comparison import BASELINE_NAME, BASELINE_SHA256
from scripts.zhipu_benchmark_meter import ENDPOINT, MODEL, PRICING, ZhipuMeter, estimate_request


class MeteredCompatibleAgent(OpenAICompatibleProvider):
    """Dispatch once through the ledger, then use the product's unchanged parser."""
    def __init__(self, settings, meter):
        self.meter, self.payload, self.body = meter, None, None
        def handle(request):
            if str(request.url) != ENDPOINT or json.loads(request.content) != self.payload:
                raise ProviderError("Product request differs from the metered request")
            return httpx.Response(200, json=self.body)
        super().__init__(replace(settings, api_key=settings.compatible_api_key),
                         transport=httpx.MockTransport(handle),
                         request_options=settings.model_options,
                         label="Zhipu GLM-4.7 live model")
        if self._endpoint() != ENDPOINT:
            raise ProviderError("Comparison requires the fixed official Zhipu endpoint")

    def complete(self, messages, task, call_number, remaining_seconds):
        self.payload = self.build_payload(messages)
        self.body = self.meter.send(self.payload, remaining_seconds,
            {"task": task.title, "method": "agent", "call": call_number})
        return super().complete(messages, task, call_number, remaining_seconds)


def frozen_reference(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != BASELINE_SHA256:
        raise ValueError("Reference must be the exact retained P1 artifact")
    return json.loads(path.read_text())


def file_hashes(directory, manifest):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in manifest["files"]}


def workspace_dirs(directory):
    return {path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()} if directory.exists() else set()


def run_with_evidence(service, app, task, metadata, receipt, meter):
    before = workspace_dirs(service.settings.data_dir / "verification")
    result = run_agent(service, app, task, receipt, meter)
    run_id = result["run"]["id"]
    workspaces = {"investigation/" + run_id: file_hashes(service.settings.data_dir / "workspaces" / run_id, metadata["manifest"])}
    hashes = []
    for path in workspace_dirs(service.settings.data_dir / "verification") - before:
        workspaces["verification/" + path.name] = file_hashes(path, metadata["manifest"])
        hashes.append(tree_hash(path))
    expected = {event["payload"]["workspace_hash"] for event in result["events"]
                if event["kind"] == "verification_started"}
    result["immutability"] = {"snapshot": file_hashes(task.source_dir, metadata["manifest"]),
        "workspaces": workspaces, "matched_verification_workspace_hashes": sorted(set(hashes) & expected),
        "unmatched_verification_workspace_hashes": sorted(set(hashes) - expected)}
    readonly = {key: digest for key, digest in metadata["files_sha256"].items()
                if key not in metadata["manifest"]["editable_files"]}
    result["immutable_tests_unchanged"] = all(
        all(observed.get(name) == digest for name, digest in readonly.items())
        for observed in [result["immutability"]["snapshot"], *workspaces.values()])
    return result


def update_report(record, path, artifact_name):
    """Idempotently add a comparison column while preserving all original P1 prose."""
    reference = json.loads((ROOT / "artifacts" / record["baseline_reference"]["artifact"]).read_text())
    text = path.read_text()
    start, end = "<!-- ZHIPU_COMPARISON_START -->", "<!-- ZHIPU_COMPARISON_END -->"
    if start in text:
        left, rest = text.split(start, 1)
        _, right = rest.split(end, 1)
        text = left.rstrip() + right
    rows = {case["name"]: case for case in record["cases"]}
    lines = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0] == "题目":
            line = "| " + " | ".join(cells[:4]) + " | 智谱 GLM-4.7 Agent |"
        elif line.startswith("| --- |") and lines and lines[-1].startswith("| 题目 |"):
            line = "| --- | --- | --- | --- | --- |"
        elif line.startswith("|") and len(cells) > 1 and cells[0] in rows:
            name = line.split("|")[1].strip()
            case = rows[name]
            attempt = case.get("agent")
            outcome = ("未运行（沿用 P1 排除）" if not case["eligible"] else
                       "通过" if attempt and attempt["status"] == "succeeded" else
                       attempt.get("failure_category", attempt["status"]) if attempt else
                       case.get("not_run_reason", "尚未运行"))
            line = "| " + " | ".join(cells[:4]) + " | " + outcome + " |"
        lines.append(line)
    old, new = reference["summary"]["agent"], record["summary"]["agent"]
    def fmt(value, places=2):
        return "未确认" if value is None else f"{value:.{places}f}"
    block = [start, "", "## 第二模型：智谱官方 API 的 Agent 对比", "",
        f"状态：`{record['state']}`；[逐题工件](../artifacts/{artifact_name})、[调用费用账本](../artifacts/{record['calls_artifact']})。",
        "冻结 P1 的同一 34 题，不重新挑选分母；原 6 个排除项不补跑。源码、测试、数据哈希、同一镜像及 8 次调用 / 12 步 / 120 秒 / 4096 输出 token 预算均核对。每题一次 Agent 运行，不重跑 DeepSeek 或单次基线；失败及未知调用全部保留，不自动重试。",
        "智谱使用官方 `https://open.bigmodel.cn/api/paas/v4` 的 `glm-4.7`，显式关闭思考以接近 P1 非思考配置；提示与工具清单保持相同。Key 仅从服务器环境读取，补丁仍由**脚本自动批准**准确 digest/version 后用原测试验证，不是人工审查。", "",
        "| 指标 | P1 DeepSeek Agent | 智谱 GLM-4.7 Agent |", "| --- | --- | --- |"]
    metrics = [("原测试全通过 / 固定分母", f"{old['fixed_tasks']}/34", f"{new['fixed_tasks']}/34"),
        ("原测试全通过比例", f"{old['repair_rate']:.2%}", f"{new['repair_rate']:.2%}"),
        ("尝试 / 未运行", f"{old['attempted_tasks']}/{old['not_run_tasks']}", f"{new['attempted_tasks']}/{new['not_run_tasks']}"),
        ("平均模型调用", fmt(old['mean_model_calls']), fmt(new['mean_model_calls'])),
        ("平均 token", fmt(old['mean_total_tokens']), fmt(new['mean_total_tokens'])),
        ("总 token（已确认）", str(old['total_tokens']), str(new['total_tokens'])),
        ("用量计价（元）", fmt(old['total_tariff_yuan'], 8), fmt(new['total_tariff_yuan'], 8)),
        ("平均耗时（秒）", fmt(old['mean_elapsed_seconds']), fmt(new['mean_elapsed_seconds']))]
    block += [f"| {name} | {first} | {second} |" for name, first, second in metrics]
    cost = record.get("cost") or {}
    block += ["", f"本轮上限 ¥{record['cap_yuan']}；最高费率保守占用 ¥{cost.get('cap_liability_yuan', 0):.8f}，未知请求 {cost.get('unknown_calls', 0)} 次。费用按[智谱官方人民币费率](https://docs.bigmodel.cn/cn/guide/start/pricing)与 API usage 分档计算，不是独立账单；缓存未报告时按零命中保守估算。每次请求先持久预留字节上界输入及最大输出的最高费率费用，未知结果保留预留并停止后续派发。",
        "当前验证器包含 P1 之后的正常完成记录修复，基线工具结果会多出完成元数据；没有为了对齐旧实验而撤回修复。因此这是同一固定题集上的历史跨模型对照，包含运行时版本差异，不能将所有差距单独归因于模型。公开基准可能进入训练数据，原测试通过不代表通用修复率或外部正确性认证。", "", "```sh",
        "# 先在服务器环境设置 ZHIPU_API_KEY，Key 不作为命令参数。",
        "python scripts/benchmark_model_comparison.py dry-run --data-dir var/benchmark/zhipu-new \\",
        "  --container-bin podman --pytest-image <P1镜像ID> --source-root <P1的quixbugs/tasks目录>",
        "# 确认预检后将 dry-run 改为 run；已开始目录拒绝再次运行。",
        f"python scripts/check_model_comparison.py artifacts/{artifact_name}", "```", "", end]
    path.write_text("\n".join(lines).rstrip() + "\n\n" + "\n".join(block) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["dry-run", "run", "report"])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--reference", type=Path, default=ROOT / "artifacts" / BASELINE_NAME)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--report-path", type=Path, default=ROOT / "docs/BENCHMARK_QUIXBUGS.md")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--container-bin", default="podman")
    parser.add_argument("--pytest-image")
    parser.add_argument("--cap-yuan", type=float, default=30)
    args = parser.parse_args(argv)
    if args.action == "report":
        record = json.loads(args.artifact.read_text())
        record["summary"] = summarize(record)
        update_report(record, args.report_path, args.artifact.name)
        return 0
    if not args.data_dir or not args.source_root or not args.pytest_image or not 0 < args.cap_yuan <= 30:
        parser.error("Dedicated --data-dir, --source-root, --pytest-image and cap in (0,30] required")
    directory = args.data_dir.resolve()
    if (directory in {ROOT, ROOT / "var", ROOT / "var/benchmark", args.source_root.resolve()}
            or (directory / "model-comparison-started.json").exists()):
        parser.error("Use a new independent benchmark directory; started runs never auto-retry")
    directory.mkdir(parents=True, exist_ok=True)
    reference = frozen_reference(args.reference)
    key = os.getenv("ZHIPU_API_KEY", "")
    if args.action == "run" and not key:
        parser.error("ZHIPU_API_KEY must be set in the server environment; zero model calls sent")
    settings = Settings(data_dir=directory / "runtime", provider_kind="openai-compatible",
        api_key="", compatible_api_key=key, model=MODEL,
        base_url=ENDPOINT.removesuffix("/chat/completions"), model_options={"thinking": {"type": "disabled"}},
        preserve_reasoning_content=False, output_token_parameter="max_tokens", admin_token=uuid.uuid4().hex,
        container_binary=args.container_bin, pytest_image=args.pytest_image, public_demo=False)
    stem = "benchmark-quixbugs-zhipu-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    artifact, calls_path = args.output_dir / (stem + ".json"), args.output_dir / (stem + "-calls.json")
    record = {"schema_version": 1, "state": "preparing", "started_at_utc": utcnow(),
        "baseline_reference": {"artifact": args.reference.name, "sha256": BASELINE_SHA256},
        "dataset": copy.deepcopy(reference["dataset"]), "calls_artifact": calls_path.name,
        "frozen_eligible_names": [case["name"] for case in reference["cases"] if case["eligible"]],
        "cases": [{key: copy.deepcopy(case[key]) for key in ("name", "source_hash", "eligible")}
                  | {"seed_source_hash": case["source_hash"], **({"skip_reason": case.get("skip_reason", "P1 exclusion")} if not case["eligible"] else {})}
                  for case in reference["cases"]], "cap_yuan": args.cap_yuan,
        "environment": {"model": MODEL, "endpoint": ENDPOINT, "image": reference["environment"]["image"],
            "limits": settings.limits, "runtime_code_hash": runtime_code_hash(),
            "controller_files_sha256": {name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
                for name in ("benchmark_model_comparison.py", "zhipu_benchmark_meter.py", "check_model_comparison.py")},
            "container_binary": args.container_bin, "data_isolation": "independent P1 benchmark directory; no deployed API/database",
            "runtime_difference": "retains post-P1 verifier completion hardening; not model-only randomized comparison"},
        "pricing": PRICING}
    save(artifact, record)
    service, meter = None, None
    try:
        image = subprocess.run([args.container_bin, "image", "inspect", args.pytest_image,
                                "--format", "{{.Id}}"], capture_output=True, text=True, timeout=20)
        if image.returncode or image.stdout.strip().removeprefix("sha256:") != reference["environment"]["image"]:
            raise RuntimeError("Exact P1 pytest image is required; no model request sent")
        settings.pytest_image = image.stdout.strip()
        service = RunService(settings, dispatch=False)
        app = create_app(service)
        tasks, metadata = {}, {task["name"]: task for task in reference["dataset"]["tasks"]}
        estimates = []
        for case in record["cases"]:
            if not case["eligible"]:
                continue
            source = args.source_root.resolve() / case["name"] / "source"
            task_meta = metadata[case["name"]]
            if tree_hash(source) != case["seed_source_hash"] or file_hashes(source, task_meta["manifest"]) != task_meta["files_sha256"]:
                raise RuntimeError("Source/test bytes differ from the frozen P1 task")
            imported = service.catalog.import_project(source, task_meta["manifest"])
            task = service.resolve_task(imported["id"])
            tasks[case["name"]] = task
            case["task_id"] = task.id
            receipt = service.preflight(task.id)
            case["preflight"] = receipt
            if not receipt["eligible"]:
                case["not_run_reason"] = "fresh_preflight_" + receipt["status"]
            elif receipt["check"]["tests_run"] != next(seed for seed in reference["cases"] if seed["name"] == case["name"])["preflight"]["check"]["tests_run"]:
                case["not_run_reason"] = "fresh_preflight_test_count_changed"
            else:
                payload = service.provider.build_payload(initial_messages(task) + [{"role": "user", "content": "Application baseline check: " + json.dumps(receipt["check"])}])
                estimates.append({"name": case["name"], **estimate_request(payload)})
            save(artifact, record)
            print(json.dumps({"prepared": case["name"], "status": receipt["status"]}), flush=True)
        record["dry_run"] = {"estimated_requests": len(estimates) * 4,
            "planning_assumption": "4 calls/task; initial input multiplied by 2 for later context; output512/call; heuristic, not tokenizer",
            "estimated_tokens": sum(4 * (2 * item["estimated_input_tokens"] + 512) for item in estimates),
            "estimated_yuan": sum(4 * (2 * item["estimated_input_tokens"] * 4 + 512 * 16) / 1e6 for item in estimates),
            "per_task": estimates, "completed_at_utc": utcnow()}
        record.update(state="dry_run_only", summary=summarize(record))
        save(artifact, record)
        if args.action == "dry-run":
            print(json.dumps({"artifact": artifact.name, "dry_run": {key: value for key, value in record["dry_run"].items() if key != "per_task"}}), flush=True)
            return 0
        save(directory / "model-comparison-started.json", {"artifact": artifact.name, "started_at_utc": utcnow()})
        meter = ZhipuMeter(calls_path, key, cap_yuan=args.cap_yuan)
        service.provider = MeteredCompatibleAgent(settings, meter)
        record["state"] = "running"
        for case in record["cases"]:
            if meter.stopped:
                break
            if not case["eligible"] or case.get("not_run_reason"):
                continue
            task = tasks[case["name"]]
            receipt = service.preflight(task.id)
            case["preflight"] = receipt
            if not receipt["eligible"]:
                case["not_run_reason"] = "fresh_preflight_" + receipt["status"]
                save(artifact, record)
                continue
            if receipt["check"]["tests_run"] != next(seed for seed in reference["cases"] if seed["name"] == case["name"])["preflight"]["check"]["tests_run"]:
                case["not_run_reason"] = "fresh_preflight_test_count_changed"
                save(artifact, record)
                continue
            case["pending_agent_attempt"] = True
            save(artifact, record)
            case["agent"] = run_with_evidence(service, app, task, metadata[case["name"]], receipt, meter)
            case.pop("pending_agent_attempt", None)
            record.update(cost=meter.summary(), summary=summarize(record))
            save(artifact, record)
            if not case["agent"]["immutable_tests_unchanged"] or not case["agent"]["original_snapshot_unchanged"]:
                raise RuntimeError("Immutable source/test evidence changed; original attempt retained")
            print(json.dumps({"task": case["name"], "state": case["agent"]["status"], "cost": record["cost"]}), flush=True)
        record.update(state="stopped" if meter.stopped else "completed", completed_at_utc=utcnow(),
                      cost=meter.summary(), summary=summarize(record))
        save(artifact, record)
        update_report(record, args.report_path, artifact.name)
        return 0 if record["state"] == "completed" else 2
    except Exception as exc:
        record.update(state="aborted", runtime_failure={"type": type(exc).__name__,
            "note": "Original run identity and ledger retained; no automatic restart"}, summary=summarize(record))
        if meter:
            record["cost"] = meter.summary()
        save(artifact, record)
        raise
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
