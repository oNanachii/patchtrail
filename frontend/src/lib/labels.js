// @ts-check
/** Human copy for server states, stop reasons and error codes. Raw codes stay visible next to it. */
import { isCheckResult, verificationEvidence } from "./checks.js";

/** @typedef {"neutral" | "info" | "attention" | "success" | "danger" | "muted" | "warning"} Tone */

/** @type {Record<string, {label: string, tone: Tone, active?: boolean}>} */
export const STATES = {
  queued: { label: "排队中", tone: "neutral", active: true },
  diagnosing: { label: "调查中", tone: "info", active: true },
  awaiting_approval: { label: "等待确认", tone: "attention" },
  verifying: { label: "验证中", tone: "info", active: true },
  succeeded: { label: "验证通过", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "muted" },
  interrupted: { label: "已中断", tone: "warning" },
};

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
/** States where the server is doing work and the page should poll quickly. */
export const WORKING = new Set(["queued", "diagnosing", "verifying"]);

/** @type {Record<string, string>} */
export const STAGE_LABELS = {
  queued: "排队",
  diagnosing: "调查",
  awaiting_approval: "人工确认",
  verifying: "容器验证",
  outcome: "结论",
};

/**
 * @type {Record<string, {label: string, title: string, detail: string, notExecuted?: boolean, unconfirmed?: boolean}>}
 */
export const STOP_REASONS = {
  verifier_passed: { label: "验证通过", title: "验证器判定通过", detail: "已确认的补丁在新工作区中通过了不可修改的测试。" },
  verifier_failed: { label: "验证未通过", title: "补丁未通过验证", detail: "已确认的补丁被应用到新工作区，但不可修改的测试没有通过。下方是实际检查输出。" },
  operator_cancelled: { label: "已取消", title: "操作者已取消", detail: "取消是终态。进行中的单次请求可能仍会结束，但其结果不会改变这个状态。" },
  budget_exhausted: { label: "预算耗尽", title: "达到执行预算上限", detail: "调用次数、工具次数或时间预算已用完，没有可确认的补丁。" },
  context_budget: { label: "上下文超限", title: "输入上下文达到上限", detail: "上下文超过配置上限后停止；服务端不做静默截断。" },
  time_budget: { label: "超时", title: "执行时间预算耗尽", detail: "时间预算在提出补丁前用完，没有接受任何补丁。" },
  no_patch_proposal: { label: "未提出补丁", title: "模型回答了，但没有提出补丁", detail: "自然语言回答不能完成运行；只有经确认并通过验证的补丁才算成功。" },
  sandbox_unavailable: { label: "容器不可用", title: "容器不可用，代码未执行", detail: "服务不会转到宿主机执行。这不是补丁失败，也不是补丁成功：检查没有运行。", notExecuted: true },
  baseline_unconfirmed: { label: "基线未确认", title: "基线检查未能确认", detail: "基线检查没有确认实际运行的用例（可能为零用例、超时或输出截断），因此不能继续提出补丁。", notExecuted: true },
  model_error: { label: "模型错误", title: "模型调用失败", detail: "模型请求失败或返回无效内容。不会自动重试结果未知的请求。" },
  runtime_error: { label: "运行时错误", title: "任务或运行时契约失败", detail: "请查看执行记录中的工具事件。" },
  verification_contract: { label: "契约不符", title: "确认的补丁与任务不再匹配", detail: "验证工作区与已确认补丁的哈希不一致，补丁未被执行。", notExecuted: true },
  verification_unconfirmed: { label: "验证未确认", title: "检查未确认完成，不能判断补丁", detail: "检查可能未启动或只执行了一部分；请核对执行器和完整输出，再决定后续处理。", unconfirmed: true },
  process_restart: { label: "进程重启", title: "服务重启导致中断", detail: "服务进程重启时这次运行仍在进行，已被标记为中断。恢复需要明确操作。" },
};

/** @param {{state: string, stop_reason?: string | null, result?: any}} run */
export function runStopReason(run) {
  if (run.state === "failed" && isCheckResult(run.result) && !verificationEvidence(run.result).complete) {
    return STOP_REASONS.verification_unconfirmed;
  }
  return run.stop_reason ? STOP_REASONS[run.stop_reason] ?? null : null;
}

/** @param {{state: string, stop_reason?: string | null, result?: any}} run */
export function stateLabel(run) {
  const reason = runStopReason(run);
  if (run.state === "failed" && reason) return reason.label;
  return STATES[run.state]?.label ?? run.state;
}

/** @param {string} state */
export function stateTone(state) {
  return STATES[state]?.tone ?? "neutral";
}

/**
 * A missing or incomplete check is a warning, so it is not read as a tested patch failure.
 * @param {{state: string, stop_reason?: string | null, result?: any}} run
 * @returns {Tone}
 */
export function runTone(run) {
  const reason = runStopReason(run);
  if (run.state === "failed" && (reason?.notExecuted || reason?.unconfirmed)) return "warning";
  return stateTone(run.state);
}

/** @type {Record<string, string>} */
const ERRORS = {
  network_error: "无法连接 PatchTrail 服务。请检查后端或回环转发是否在运行。",
  invalid_response: "服务返回了无法解析或不完整的响应。",
  operator_required: "需要操作令牌。live 运行及其记录只对操作者开放。",
  operator_not_configured: "服务端没有配置操作令牌，live 功能不可用。",
  model_not_configured: "服务端没有配置 DeepSeek 密钥，不能创建真实模型运行。",
  sandbox_unavailable: "容器运行环境不可用。服务不会转到宿主机执行，因此没有创建运行。",
  rate_limited: "启动过于频繁（每分钟最多 6 次），请稍后再试。",
  queue_full: "预览队列已满（最多 6 个未结束的运行）。请等待或取消已有运行。",
  state_conflict: "服务端状态已变化（版本或状态冲突），已重新获取最新状态。",
  not_found: "找不到这次运行。",
  unknown_task: "任务不在目录中。",
  invalid_request: "请求字段不符合 API 契约。",
  request_too_large: "控制请求过大。",
  invalid_cursor: "事件游标无效。",
  storage_busy: "服务端存储暂时繁忙，可以重试。创建请求会复用原幂等键，不会重复创建。",
  storage_error: "服务端存储操作失败。",
};

/** @param {any} error */
export function describeError(error) {
  const code = typeof error?.code === "string" ? error.code : "unknown";
  return {
    code,
    status: typeof error?.status === "number" ? error.status : 0,
    message: ERRORS[code] ?? (typeof error?.message === "string" ? error.message : "请求失败"),
    server: typeof error?.message === "string" ? error.message : "",
  };
}

/** @type {Record<string, string>} */
export const TOOL_LABELS = {
  read_file: "读取文件",
  search_code: "搜索代码",
  run_check: "运行基线检查",
  propose_patch: "提出补丁",
};
