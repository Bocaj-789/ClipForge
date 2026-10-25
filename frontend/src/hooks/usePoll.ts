import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";

/** Fetch on mount and poll while the tab is visible. No full-page refreshes. */
export function usePoll<T>(fetcher: (signal: AbortSignal) => Promise<T>, intervalMs = 5000, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  useLayoutEffect(() => {
    fetchRef.current = fetcher;
  });

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const d = await fetchRef.current(signal ?? new AbortController().signal);
      setData(d);
      setError(null);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctl = new AbortController();
    setLoading(true);
    load(ctl.signal);
    if (!intervalMs) return () => ctl.abort();
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") load(ctl.signal);
    }, intervalMs);
    return () => {
      ctl.abort();
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, load, ...deps]);

  return { data, error, loading, reload: () => load() };
}
