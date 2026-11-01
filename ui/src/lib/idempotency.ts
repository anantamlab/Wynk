import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Sent } from "@/api";
import {
  clearPending,
  currentWorkspaceId,
  loadPending,
  type PendingOutcome,
  type RecoveryFailure,
  requestDigest,
  resolveWorkspace,
  type StoreResult,
  writePending,
} from "./pendingKeys";

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
  - Every failure falls in one of three classes (`sendIdempotent`):
      proven not sent       the request never left the browser (`request_not_sent`)
      authoritative answer  the Wynk API answered with its error envelope (`fromServer`), except
                            `internal_error`
      uncertain             it may have reached the server: a 2xx answer that does not decode
                            (`contract_mismatch`), a client error after dispatch, `internal_error`,
                            anything unknown. It stays "unresolved" and keeps its key and record.
    After an attempt with this key that may have run, a refusal the server gives before it reads
    the key (401, 403, 404, 429, 503) does not end the request either: it stays "unresolved".
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

/** Failures after which the same request may be sent again with the same key, automatically. */
export const RETRYABLE = new Set(["network_error", "bad_response", "storage_error", "inference_in_progress"]);
const AMBIGUOUS = new Set(["inference_ambiguous_attempt", "ambiguous_attempt"]);
/** proven before dispatch: the request did not leave the browser */
const NOT_SENT = new Set(["request_not_sent", "simulated_only"]);
/**
 * The Wynk API itself answered with its error envelope: an answer about this request (a 422
 * validation, a FAILED run's 502 replayed under the key, …). Not `internal_error`: the server's
 * catch-all for a fault it did not expect, which may come after the request committed.
 */
const authoritative = (e: ApiError) => e.fromServer && e.code !== "internal_error";
/**
 * Refusals the server gives BEFORE it reads the key (docs/champion_inference.md "Scope"):
 * authentication, scope, a missing target, quota, an unavailable backend, and the workspace
 * precondition (412 `workspace_mismatch`, api/tenancy.py). They say nothing about an earlier
 * attempt with the same key.
 */
const beforeKey = (e: ApiError) =>
  e.status === 401 || e.status === 403 || e.status === 404 || e.status === 412 || e.status === 429 || e.status === 503;

export type Outcome<T> =
  | { kind: "done"; key: string; value: T; replayed: boolean }
  /** may have reached the provider: never resent automatically */
  | { kind: "ambiguous"; key: string; error: ApiError }
  /** the bound deployment changed before it ran: only a new key can run it */
  | { kind: "deployment_changed"; key: string; error: ApiError }
  /** this key was already used for a different request */
  | { kind: "conflict"; key: string; error: ApiError }
  /**
   * may have run, and nobody knows its answer: offline or still running after every retry, an
   * answer that does not decode, a client error after dispatch, a server fault. Keeps its key.
   */
  | { kind: "unresolved"; key: string; error: ApiError }
  /** a conclusive refusal (validation, authorization) of a request that never ran */
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

/**
 * Sends one request under `key`, retrying it with the SAME key while that is safe.
 * `mayHaveRun`: an earlier attempt with this key may already have reached the server (a resend
 * of an unresolved or recovered request), so no refusal can end it.
 */
export async function sendIdempotent<T>(
  key: string,
  send: (key: string) => Promise<Sent<T>>,
  policy: RetryPolicy = DEFAULT_POLICY,
  onWait?: (error: ApiError, attempt: number, ms: number) => void,
  mayHaveRun = false,
): Promise<Outcome<T>> {
  const sleep = policy.sleep ?? realSleep;
  let uncertain = mayHaveRun;
  for (let attempt = 0; ; attempt++) {
    try {
      const { value, replayed } = await send(key);
      return { kind: "done", key, value, replayed };
    } catch (err) {
      const error = asApiError(err);
      if (AMBIGUOUS.has(error.code)) return { kind: "ambiguous", key, error };
      if (error.code === "inference_deployment_changed") return { kind: "deployment_changed", key, error };
      if (error.code === "idempotency_conflict") return { kind: "conflict", key, error };
      if (RETRYABLE.has(error.code)) {
        uncertain = true; // this attempt may have reached the server
        if (attempt >= policy.retries) return { kind: "unresolved", key, error };
        const ms = backoffMs(attempt, policy);
        onWait?.(error, attempt + 1, ms);
        await sleep(ms);
        continue;
      }
      const settled = uncertain ? authoritative(error) && !beforeKey(error) : NOT_SENT.has(error.code) || authoritative(error);
      if (settled) return { kind: "refused", key, error };
      // it may have run: no automatic resend (it would fail the same way), the key is kept
      return { kind: "unresolved", key, error };
    }
  }
}

