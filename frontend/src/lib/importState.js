// @ts-check
/** File selection and delayed-response guards; files are read only on upload. */
export const IMPORT_LIMITS = { files: 200, fileBytes: 128 * 1024, totalBytes: 2 * 1024 * 1024, bodyBytes: 4 * 1024 * 1024 };
const DENIED_PARTS = new Set(['.git','.hg','.svn','.venv','venv','node_modules','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','dist','build','artifacts','var','secrets','credentials','site-packages']);
const DENIED_NAMES = new Set(['credentials.json','credential.json','secrets.json','secret.json','id_rsa','id_ed25519','token.json','tokens.json','authorized_keys','known_hosts']);
const SUFFIXES = new Set(['py','md','txt','json','toml','ini','cfg','yaml','yml','csv']);
const SAFE_PART = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const CONFIG = /^(?:(?:config|settings)(?:_[a-z0-9_-]+)?|[a-z0-9_-]+_(?:config|settings))\.py$/;

export function staleImportError() { return new DOMException('导入内容或操作令牌已变化', 'AbortError'); }
export function createImportGeneration() {
  let generation = 0;
  return {
    capture: () => generation,
    advance: () => ++generation,
    isCurrent: (/** @type {number} */ value) => value === generation,
    assertCurrent: (/** @type {number} */ value) => { if (value !== generation) throw staleImportError(); },
    async guard(/** @type {number} */ value, /** @type {() => Promise<any>} */ action) {
      try { const result = await action(); if (value !== generation) throw staleImportError(); return result; }
      catch (error) { if (value !== generation) throw staleImportError(); throw error; }
    },
  };
}

