import { DependencyList, useCallback, useEffect, useRef, useState } from "react";

export interface ApiState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Load data for a screen. Re-runs when deps change. Answers that arrive
 * after a newer request was started are ignored, so quick filter changes
 * can't show stale results. The previous data stays visible while loading.
 */
export function useApi<T>(load: () => Promise<T>, deps: DependencyList): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    const id = ++latest.current;
    setLoading(true);
    setError(null);
    load().then(
      (value) => {
        if (id !== latest.current) return;
        setData(value);
        setLoading(false);
      },
      (err) => {
        if (id !== latest.current) return;
        setError(err);
        setLoading(false);
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => () => { latest.current += 1; }, []); // ignore answers after unmount

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
