// @ts-check
/**
 * Same-origin JSON client for the PatchTrail API.
 * Every non-2xx response becomes an ApiError; fetch() alone does not throw for those.
 * The operator token is read from memory on each call and only ever sent as a header.
 */

export class ApiError extends Error {
  /**
   * @param {{status: number, code: string, message: string, network?: boolean}} details
   */
  constructor({ status, code, message, network = false }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    /** True when no HTTP response was received, so the outcome is unknown. */
    this.network = network;
  }
}

/**
 * @typedef {{method?: string, body?: unknown, headers?: Record<string, string>, signal?: AbortSignal, responseType?: "json" | "text"}} RequestOptions
 * @typedef {(path: string, options?: RequestOptions) => Promise<any>} Request
 */

/**
 * @param {{fetchImpl?: typeof fetch, getToken: () => string}} options
 * @returns {Request}
 */
export function createApi({ fetchImpl, getToken }) {
  const doFetch = fetchImpl ?? ((input, init) => fetch(input, init));
  return async function request(path, { method = "GET", body, headers = {}, signal, responseType = "json" } = {}) {
    const token = getToken().trim();
    /** @type {Record<string, string>} */
    const finalHeaders = { Accept: "application/json", ...headers };
    if (body !== undefined) finalHeaders["Content-Type"] = "application/json";
    if (token) finalHeaders.Authorization = `Bearer ${token}`;
    let response;
    try {
      response = await doFetch(path, {
        method,
        headers: finalHeaders,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal,
      });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw new ApiError({ status: 0, code: "network_error", message: "无法连接 PatchTrail 服务", network: true });
    }
    let data;
    try {
      if (response.ok && responseType === "text") return await response.text();
      data = await response.json();
    } catch {
      throw new ApiError({
        status: response.status,
        code: response.ok ? "invalid_response" : `http_${response.status}`,
        message: `服务返回了无法解析的响应（HTTP ${response.status}）`,
      });
    }
    if (!response.ok) {
      const error = data && typeof data === "object" ? data.error : null;
      throw new ApiError({
        status: response.status,
        code: typeof error?.code === "string" ? error.code : `http_${response.status}`,
        message: typeof error?.message === "string" ? error.message : `请求失败（HTTP ${response.status}）`,
      });
    }
    return data;
  };
}
