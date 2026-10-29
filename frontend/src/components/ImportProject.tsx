import { useEffect, useRef, useState } from 'react';
import { commitOutcome, createImportGeneration, editablePath, importProblem, inspectFiles, readUploadBody, validImportedTask, validImportPreview, type ImportFile } from '../lib/importState.js';
import type { Request } from '../lib/api.js';
import { describeError } from '../lib/labels.js';
import type { Health, Task, TokenStatus } from '../types';
import { Dialog, Icon, Spinner } from './ui';

type Preview = { preview_id:string; manifest:{title:string}; files:{path:string;bytes:number;sha256:string}[]; file_count:number; total_bytes:number; expires_at:string };
type Props = { open:boolean; onClose:()=>void; hasToken:boolean; credentialEpoch:number; request:Request; health:Health|null; onOperator:()=>void; onImported:(task:Task)=>void; tokenStatus:TokenStatus; operatorCheck:{checking:boolean;error:string|null}; onVerifyOperator:()=>void };

export function ImportProjectDialog(props:Props) {
  const {open,onClose,hasToken,onOperator}=props;
  return <Dialog open={open} onClose={onClose} title="导入Python项目" footer={!hasToken ? <>
    <button className="button" data-autofocus onClick={onClose}>返回</button>
    <button className="button button-primary" onClick={()=>{onClose();window.setTimeout(onOperator,0);}}>载入操作令牌</button>
  </> : undefined}>
    {hasToken && props.tokenStatus==='accepted' && !props.operatorCheck.checking && !props.operatorCheck.error ? <ImportForm key={props.credentialEpoch} {...props}/> : hasToken ? <div className="stack" role="status">
      <p>{props.operatorCheck.checking ? <><Spinner/> 正在确认操作权限…</> : '操作权限尚未确认。文件还没有读取或上传。'}</p>
      {props.operatorCheck.error && <p className="error-line">{props.operatorCheck.error}</p>}
      <p className="small muted">这是只读令牌验证，不创建运行，也不会产生模型费用。</p>
      <button type="button" className="button button-primary" disabled={props.operatorCheck.checking} onClick={props.onVerifyOperator}>重新验证操作权限</button>
      <button type="button" className="button" data-autofocus onClick={onClose}>返回</button>
    </div> : <div className="stack">
      <p>先载入操作令牌，就可以在网页选择源码和测试文件，无需SSH或手写清单。</p>
      <p className="small muted">操作令牌由管理员提供，或在本地私有配置 <code>.env</code> 的 <code>PATCHTRAIL_ADMIN_TOKEN</code> 中查看。这里不填写 <code>DEEPSEEK_API_KEY</code>。</p>
    </div>}
  </Dialog>;
}

