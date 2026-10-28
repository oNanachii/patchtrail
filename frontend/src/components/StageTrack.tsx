import { formatDuration } from "../lib/format.js";
import { STAGE_LABELS, runTone, stateLabel } from "../lib/labels.js";
import { stageTrack } from "../lib/trail.js";
import type { Run, RunEvent } from "../types";
import { Icon } from "./ui";

const STATUS_TEXT = { done: "已完成", current: "进行中", pending: "未开始", stopped: "停在此处", skipped: "未执行" } as const;

export function StageTrack({ run, events, now }: { run: Run; events: RunEvent[]; now: number }) {
  const stages = stageTrack(run, events, now);
  const tone = runTone(run);
  return (
    <ol className="stage-track" aria-label="运行阶段">
      {stages.map((stage) => {
        const isGate = stage.key === "awaiting_approval";
        const isOutcome = stage.key === "outcome";
        const stageTone = stage.status === "stopped" || (isOutcome && stage.status === "done") || (stage.status === "current" && isGate) ? tone : "";
        const label = isOutcome && (stage.status === "done" || stage.status === "stopped") ? stateLabel(run) : STAGE_LABELS[stage.key];
        let icon: "check" | "x" | "alert" | "gate" | null = null;
        if (stage.status === "done") icon = "check";
        if (stage.status === "stopped") icon = run.state === "interrupted" ? "alert" : run.state === "cancelled" ? "x" : isOutcome ? "x" : "alert";
        if (isGate && (stage.status === "current" || stage.status === "pending")) icon = "gate";
        return (
          <li key={stage.key} className={`stage stage-${stage.status}${isGate ? " is-gate" : ""}${stageTone ? ` tone-${stageTone}` : ""}`}
            aria-current={stage.status === "current" ? "step" : undefined}>
            <span className="stage-node" aria-hidden="true">{icon && <Icon name={icon} size={12} />}</span>
            <span className="stage-label">{label}</span>
            <span className="stage-meta">
              <span className="sr-only">{STATUS_TEXT[stage.status]}</span>
              {stage.duration != null ? formatDuration(stage.duration) : stage.status === "current" ? "进行中" : ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