/** @param {string} path @returns {string | null} */
export function excludedPath(path) {
  const parts = path.split('/');
  if (!path || parts.some((part) => !SAFE_PART.test(part))) return '路径包含不支持的字符';
  if (parts.some((part) => part.startsWith('.') || DENIED_PARTS.has(part.toLowerCase()))) return '隐藏、私密或生成目录';
  const name = parts.at(-1)?.toLowerCase() ?? '';
  if (DENIED_NAMES.has(name) || name.startsWith('secrets.') || name.startsWith('credentials.')) return '常见凭据文件';
  if (!SUFFIXES.has(name.split('.').at(-1) ?? '') && !['license','notice','readme'].includes(name)) return '不是允许的文本源码/测试文件';
  return null;
}
/** @param {string} path */
export function editablePath(path) {
  const parts = path.toLowerCase().split('/'), name = parts.at(-1) ?? '';
  return !excludedPath(path) && path.endsWith('.py') && name !== '__init__.py'
    && !['conftest.py','setup.py'].includes(name) && !CONFIG.test(name)
    && !name.startsWith('test_') && !name.endsWith('_test.py')
    && !parts.some((part) => ['test','tests','config','configs','configuration','settings'].includes(part));
}
/** @typedef {{name:string,size:number,webkitRelativePath?:string,arrayBuffer:()=>Promise<ArrayBuffer>}} UploadFile */
/** @typedef {{path:string,file:UploadFile,bytes:number,selected:boolean,editable:boolean}} ImportFile */
/** @param {UploadFile[]} files @param {boolean} folder */
export function inspectFiles(files, folder = false) {
  /** @type {ImportFile[]} */ const accepted = [];
  /** @type {{path:string,reason:string}[]} */ const excluded = [];
  const paths = new Set();
  for (const file of files) {
    const relative = folder ? file.webkitRelativePath || file.name : file.name;
    const root = folder && relative.includes('/') ? relative.split('/')[0] : '';
    const path = root ? relative.slice(root.length + 1) : relative;
    const reason = root && (root.startsWith('.') || DENIED_PARTS.has(root.toLowerCase())) ? '选择的是隐藏或生成目录' : excludedPath(path);
    if (reason || file.size > IMPORT_LIMITS.fileBytes || paths.has(path)) {
      excluded.push({ path, reason: reason ?? (paths.has(path) ? '重复相对路径' : '单文件超过128 KiB') });
      continue;
    }
    paths.add(path);
    accepted.push({ path, file, bytes: file.size, selected: path.endsWith('.py'), editable: false });
  }
  accepted.sort((a,b) => a.path.localeCompare(b.path));
  const candidates = accepted.filter((entry) => entry.selected && editablePath(entry.path));
  if (candidates.length <= 3) for (const entry of candidates) entry.editable = true;
  const first = files[0];
  const title = folder && first?.webkitRelativePath?.includes('/') ? first.webkitRelativePath.split('/')[0] : candidates[0]?.path.replace(/\.py$/i,'') ?? '自己的Python项目';
  return { files: accepted, excluded, title, testPath: suggestTestPath(accepted.filter((entry) => entry.selected).map((entry) => entry.path)) };
}
/** @param {string[]} paths */
export function suggestTestPath(paths) {
  const tests = paths.filter((path) => /(?:^|\/)(?:test_[^/]+|[^/]+_test)\.py$/i.test(path));
  return tests.some((path) => path.startsWith('tests/')) ? 'tests' : tests[0] ?? '';
}
/** Display grouping only; the server separately hashes contents. @param {string} name */
export function projectSlug(name) {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,56);
  if (slug.length >= 2) return slug;
  let hash=2166136261; for (const char of name) hash=Math.imul(hash^char.charCodeAt(0),16777619);
  return 'owned-' + (hash>>>0).toString(16);
}
/** @param {{files:ImportFile[],title:string,description:string,profile:string,testPath:string}} state */
export function importProblem(state) {
  const selected = state.files.filter((entry) => entry.selected);
  if (!state.title.trim()) return '请填写项目名称。';
  if (!state.description.trim()) return '请描述需要修复的问题。';
  if (selected.length < 2) return '请至少选择一个源码文件和一个测试文件。';
  if (selected.length > IMPORT_LIMITS.files) return '选中文件超过200个，请缩小范围。';
  if (selected.reduce((sum,entry) => sum+entry.bytes,0) > IMPORT_LIMITS.totalBytes) return '选中文件总量超过2 MiB，请缩小范围。';
  const editable = selected.filter((entry) => entry.editable);
  if (!editable.length || editable.length > 3) return '请选择1至3个允许模型修改的源码文件。';
  if (editable.some((entry) => !editablePath(entry.path))) return '测试和配置文件不能允许修改。';
  const tests = state.testPath.split(/[\n,]/).map((value) => value.trim()).filter(Boolean);
  if (!tests.length || tests.length > 8 || new Set(tests).size !== tests.length) return '请填写1至8个不重复的测试路径。';
  if (state.profile === 'unittest' && tests.length !== 1) return 'unittest模式只能选择一个测试目录或文件。';
  for (const test of tests) {
    const path = test.split('::')[0];
    if (excludedPath(path + (path.includes('.') ? '' : '/test_placeholder.py'))) return '测试路径必须是项目内的正常相对路径。';
    if (!selected.some((entry) => entry.path === path || entry.path.startsWith(path+'/'))) return '测试入口不在选中文件中。';
    if (editable.some((entry) => entry.path === path || entry.path.startsWith(path+'/'))) return '测试入口中的文件不能允许修改。';
  }
  return null;
}
/** @param {{files:ImportFile[],title:string,description:string,profile:string,testPath:string}} state */
export async function readUploadBody(state) {
  const problem = importProblem(state); if (problem) throw new Error(problem);
  const selected = state.files.filter((entry) => entry.selected);
  const encoder = new TextEncoder();
  const files = await Promise.all(selected.map(async (entry) => {
    const buffer = await entry.file.arrayBuffer();
    if (buffer.byteLength > IMPORT_LIMITS.fileBytes || buffer.byteLength !== entry.bytes) throw new Error('文件大小在选择后变化，请重新选择。');
    let content;
    try { content = new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(buffer); }
    catch { throw new Error(entry.path+'不是有效UTF-8文本，尚未上传。'); }
    if (content.includes('\0')) throw new Error(entry.path+'含有NUL字符，可能不是文本源码，尚未上传。');
    if (encoder.encode(content).byteLength !== buffer.byteLength) throw new Error('文件编码校验失败，请重新选择。');
    return { path:entry.path,content };
  }));
  const body = { manifest:{ schema_version:1,project_name:projectSlug(state.title.trim()),title:state.title.trim(),description:state.description.trim(),files:files.map((entry) => entry.path),editable_files:selected.filter((entry) => entry.editable).map((entry) => entry.path),check_profile:state.profile,test_paths:state.testPath.split(/[\n,]/).map((v) => v.trim()).filter(Boolean),tags:['web-import','owned-project'],provenance:'Submitted through the authenticated browser file preview; no code execution at import.' },files };
  if (encoder.encode(JSON.stringify(body)).byteLength > IMPORT_LIMITS.bodyBytes) throw new Error('编码后的上传请求超过4 MiB，请减少文件。');
  return body;
}
/** Guard a catalog read which began before a successful import. */
export function createCatalogGeneration() {
  let generation=0;
  return { capture:()=>generation, advance:()=>++generation, isCurrent:(/** @type {number} */value)=>value===generation };
}
/** A malformed success response does not prove a commit was rejected. @param {any} error */
export function commitOutcome(error) {
  const status = Number(error?.status) || 0;
  return error?.network || error?.code === 'invalid_response' || status < 400 || status >= 500 ? 'unknown' : 'rejected';
}
/**
 * Bind before reading files: a late read must never send old source using a new token.
 * @param {import('./api.js').Request} request
 * @param {number} epoch
 * @param {(epoch:number)=>boolean} isCurrent
 * @returns {import('./api.js').Request}
 */
