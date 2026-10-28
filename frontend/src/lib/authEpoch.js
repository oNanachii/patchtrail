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
