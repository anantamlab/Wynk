import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Sent } from "@/api";
import { clearPending, currentWorkspaceId, type PendingOutcome, readPending, requestDigest, writePending } from "./pendingKeys";

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
  /** after a reload or a remount: an uncertain request from before it (its key and outcome only) */
  | { phase: "recovered"; key: string; outcome: PendingOutcome }
  | { phase: "sending"; key: string; waiting: { code: string; attempt: number } | null }
  | ({ phase: "settled" } & Outcome<T>);

/**
 * Why `submit` of a request sends nothing now:
 *   held           this request ended ambiguous, changed or conflicting: only a new key, deliberately
 *   other_pending  a DIFFERENT request is still uncertain: recover that one (same values, same
 *                  key) or send this one with a new key, deliberately
 */
export type Block = "held" | "other_pending" | null;

type Stage = Outcome<unknown>["kind"] | "sending";
const HELD = new Set<Stage>(["ambiguous", "deployment_changed", "conflict"]);
/** the request may have run (or may still run) and nobody knows its answer */
const UNCERTAIN = new Set<Stage>(["sending", "unresolved", "ambiguous"]);
/** the same request again keeps its key */
const REUSE = new Set<Stage>(["sending", "unresolved"]);

export function blockOf(pending: { digest: string; outcome: Stage } | null, digest: string): Block {
  if (!pending) return null;
  if (pending.digest === digest) return HELD.has(pending.outcome) ? "held" : null;
  return UNCERTAIN.has(pending.outcome) ? "other_pending" : null;
}

/** Keeps an uncertain request across a reload, per workspace and operation (lib/pendingKeys.ts). */
export interface Persist {
  /** the operation inside the workspace, e.g. `create-experiment:<project id>` */
  operation: string;
  /** default: `GET /workspace` (cached) */
  workspace?: () => Promise<string | null>;
}

/** keeps the ref (read by callbacks at once) and the state (read while rendering) together */
function put<P>(ref: { current: P | null }, set: (p: P | null) => void, p: P | null) {
  ref.current = p;
  set(p);
}

/**
 * A submit button's idempotent request.
 * - `submit(fingerprint)` starts a user intent. The SAME request (same fingerprint) keeps the key
 *   of its uncertain earlier attempt: one that is unresolved, or one that was still in flight when
 *   the page reloaded (`persist`). Otherwise it takes a new key.
 * - It sends nothing (see `blocks`) after an ambiguous, changed or conflicting outcome of the same
 *   request, or while a different request is still uncertain. Only `sendWithNewKey()`, the
 *   deliberate "new request" action, sends then.
 * - `retry()` resends the ORIGINAL unresolved request (its key and its body), whatever the form
 *   shows now. A request recovered after a reload has no body in memory: the person submits the
 *   same values again, and they go out with the stored key.
 */
export function useIdempotentSend<T>(prefix: string, policy: RetryPolicy = DEFAULT_POLICY, persist?: Persist) {
  type Send = (key: string) => Promise<Sent<T>>;
  type Pending = { key: string; digest: string; outcome: Stage; send: Send | null };
  const [state, setState] = useState<SendState<T>>({ phase: "idle" });
  const [pending, setPendingView] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const busy = useRef(false);
  const policyRef = useRef(policy);
  const workspaceRef = useRef(persist?.workspace ?? currentWorkspaceId);
  useEffect(() => {
    policyRef.current = policy;
    workspaceRef.current = persist?.workspace ?? currentWorkspaceId;
  });

  const operation = persist?.operation ?? null;
  const scopeRef = useRef<{ workspace: string; operation: string } | null>(null);
  const scope = useCallback(async () => {
    if (operation === null) return null;
    if (scopeRef.current?.operation === operation) return scopeRef.current;
    const workspace = await workspaceRef.current().catch(() => null);
    scopeRef.current = workspace ? { workspace, operation } : null;
    return scopeRef.current;
  }, [operation]);

  /** takes over the stored uncertain request of this workspace and operation, if any */
  const recoverStored = useCallback(
    async (alive: () => boolean = () => true) => {
      const s = await scope();
      if (!alive() || !s || pendingRef.current) return;
      const r = readPending(s.workspace, s.operation);
      if (!r) return;
      put(pendingRef, setPendingView, { key: r.key, digest: r.digest, outcome: r.outcome, send: null });
      setState({ phase: "recovered", key: r.key, outcome: r.outcome });
    },
    [scope],
  );
  // a submit waits for this, so it never picks a new key before a stored record is read
  const recovered = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => {
    let alive = true;
    recovered.current = recoverStored(() => alive);
    return () => {
      alive = false;
    };
  }, [recoverStored]);

  const run = useCallback(
    async (key: string, digest: string, send: Send) => {
      if (busy.current) return null; // a double click never sends twice
      busy.current = true;
      put(pendingRef, setPendingView, { key, digest, outcome: "sending", send });
      setState({ phase: "sending", key, waiting: null });
      const s = await scope(); // never throws: no workspace is `null`
      try {
        if (s) writePending(s.workspace, s.operation, { key, digest, outcome: "sending" }); // before the request leaves
        const outcome = await sendIdempotent(key, send, policyRef.current, (error, attempt) =>
          setState({ phase: "sending", key, waiting: { code: error.code, attempt } }),
        );
        put(pendingRef, setPendingView, { key, digest, outcome: outcome.kind, send });
        if (s) {
          if (outcome.kind === "unresolved" || outcome.kind === "ambiguous") writePending(s.workspace, s.operation, { key, digest, outcome: outcome.kind });
          else clearPending(s.workspace, s.operation);
        }
        setState({ phase: "settled", ...outcome });
        return outcome;
      } finally {
        busy.current = false;
      }
    },
    [scope],
  );

  const submit = useCallback(
    async (fingerprint: string, send: Send) => {
      await recovered.current;
      if (!pendingRef.current) await recoverStored(); // the workspace may only answer now
      const p = pendingRef.current;
      const digest = requestDigest(fingerprint);
      if (blockOf(p, digest)) return null;
      const key = p && p.digest === digest && REUSE.has(p.outcome) ? p.key : newIdempotencyKey(prefix);
      return run(key, digest, send);
    },
    [prefix, recoverStored, run],
  );
  const retry = useCallback(async () => {
    const p = pendingRef.current;
    if (!p || p.outcome !== "unresolved" || !p.send) return null;
    return run(p.key, p.digest, p.send);
  }, [run]);
  const sendWithNewKey = useCallback(
    async (fingerprint: string, send: Send) => {
      await recovered.current;
      return run(newIdempotencyKey(prefix), requestDigest(fingerprint), send);
    },
    [prefix, run],
  );
  const reset = useCallback(async () => {
    if (busy.current) return;
    put(pendingRef, setPendingView, null);
    setState({ phase: "idle" });
    const s = await scope();
    if (s) clearPending(s.workspace, s.operation);
  }, [scope]);
  /** why `submit(fingerprint)` would send nothing now (for the page's submit button) */
  const blocks = (fingerprint: string): Block => blockOf(pending, requestDigest(fingerprint));
  return { state, blocks, submit, retry, sendWithNewKey, reset };
}
