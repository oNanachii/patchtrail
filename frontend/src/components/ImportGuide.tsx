import { CopyButton, Dialog, Icon } from "./ui";

const PREVIEW = 'cd /opt/patchtrail/current\nsudo -n -u patchtrail -H .venv/bin/python -m patchtrail.import_project /path/to/project --manifest /path/to/manifest.json --data-dir /var/lib/patchtrail/data';
const COMMIT = `${PREVIEW} --commit`;

export function ImportInstructions() {
  return (
    <div className="import-instructions">
      <p><strong>当前通过后端命令行导入，网页没有选择文件或上传源码的功能。</strong></p>
      <ol className="plain-list numbered">
        <li>
          <strong>准备项目和清单</strong>
          <p>准备自己的 Python 源码和测试，填写允许读取的文件、最多三个可修改源码文件、问题描述与固定测试路径。</p>
          <p><a href="/assets/import_manifest.example.json" download="manifest.json">下载清单模板 manifest.json</a>，把示例路径替换成项目里的实际文件路径。</p>
        </li>
        <li>
          <strong>在运行 PatchTrail 后端的机器上导入</strong>
          <p>使用当前公网服务时，先登录后端服务器，进入 <code>/opt/patchtrail/current</code>。将下面两处示例路径替换成服务器上实际、且服务账户可读的项目和清单路径；先预览，再确认。</p>
          <p>源码在电脑上时，先把选定源码和清单放到后端机器，再使用服务器上的路径。此步骤需要服务器命令行权限，网页操作令牌不提供 SSH 或文件传输权限。</p>
          <p className="small">预览文件清单 <CopyButton value={PREVIEW} label="预览命令" /></p>
          <pre className="code-block code-scroll" tabIndex={0} aria-label="预览导入命令">{PREVIEW}</pre>
          <p className="small">确认导入 <CopyButton value={COMMIT} label="确认导入命令" /></p>
          <pre className="code-block code-scroll" tabIndex={0} aria-label="确认导入命令">{COMMIT}</pre>
          <p><strong>电脑上运行 CLI 不会自动同步到这个网站。</strong>自己的本地部署可直接使用 <code>.venv/bin/python</code>，并将数据目录换成该后端的实际目录，默认是 <code>var</code>。</p>
        </li>
        <li>
          <strong>载入令牌，选择私有任务</strong>
          <p>导入成功后，关闭说明，点击右上角的“未载入令牌”。载入操作令牌后，“任务”列表会出现“私有项目 · 项目名称”，可以开始真实模型运行。</p>
          <p>令牌只用于访问已导入的项目，不负责上传代码。刷新网页后需要重新载入令牌。</p>
        </li>
      </ol>
      <p className="small"><a href="https://github.com/oNanachii/patchtrail#导入自己的python项目" target="_blank" rel="noreferrer">查看完整导入说明</a></p>
    </div>
  );
}

export function ImportGuide({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title="导入自己的项目 · 命令行步骤"
      footer={<button type="button" className="button" data-autofocus onClick={onClose}>关闭说明</button>}>
      <ImportInstructions />
    </Dialog>
  );
}

export function ImportGuideButton({ onClick }: { onClick: () => void }) {
  return <button type="button" className="button button-block" onClick={onClick}><Icon name="terminal" size={15} />导入指引（CLI）</button>;
}
