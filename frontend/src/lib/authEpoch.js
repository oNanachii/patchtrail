// @ts-check
/** A request belongs to the credential generation that started it, even if its body is already buffered. */
export function obsoleteAuthError() {
  const error = new Error("凭据已变化，忽略旧请求结果");
  error.name = "AbortError";
  return error;
}

export function createAuthEpoch() {
  let current = 0;
  /** @param {number} ticket */
  const assertCurrent = (ticket) => {
    if (ticket !== current) throw obsoleteAuthError();
  };
  return {
    capture: () => current,
    advance: () => ++current,
    /** @param {number} ticket */
    isCurrent: (ticket) => ticket === current,
    assertCurrent,
    /**
     * @template T
     * @param {number} ticket
     * @param {() => Promise<T>} operation
     * @returns {Promise<T>}
     */
    async guard(ticket, operation) {
      try {
        const result = await operation();
        assertCurrent(ticket);
        return result;
      } catch (error) {
        assertCurrent(ticket);
        throw error;
      }
    },
  };
}

/**
 * A read-only auth check: no run creation, source upload or model request.
 * @param {import('./api.js').Request} request
 * @param {ReturnType<typeof createAuthEpoch>} epochs
 * @param {number} ticket
 */
export async function verifyOperatorAccess(request, epochs, ticket) {
  const response = await epochs.guard(ticket, () => request('/api/projects'));
  epochs.assertCurrent(ticket);
  if (!response || !Array.isArray(response.projects)) {
    throw Object.assign(new Error('操作权限响应未确认，请重新验证。'), { status: 200, code: 'invalid_response' });
  }
}
