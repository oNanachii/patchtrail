/* First-visit guide storage contract with storage doubles. This is not a browser run. */
import assert from "node:assert/strict";
import test from "node:test";
import { FIRST_VISIT_KEY, guideClosed, localStorageOrNull, rememberGuideClosed } from "../src/lib/firstVisit.js";

function memoryStorage() {
  const map = new Map();
  return { map, getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)) };
}
const blocked = {
  getItem() { throw new Error("SecurityError: storage blocked"); },
  setItem() { throw new Error("QuotaExceededError"); },
};

test("the guide shows until this browser closes it, and only that choice is stored", () => {
  const storage = memoryStorage();
  assert.equal(guideClosed(storage), false);
  assert.equal(rememberGuideClosed(storage), true);
  assert.equal(guideClosed(storage), true);
  assert.deepEqual([...storage.map], [[FIRST_VISIT_KEY, "closed"]]);
});

test("blocked or missing storage never throws and keeps showing the guide", () => {
  assert.equal(guideClosed(blocked), false);
  assert.equal(rememberGuideClosed(blocked), false);
  assert.equal(guideClosed(null), false);
  assert.equal(rememberGuideClosed(null), false);
});

test("a throwing localStorage accessor is treated as no storage", (t) => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new Error("SecurityError"); } });
  t.after(() => {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
    else delete globalThis.localStorage;
  });
  assert.equal(localStorageOrNull(), null);
});
