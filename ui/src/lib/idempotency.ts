import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Sent } from "@/api";

/*
  Idempotent requests (experiment creation, deployment inference; api/product.py and
  docs/champion_inference.md "Idempotent invocations").

  - One key per user intent: a submission gets a new key. A retry of the SAME submission (a
    dropped connection, a request still in progress, a storage hiccup) reuses that key, so the
    server answers with the original job or invocation and never runs it twice.
  - Some outcomes must never be retried automatically:
      inference_ambiguous_attempt / ambiguous_attempt  a model call may have reached the provider
      inference_deployment_changed                    the bound version no longer serves
      idempotency_conflict                            the key belongs to a different request
    The person decides; sending again always takes a NEW key and a deliberate action.
*/

/** api/product.py IDEMPOTENCY_KEY */
export const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export function newIdempotencyKey(prefix: string): string {
  const random =
    globalThis.crypto?.randomUUID?.() ??
    Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  const key = `${prefix}-${random}`;
  if (!IDEMPOTENCY_KEY.test(key)) throw new Error(`not a valid Idempotency-Key: ${key}`);
  return key;
}

/** Failures after which the same request may be sent again with the same key. */
export const RETRYABLE = new Set(["network_error", "bad_response", "storage_error", "inference_in_progress"]);
const AMBIGUOUS = new Set(["inference_ambiguous_attempt", "ambiguous_attempt"]);

export type Outcome<T> =
  | { kind: "done"; key: string; value: T; replayed: boolean }
  /** may have reached the provider: never resent automatically */
  | { kind: "ambiguous"; key: string; error: ApiError }
  /** the bound deployment changed before it ran: only a new key can run it */
  | { kind: "deployment_changed"; key: string; error: ApiError }
  /** this key was already used for a different request */
  | { kind: "conflict"; key: string; error: ApiError }
  /** still unknown after every retry (offline, still running): retry with the same key */
  | { kind: "unresolved"; key: string; error: ApiError }
  /** a conclusive refusal or failure (validation, authorization, a failed run) */
  | { kind: "refused"; key: string; error: ApiError };

export interface RetryPolicy {
  /** retries after the first attempt */
  retries: number;
  baseMs: number;
  maxMs: number;
  sleep?: (ms: number) => Promise<void>;
}

export const DEFAULT_POLICY: RetryPolicy = { retries: 5, baseMs: 500, maxMs: 8000 };
export const backoffMs = (attempt: number, p: Pick<RetryPolicy, "baseMs" | "maxMs">) => Math.min(p.maxMs, p.baseMs * 2 ** attempt);
const realSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const asApiError = (err: unknown) => (err instanceof ApiError ? err : new ApiError("internal_client_error", err instanceof Error ? err.message : String(err)));

/** Sends one request under `key`, retrying it with the SAME key while that is safe. */
export async function sendIdempotent<T>(
  key: string,
  send: (key: string) => Promise<Sent<T>>,
  policy: RetryPolicy = DEFAULT_POLICY,
  onWait?: (error: ApiError, attempt: number, ms: number) => void,
): Promise<Outcome<T>> {
  const sleep = policy.sleep ?? realSleep;
  for (let attempt = 0; ; attempt++) {
    try {
      const { value, replayed } = await send(key);
      return { kind: "done", key, value, replayed };
    } catch (err) {
      const error = asApiError(err);
      if (AMBIGUOUS.has(error.code)) return { kind: "ambiguous", key, error };
      if (error.code === "inference_deployment_changed") return { kind: "deployment_changed", key, error };
      if (error.code === "idempotency_conflict") return { kind: "conflict", key, error };
      if (!RETRYABLE.has(error.code)) return { kind: "refused", key, error };
      if (attempt >= policy.retries) return { kind: "unresolved", key, error };
      const ms = backoffMs(attempt, policy);
      onWait?.(error, attempt + 1, ms);
      await sleep(ms);
    }
  }
}

export type SendState<T> =
  | { phase: "idle" }
  | { phase: "sending"; key: string; waiting: { code: string; attempt: number } | null }
  | ({ phase: "settled" } & Outcome<T>);

/** Outcomes after which the SAME request is sent again only through `sendWithNewKey`. */
const HELD = new Set<Outcome<unknown>["kind"]>(["ambiguous", "deployment_changed", "conflict"]);

/**
 * A submit button's idempotent request. `submit(fingerprint)` starts a user intent: it reuses
 * the previous key only while the previous attempt of the SAME request is unresolved, otherwise
 * it takes a new key. After an ambiguous, changed or conflicting outcome, `submit` of the same
 * request sends nothing (`held` names it): only `sendWithNewKey()`, the deliberate "run it
 * again", can send it. `retry()` resends the ORIGINAL unresolved request (its key and its body),
 * whatever the form shows now.
 */
export function useIdempotentSend<T>(prefix: string, policy: RetryPolicy = DEFAULT_POLICY) {
  const [state, setState] = useState<SendState<T>>({ phase: "idle" });
  /** the fingerprint of a request that only `sendWithNewKey` may send again */
  const [held, setHeld] = useState<string | null>(null);
  type Send = (key: string) => Promise<Sent<T>>;
  const last = useRef<{ key: string; fingerprint: string; send: Send; outcome: Outcome<T>["kind"] | "sending" } | null>(null);
  const busy = useRef(false);
  const policyRef = useRef(policy);
  useEffect(() => {
    policyRef.current = policy;
  });

  const run = useCallback(async (key: string, fingerprint: string, send: Send) => {
    if (busy.current) return null; // a double click never sends twice
    busy.current = true;
    last.current = { key, fingerprint, send, outcome: "sending" };
    setHeld(null);
    setState({ phase: "sending", key, waiting: null });
    try {
      const outcome = await sendIdempotent(key, send, policyRef.current, (error, attempt) =>
        setState({ phase: "sending", key, waiting: { code: error.code, attempt } }),
      );
      last.current = { key, fingerprint, send, outcome: outcome.kind };
      setHeld(HELD.has(outcome.kind) ? fingerprint : null);
      setState({ phase: "settled", ...outcome });
      return outcome;
    } finally {
      busy.current = false;
    }
  }, []);

  const submit = useCallback(
    (fingerprint: string, send: Send) => {
      const prev = last.current;
      const same = prev !== null && prev.fingerprint === fingerprint;
      if (same && HELD.has(prev.outcome as Outcome<T>["kind"])) return Promise.resolve(null);
      const key = same && prev.outcome === "unresolved" ? prev.key : newIdempotencyKey(prefix);
      return run(key, fingerprint, send);
    },
    [prefix, run],
  );
  const retry = useCallback(() => {
    const prev = last.current;
    if (!prev || prev.outcome !== "unresolved") return Promise.resolve(null);
    return run(prev.key, prev.fingerprint, prev.send);
  }, [run]);
  const sendWithNewKey = useCallback((fingerprint: string, send: Send) => run(newIdempotencyKey(prefix), fingerprint, send), [prefix, run]);
  const reset = useCallback(() => {
    last.current = null;
    setHeld(null);
    setState({ phase: "idle" });
  }, []);
  return { state, held, submit, retry, sendWithNewKey, reset };
}