/**
 * Why nothing was sent. No durable recovery record means no request: without it a lost answer
 * and a reload would lose the key, and the same values could run (and spend) twice.
 *   RecoveryFailure    the workspace or the tab's storage is not usable (lib/pendingKeys.ts)
 *   workspace_changed   the API key now belongs to another workspace than the original request
 *   workspace_mismatch  the key changed between the workspace read and the request: the server
 *                       refused it before it ran (`Wynk-Expected-Workspace`, 412)
 */
export type NotSentReason = RecoveryFailure | "workspace_changed" | "workspace_mismatch";

export type SendState<T> =
  | { phase: "idle" }
  /** after a reload or a remount: an uncertain request from before it (its key and outcome only) */
  | { phase: "recovered"; key: string; outcome: PendingOutcome }
  | { phase: "sending"; key: string; waiting: { code: string; attempt: number } | null }
  /** a precondition failed: zero requests went out */
  | { phase: "not_sent"; reason: NotSentReason; detail?: string }
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
  /** default: `GET /workspace`, asked for every request. `null` or a throw: no workspace */
  workspace?: () => Promise<string | null>;
}

/** keeps the ref (read by callbacks at once) and the state (read while rendering) together */
function put<P>(ref: { current: P | null }, set: (p: P | null) => void, p: P | null) {
  ref.current = p;
  set(p);
}

/** one action at a time: a double click, or a click during another action, sends nothing */
async function exclusive<R>(busy: { current: boolean }, action: () => Promise<R | null>): Promise<R | null> {
  if (busy.current) return null;
  busy.current = true;
  try {
    return await action();
  } finally {
    busy.current = false;
  }
}

/**
 * A submit button's idempotent request.
 * - Before ANY request leaves, the hook resolves the workspace (`GET /workspace`), stores the key,
 *   the request's digest and the stage "sending" in the tab's storage, and reads the record back.
 *   If one step fails, it sends nothing and shows `not_sent` with the reason. It never falls back
 *   to an in-memory key.
 * - `submit(fingerprint)` starts a user intent. The SAME request (same fingerprint) keeps the key
 *   of its uncertain earlier attempt: one that is unresolved, or one that was still in flight when
 *   the page reloaded. Otherwise it takes a new key.
 * - It sends nothing (see `blocks`) after an ambiguous, changed or conflicting outcome of the same
 *   request, or while a different request is still uncertain. Only `sendWithNewKey()`, the
 *   deliberate "new request" action, sends then.
 * - `retry()` resends the ORIGINAL unresolved request (its key and its body), whatever the form
 *   shows now, and only to the workspace that received it. A request recovered after a reload has
 *   no body in memory: the person submits the same values again, and they go out with the stored key.
 */
