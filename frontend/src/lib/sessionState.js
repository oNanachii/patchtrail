// @ts-check
import { obsoleteAuthError } from './authEpoch.js';

/** HTTPS is mandatory except for explicit loopback development. @param {string} origin */
export function isSecureCredentialOrigin(origin) {
  try {
    const url = new URL(origin);
    return url.protocol === 'https:' || url.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(url.hostname);
  } catch { return false; }
}

/** @param {any} value */
export function validSession(value) {
  return Boolean(value && (value.role === 'operator' || value.role === 'member'
    && /^[a-f0-9]{32}$/.test(value.member_id) && Number.isFinite(value.expires_at))
    && typeof value.model === 'string');
}

/** Stable namespace for request reconciliation; never derived from a secret. @param {any} session */
export function sessionScope(session) {
  return validSession(session) ? session.role === 'member' ? `member-${session.member_id}` : 'operator' : 'public';
}

/**
 * Login itself never creates a run. If the dialog closes or another login wins,
 * discard the late session at the server as well as refusing its UI update.
 * @param {{request: import('./api.js').Request, invitation: string, apiKey: string,
 * epochs: ReturnType<import('./authEpoch.js').createAuthEpoch>, ticket: number,
 * discard: (token: string) => Promise<any>, origin?: string}} options
 */
export async function connectInvitation({request, invitation, apiKey, epochs, ticket, discard, origin}) {
  if (origin !== undefined && !isSecureCredentialOrigin(origin)) throw Object.assign(new Error("请通过HTTPS连接后再填写凭据。"), {status:0,code:"insecure_origin"});
  let result;
  try { result = await request('/api/session', {method:'POST',body:{invite_code:invitation,api_key:apiKey}}); }
  catch (error) { epochs.assertCurrent(ticket); throw error; }
  if (!validSession(result) || result.role !== 'member' || typeof result.session_token !== 'string' || !result.session_token) {
    epochs.assertCurrent(ticket);
    throw Object.assign(new Error('连接结果无法确认，请重新连接；尚未开始模型调用。'), {status:200,code:'invalid_response'});
  }
  if (!epochs.isCurrent(ticket)) {
    // Never hold up the UI while revoking an abandoned login; no new model request.
    void discard(result.session_token).catch(() => {});
    throw obsoleteAuthError();
  }
  return result;
}

/** @param {any} result @param {{id:string,source_hash:string}} task */
export function validPreflight(result, task) {
  return Boolean(result && /^[a-f0-9]{32}$/.test(result.preflight_id)
    && result.task_id === task.id && result.source_hash === task.source_hash
    && ['ready','already_passed','environment_missing','timed_out','unconfirmed'].includes(result.status)
    && result.eligible === (result.status === 'ready') && Number.isFinite(result.expires_at));
}

/** @param {any} result @param {{id:string,source_hash:string}|null} task @param {number} now */
export function canUsePreflight(result, task, now = Date.now()) {
  return Boolean(task && validPreflight(result, task) && result.eligible && result.expires_at * 1000 > now);
}