export function bindImportRequest(request, epoch, isCurrent) {
  const assert = () => { if (!isCurrent(epoch)) throw staleImportError(); };
  return async (path,options) => {
    assert();
    try { const data=await request(path,options); assert(); return data; }
    catch(error) { assert(); throw error; }
  };
}
/** Validate before rendering a successful but possibly malformed API response. @param {any} value */
export function validImportPreview(value) {
  if (!value || !/^upload-[a-f0-9]{64}$/.test(value.preview_id ?? '') || typeof value.manifest?.title !== 'string'
      || !Array.isArray(value.files) || value.files.length < 2 || value.files.length > IMPORT_LIMITS.files
      || value.file_count !== value.files.length) return false;
  const paths=new Set();let bytes=0;
  for (const entry of value.files) {
    if (typeof entry.path!=='string' || excludedPath(entry.path) || paths.has(entry.path)
        || !Number.isInteger(entry.bytes) || entry.bytes<0 || entry.bytes>IMPORT_LIMITS.fileBytes
        || !/^[a-f0-9]{64}$/.test(entry.sha256 ?? '')) return false;
    paths.add(entry.path);bytes+=entry.bytes;
  }
  return bytes===value.total_bytes && bytes<=IMPORT_LIMITS.totalBytes;
}
/** @param {any} value */
export function validImportedTask(value) {
  return value?.private===true && /^project-[a-f0-9]{56}$/.test(value.id ?? '')
    && typeof value.title==='string' && typeof value.description==='string'
    && Array.isArray(value.editable_files) && value.editable_files.length>0 && value.editable_files.length<=3
    && value.editable_files.every((/** @type {any} */ p)=>typeof p==='string')
    && Array.isArray(value.tags) && value.tags.every((/** @type {any} */ tag)=>typeof tag==='string')
    && /^[a-f0-9]{64}$/.test(value.source_hash ?? '');
}