export function useIdempotentSend<T>(prefix: string, policy: RetryPolicy, persist: Persist) {
  /** `workspace`: the one just read; the request sends it as the server-enforced precondition */
  type Send = (key: string, workspace: string) => Promise<Sent<T>>;
  type Pending = { workspace: string; operation: string; key: string; digest: string; outcome: Stage; send: Send | null };
  const [state, setState] = useState<SendState<T>>({ phase: "idle" });
  const [pending, setPendingView] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const busy = useRef(false);
  const policyRef = useRef(policy);
  const workspaceRef = useRef(persist.workspace ?? currentWorkspaceId);
  useEffect(() => {
    policyRef.current = policy;
    workspaceRef.current = persist.workspace ?? currentWorkspaceId;
  });
  const operation = persist.operation;

  /** the uncertain request in memory belongs to this workspace (from its stored record, if any) */
  const adopt = useCallback(
    (workspace: string): StoreResult<null> => {
      const p = pendingRef.current;
      if (p?.workspace === workspace && p.operation === operation) return { ok: true, value: null };
      const r = loadPending(workspace, operation);
      if (!r.ok) return r;
      put(pendingRef, setPendingView, r.value ? { workspace, operation, key: r.value.key, digest: r.value.digest, outcome: r.value.outcome, send: null } : null);
      if (r.value) setState({ phase: "recovered", key: r.value.key, outcome: r.value.outcome });
      return { ok: true, value: null };
    },
    [operation],
  );
  // a submit waits for this, so it never picks a new key before a stored record is read
  const recovered = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => {
    let alive = true;
    recovered.current = (async () => {
      const ws = await resolveWorkspace(workspaceRef.current);
      if (alive && ws.ok && !pendingRef.current) adopt(ws.value); // a failure is shown by the next submit
    })();
    return () => {
      alive = false;
    };
  }, [adopt]);

  /**
   * Stores and verifies the record, THEN sends. A failure sends nothing. `reused`: the key comes
   * from an earlier, uncertain attempt, so no refusal can end it (`sendIdempotent`).
   */
  const run = useCallback(
    async (workspace: string, key: string, digest: string, send: Send, reused: boolean) => {
      const stored = writePending(workspace, operation, { key, digest, outcome: "sending" });
      if (!stored.ok) {
        setState({ phase: "not_sent", reason: stored.reason, detail: stored.detail });
        return null;
      }
      put(pendingRef, setPendingView, { workspace, operation, key, digest, outcome: "sending", send });
      setState({ phase: "sending", key, waiting: null });
      const outcome = await sendIdempotent(
        key,
        (k) => send(k, workspace),
        policyRef.current,
        (error, attempt) => setState({ phase: "sending", key, waiting: { code: error.code, attempt } }),
        reused,
      );
      put(pendingRef, setPendingView, { workspace, operation, key, digest, outcome: outcome.kind, send });
      // if this update fails, the "sending" record stays: after a reload the same values still reuse the key
      if (outcome.kind === "unresolved" || outcome.kind === "ambiguous") writePending(workspace, operation, { key, digest, outcome: outcome.kind });
      else clearPending(workspace, operation);
      if (outcome.kind === "refused" && outcome.error.code === "workspace_mismatch") {
        // the server proved it ran nowhere: no workspace received it, nothing was spent
        setState({ phase: "not_sent", reason: "workspace_mismatch", detail: outcome.error.message });
        return outcome;
      }
      setState({ phase: "settled", ...outcome });
      return outcome;
    },
    [operation],
  );

  /** the workspace that will receive the request; on failure the reason is shown and nothing is sent */
  const target = useCallback(async () => {
    const ws = await resolveWorkspace(workspaceRef.current);
    if (!ws.ok) setState({ phase: "not_sent", reason: ws.reason, detail: ws.detail });
    return ws.ok ? ws.value : null;
  }, []);

  const submit = useCallback(
    (fingerprint: string, send: Send) =>
      exclusive(busy, async () => {
        await recovered.current;
        const workspace = await target(); // a fresh check: the workspace may have changed while it was asked
        if (workspace === null) return null;
        const mine = pendingRef.current;
        if (mine && mine.operation === operation && mine.workspace !== workspace && UNCERTAIN.has(mine.outcome)) {
          // the uncertain request belongs to another workspace: it is neither resent nor replaced
          // here. Its record stays for that workspace; only the deliberate new request runs here.
          setState({ phase: "not_sent", reason: "workspace_changed" });
          return null;
        }
        const own = adopt(workspace); // the workspace may only answer now
        if (!own.ok) {
          setState({ phase: "not_sent", reason: own.reason, detail: own.detail });
          return null;
        }
        const p = pendingRef.current;
        const digest = requestDigest(fingerprint);
        if (blockOf(p, digest)) return null;
        const reuse = p !== null && p.digest === digest && REUSE.has(p.outcome);
        return run(workspace, reuse ? p.key : newIdempotencyKey(prefix), digest, send, reuse);
      }),
    [adopt, operation, prefix, run, target],
  );
  const retry = useCallback(
    () =>
      exclusive(busy, async () => {
        const p = pendingRef.current;
        if (!p || p.outcome !== "unresolved" || !p.send) return null;
        const workspace = await target();
        if (workspace === null) return null;
        if (workspace !== p.workspace) {
          setState({ phase: "not_sent", reason: "workspace_changed" }); // its key means nothing to the other workspace
          return null;
        }
        return run(workspace, p.key, p.digest, p.send, true);
      }),
    [run, target],
  );
  /** the deliberate new request: a new key, even while another request is uncertain or held */
  const sendWithNewKey = useCallback(
    (fingerprint: string, send: Send) =>
      exclusive(busy, async () => {
        await recovered.current;
        const workspace = await target();
        if (workspace === null) return null;
        return run(workspace, newIdempotencyKey(prefix), requestDigest(fingerprint), send, false);
      }),
    [prefix, run, target],
  );
  /** forgets the uncertain request (an explicit action) */
  const reset = useCallback(
    () =>
      exclusive(busy, async () => {
        const known = pendingRef.current?.workspace;
        put(pendingRef, setPendingView, null);
        setState({ phase: "idle" });
        const ws = await resolveWorkspace(workspaceRef.current);
        for (const w of new Set([known, ws.ok ? ws.value : undefined])) if (w) clearPending(w, operation);
        return null;
      }),
    [operation],
  );
  /** why `submit(fingerprint)` would send nothing now (for the page's submit button) */
  const blocks = (fingerprint: string): Block => blockOf(pending, requestDigest(fingerprint));
  return { state, blocks, submit, retry, sendWithNewKey, reset };
}