function ImportForm({request,health,onImported,onClose}:Props) {
  const [files,setFiles]=useState<ImportFile[]>([]);
  const [excluded,setExcluded]=useState<{path:string;reason:string}[]>([]);
  const [title,setTitle]=useState(''); const [description,setDescription]=useState('');
  const [profile,setProfile]=useState('pytest');const [testPath,setTestPath]=useState('');
  const [preview,setPreview]=useState<Preview|null>(null);
  const [phase,setPhase]=useState<'idle'|'reading'|'uploading'|'confirming'|'unknown'>('idle');
  const [error,setError]=useState('');const generation=useRef(createImportGeneration());
  const folderInput=useRef<HTMLInputElement>(null),filesInput=useRef<HTMLInputElement>(null);
  useEffect(()=>{folderInput.current?.setAttribute('webkitdirectory','');return()=>{generation.current.advance();};},[]);
  const commitLocked=phase==='confirming'||phase==='unknown';
  const state={files,title,description,profile,testPath};const problem=importProblem(state);
  const imageUnavailable=profile==='pytest'&&health?.pytest_configured!==true;
  const selected=files.filter((entry)=>entry.selected),size=selected.reduce((sum,entry)=>sum+entry.bytes,0);
  function invalidate(){generation.current.advance();setPreview(null);setError('');setPhase('idle');}
  function choose(fileList:FileList|null,folder:boolean){
    invalidate();const found=inspectFiles(Array.from(fileList??[]),folder);setFiles(found.files);setExcluded(found.excluded);setTitle(found.title);setTestPath(found.testPath);
    if(folderInput.current)folderInput.current.value='';if(filesInput.current)filesInput.current.value='';
  }
  async function upload(){
    const ticket=generation.current.capture();setError('');setPhase('reading');
    try{
      const body=await generation.current.guard(ticket,()=>readUploadBody(state));setPhase('uploading');
      const result=await generation.current.guard(ticket,()=>request('/api/imports/preview',{method:'POST',body}));
      if(!validImportPreview(result))throw new Error('服务器预览响应不完整，请重试同一份内容。');
      setPreview(result);setPhase('idle');
    }catch(caught:any){if(caught?.name==='AbortError')return;setError(describeError(caught).message);setPhase('idle');}
  }
  async function confirm(){
    if(!preview)return;const identity=preview.preview_id,ticket=generation.current.capture();setError('');setPhase('confirming');
    try{
      const result=await generation.current.guard(ticket,()=>request('/api/imports/commit',{method:'POST',body:{preview_id:identity}}));
      if(!validImportedTask(result?.task))throw new Error('确认响应未完整，请保留同一预览核对。');
      onImported(result.task);onClose();
    }catch(caught:any){
      if(caught?.name==='AbortError')return;
      const unknown=commitOutcome(caught)==='unknown';setPhase(unknown?'unknown':'idle');
      if(!unknown&&[404,409,410].includes(caught?.status))setPreview(null);
      setError(unknown?'导入结果未知，项目可能已经写入。用同一预览重试会核对原快照，不会调用模型。':describeError(caught).message);
    }
  }
  return <div className="web-import stack">
    <p>选择文件 → 填写问题 → 上传预览 → 确认导入。完成后会自动选择新私有任务，<strong>不会自动开始模型调用。</strong></p>
    <div className="import-pickers">
      <button className="button" type="button" disabled={commitLocked} onClick={()=>folderInput.current?.click()}><Icon name="file"/>选择项目文件夹</button>
      <button className="button" type="button" disabled={commitLocked} onClick={()=>filesInput.current?.click()}>选择多个文件</button>
      <input ref={folderInput} type="file" multiple hidden aria-label="选择项目文件夹" onChange={(event)=>choose(event.currentTarget.files,true)}/>
      <input ref={filesInput} type="file" multiple hidden aria-label="选择多个文件" onChange={(event)=>choose(event.currentTarget.files,false)}/>
    </div>
    <p className="small muted">默认选择Python文件；文件夹保留相对目录，多个文件按文件名平铺。最多200个文件、总计2 MiB、每文件128 KiB。常见私密和生成文件按名称排除，请确认选中文件的内容可以上传。</p>
    {excluded.length>0&&<details className="small"><summary>已排除{excluded.length}个文件</summary><ul className="plain-list">{excluded.slice(0,30).map((entry,i)=><li key={i}><code>{entry.path}</code>：{entry.reason}</li>)}{excluded.length>30&&<li>其余已排除文件未展开。</li>}</ul></details>}
    <fieldset className="import-fields" disabled={commitLocked}>
      <label className="field"><span>项目名称</span><input value={title} maxLength={160} onChange={(event)=>{invalidate();setTitle(event.target.value);}} placeholder="选择文件后自动填写"/></label>
      <label className="field"><span>需要修复的问题</span><textarea value={description} maxLength={4000} rows={3} onChange={(event)=>{invalidate();setDescription(event.target.value);}} placeholder="例如：数量不足时仍返回无需采购；期望计算实际缺口。"/></label>
      <div className="import-test-fields">
        <label className="field"><span>测试方式</span><select value={profile} onChange={(event)=>{invalidate();setProfile(event.target.value);}}><option value="pytest">pytest（也能运行unittest测试）</option><option value="unittest">unittest</option></select></label>
        <label className="field"><span>测试入口</span><input value={testPath} onChange={(event)=>{invalidate();setTestPath(event.target.value);}} placeholder="自动识别，例如 tests 或 test_app.py"/></label>
      </div>
      {imageUnavailable&&<p className="inline-warn"><Icon name="info"/>固定pytest环境{health?'尚未配置，请选择unittest或联系管理员。':'正在确认中。'}</p>}
      {files.length>0&&<>
        <p className="small">选中{selected.length}个文件 · {(size/1024).toFixed(1)} KiB。请确认允许模型修改的源码，最多3个；测试和配置只读。</p>
        <div className="import-file-list" aria-label="上传文件清单">{files.map((entry,index)=><div className="import-file-row" key={entry.path}>
          <label><input type="checkbox" checked={entry.selected} aria-label={'上传 '+entry.path} onChange={(event)=>{invalidate();setFiles((current)=>current.map((file,i)=>i===index?{...file,selected:event.target.checked,editable:event.target.checked&&file.editable}:file));}}/><code>{entry.path}</code><span className="muted">{(entry.bytes/1024).toFixed(1)} KiB</span></label>
          {editablePath(entry.path)?<label className="small"><input type="checkbox" checked={entry.editable&&entry.selected} disabled={!entry.selected} aria-label={'允许修改 '+entry.path} onChange={(event)=>{invalidate();setFiles((current)=>current.map((file,i)=>i===index?{...file,editable:event.target.checked}:file));}}/>允许修改</label>:<span className="small muted">只读</span>}
        </div>)}</div>
      </>}
    </fieldset>
    {!preview&&<>
      <p className="small">点击“上传并预览”才会将选中文件发送到服务器。导入不会执行源码，也不会调用或计费DeepSeek；真实运行需要另行确认。当前只提供标准库/pytest固定环境，不会自动安装项目依赖。</p>
      {problem&&files.length>0&&<p className="small muted">{problem}</p>}
      <button className="button button-primary" disabled={Boolean(problem)||imageUnavailable||phase==='reading'||phase==='uploading'} onClick={upload}>{phase==='reading'||phase==='uploading'?<Spinner/>:<Icon name="file"/>}{phase==='reading'?'正在读取选中文件…':phase==='uploading'?'正在上传预览…':'上传并预览'}</button>
    </>}
    {preview&&<div className="import-preview stack" role="status">
      <h3>服务器预览已就绪</h3><p>{preview.manifest.title} · {preview.file_count}个文件 · {(preview.total_bytes/1024).toFixed(1)} KiB。还没有创建运行或调用模型。</p>
      <details><summary>查看服务器文件与摘要</summary><ul className="plain-list small">{preview.files.map((entry)=><li key={entry.path}><code>{entry.path}</code> · {entry.bytes}字节 · <code title={entry.sha256}>{entry.sha256.slice(0,12)}…</code></li>)}</ul></details>
      <button className="button button-primary" disabled={phase==='confirming'} onClick={confirm}>{phase==='confirming'?<Spinner/>:<Icon name="check"/>}{phase==='unknown'?'用同一预览重试确认':'确认导入'}</button>
      {phase==='unknown'&&<p className="small">编辑已锁定以保留原预览；也可关闭后在私有任务目录核对。</p>}
    </div>}
    {error&&<p className="error-line" role="alert">{error}</p>}
    <button className="button" data-autofocus onClick={onClose}>关闭导入窗口</button>
  </div>;
}
