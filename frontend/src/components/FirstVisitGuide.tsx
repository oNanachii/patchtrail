import { useState } from "react";
import { guideClosed, localStorageOrNull, rememberGuideClosed } from "../lib/firstVisit.js";
import { Icon, ModeTag } from "./ui";

/** Dismissible, non-modal card: what PatchTrail is and a one-minute path through the script demo. */
export function FirstVisitGuide({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(() => !guideClosed(localStorageOrNull()));
  if (!open) return null;
  const close = () => {
    rememberGuideClosed(localStorageOrNull());
    setOpen(false);
    onClose();
  };
  return (
    <section className="first-visit" aria-labelledby="first-visit-title"
      onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
      <div className="first-visit-head">
        <div>
          <h2 id="first-visit-title" className="first-visit-kicker">第一次来？看这里</h2>
          <p className="first-visit-lede">带人工审批与沙箱验证的 AI 代码修复 Agent。</p>
        </div>
        <button type="button" className="icon-button" onClick={close} aria-label="关闭首次使用说明" title="关闭说明（Esc）">
          <Icon name="x" size={16} />
        </button>
      </div>
      <ol className="first-visit-steps" aria-label="1 分钟试一遍脚本演示">
        <li><span className="first-visit-n">1</span><div><strong>选一个演示任务</strong>
          <p>在“选择任务”中选一个公开样例，点“开始脚本演示”；也可以直接点“一键体验脚本演示”。</p></div></li>
        <li><span className="first-visit-n">2</span><div><strong>看调查过程和补丁</strong>
          <p>“执行记录”列出读取文件、搜索代码和运行检查的步骤，“补丁审阅”显示提出的修改。</p></div></li>
        <li><span className="first-visit-n">3</span><div><strong>确认并看测试结果</strong>
          <p>点“确认修改并验证”并在弹窗中确认，补丁应用到新副本后运行原有测试，显示是否通过。</p></div></li>
      </ol>
      <p className="first-visit-note"><ModeTag mode="demo" /><span>演示模式使用预置补丁，不调用模型；测试仍由容器执行。</span></p>
    </section>
  );
}
