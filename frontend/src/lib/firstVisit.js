// @ts-check
/* First-visit guide state. Only the fact that this browser closed the guide is stored. When
   storage is missing or throws (private windows, blocked site data), the guide closes for the
   current page and shows again on the next visit. */
export const FIRST_VISIT_KEY = "patchtrail.firstVisitGuide";

/** @returns {Pick<Storage, "getItem" | "setItem"> | null} */
export function localStorageOrNull() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** @param {Pick<Storage, "getItem"> | null} storage */
export function guideClosed(storage) {
  try {
    return storage?.getItem(FIRST_VISIT_KEY) === "closed";
  } catch {
    return false;
  }
}

/**
 * @param {Pick<Storage, "setItem"> | null} storage
 * @returns {boolean} whether the choice was saved for later visits
 */
export function rememberGuideClosed(storage) {
  if (!storage) return false;
  try {
    storage.setItem(FIRST_VISIT_KEY, "closed");
    return true;
  } catch {
    return false;
  }
}
