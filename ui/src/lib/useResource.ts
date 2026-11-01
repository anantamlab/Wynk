import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "@/api";

/** Failures that pass on their own (the connection, the server's storage, a server fault). */
const POLL_AGAIN = new Set(["network_error", "bad_response", "storage_error", "internal_error"]);

export type Resource<T> =
  | { state: "loading"; data: null; error: null; reload: () => void }
  | { state: "error"; data: null; error: string; /** what was thrown, e.g. an ApiError with its code */ cause: unknown; reload: () => void }
  | {
      state: "ready";
      data: T;
      error: null;
      reload: () => void;
      /** the last poll failed (e.g. the connection dropped): `data` is the last good answer */
      stale: unknown;
    };

interface Options<T> {
  /** refetch every `pollMs` while `shouldPoll(data)` is true */
  pollMs?: number;
  shouldPoll?: (data: T) => boolean;
  /** a failed poll backs off (doubling) up to this; default 8 × pollMs */
  maxPollMs?: number;
}

/**
 * Loads data through the adapter and tracks loading, error and ready states. A failed poll
 * that is passing (`POLL_AGAIN`) keeps the last good data on screen rather than blanking it,
 * marks it `stale`, and retries with exponential backoff until the server answers again. Any
 * other failure (401/403 above all) drops the data, shows the error and stops the polling.
 */
export function useResource<T>(load: () => Promise<T>, deps: unknown[], options: Options<T> = {}): Resource<T> {
  // `has`: data holds an answer (which may itself be null, e.g. "no promotion yet")
  const [state, setState] = useState<{ data: T | null; has: boolean; error: string | null; cause?: unknown; loading: boolean; stale?: unknown }>({
    data: null,
    has: false,
    error: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const opts = useRef(options);
  opts.current = options;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    setState((s) => ({ data: s.data, has: s.has, error: null, loading: !s.has, stale: s.stale }));

    const run = async () => {
      try {
        const data = await loadRef.current();
        if (!alive) return;
        failures = 0;
        setState({ data, has: true, error: null, loading: false, stale: null });
        const { pollMs, shouldPoll } = opts.current;
        if (pollMs && shouldPoll?.(data)) timer = setTimeout(run, pollMs);
      } catch (err) {
        if (!alive) return;
        const { pollMs, maxPollMs } = opts.current;
        const passing = err instanceof ApiError && POLL_AGAIN.has(err.code);
        // only a passing failure keeps the last answer (marked stale) and is polled again. A
        // refusal (401/403, not found, a contract mismatch) removes what is on screen, because the
        // person may no longer see it, and stays until they reload.
        setState((s) =>
          s.has && pollMs && passing ? { ...s, stale: err } : { data: null, has: false, error: errorMessage(err), cause: err, loading: false },
        );
        if (pollMs && passing) timer = setTimeout(run, Math.min(maxPollMs ?? pollMs * 8, pollMs * 2 ** ++failures));
      }
    };
    run();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  if (state.loading) return { state: "loading", data: null, error: null, reload };
  if (state.error !== null || !state.has)
    return { state: "error", data: null, error: state.error ?? "Nothing was returned.", cause: state.cause ?? null, reload };
  return { state: "ready", data: state.data as T, error: null, reload, stale: state.stale ?? null };
}
