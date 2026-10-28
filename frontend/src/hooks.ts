import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Runs `task` now and then every `delay` ms while the page is visible. A null delay runs once.
 * Calling the returned function restarts the cycle immediately (aborting an in-flight run).
 */
export function usePoll(task: (signal: AbortSignal) => Promise<void>, delay: number | null, deps: unknown[]) {
  const taskRef = useRef(task);
  taskRef.current = task;
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let stopped = false;
    let inflight = false;
    let timer: number | undefined;
    const controller = new AbortController();
    const schedule = () => {
      window.clearTimeout(timer);
      if (!stopped && delay != null) timer = window.setTimeout(run, delay);
    };
    const run = async () => {
      if (stopped || inflight) return;
      if (document.visibilityState === "hidden" && delay != null) return schedule();
      inflight = true;
      try {
        await taskRef.current(controller.signal);
      } catch {
        // Tasks record their own errors; polling continues.
      } finally {
        inflight = false;
      }
      schedule();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible" && delay != null) run();
    };
    run();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, delay, tick]);
  return useCallback(() => setTick((value) => value + 1), []);
}

export function useNow(interval = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), interval);
    return () => window.clearInterval(timer);
  }, [interval]);
  return now;
}

export function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return matches;
}

const RUN_HASH = /^#run=([a-f0-9]{8,64})$/;

/** The selected run lives in the URL hash so back/forward and deep links work without a router. */
export function useHashRun(): [string | null, (id: string | null) => void] {
  const read = () => RUN_HASH.exec(window.location.hash)?.[1] ?? null;
  const [id, setId] = useState<string | null>(read);
  useEffect(() => {
    const update = () => setId(read());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  const select = useCallback((next: string | null) => {
    if (next) {
      if (read() !== next) window.location.hash = `run=${next}`;
    } else if (window.location.hash) {
      history.pushState(null, "", window.location.pathname + window.location.search);
      setId(null);
    }
  }, []);
  return [id, select];
}

type Theme = "system" | "light" | "dark";
const THEME_KEY = "patchtrail.theme";

/** Theme is a per-viewer convenience; storage failures fall back to the system setting. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      return saved === "light" || saved === "dark" ? saved : "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      if (theme === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Not persisted; the page still works.
    }
  }, [theme]);
  const cycle = useCallback(() => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system")), []);
  return [theme, cycle];
}
