import { api, ApiError } from "@/api";

/*
  An uncertain idempotent request survives a reload of the tab (sessionStorage: this tab only,
  gone when the tab closes). There is one record per (workspace, operation). A record holds:
    key      the Idempotency-Key that was sent
    digest   a digest of the request, to recognise the same request again. It is never the
             request itself: no inputs, outputs or credentials are stored.
    outcome  how far it got: "sending" (sent, no answer yet), "unresolved" (no confirmed answer
             after the retries) or "ambiguous" (a model call may have reached the provider)
  A confirmed or conclusive outcome removes the record.

  The record is the only way to find the key again after a reload. So it is a precondition:
  no stored and verified record, no request (lib/idempotency.ts). Storage that is unavailable,
  full, denied or damaged makes these operations fail closed; it never falls back to memory.
*/

export type PendingOutcome = "sending" | "unresolved" | "ambiguous";
export interface PendingRecord {
  key: string;
  digest: string;
  outcome: PendingOutcome;
  /** epoch ms when it was written */
  at: number;
}

/**
 * Why a request cannot have a durable recovery record, so it is not sent:
 *   workspace_unavailable  GET /workspace failed or answered no valid workspace id
 *   storage_unavailable    sessionStorage is missing or access to it is denied
 *   storage_write_failed   the write threw (quota full, security error)
 *   storage_verify_failed  the write did not read back the same
 *   storage_corrupted      a stored record is damaged: its key cannot be recovered
 */
export type RecoveryFailure = "workspace_unavailable" | "storage_unavailable" | "storage_write_failed" | "storage_verify_failed" | "storage_corrupted";
/** `detail`: the browser's or the API's error name or code, for the person and for support */
export type StoreResult<T> = { ok: true; value: T } | { ok: false; reason: RecoveryFailure; detail?: string };

const PREFIX = "wynk.pending-request.v1:";
const OUTCOMES = new Set<string>(["sending", "unresolved", "ambiguous"]);
const KEY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/; // api/product.py IDEMPOTENCY_KEY
const DIGEST = /^[0-9a-f]{28}$/;
const WORKSPACE = /^ws-[0-9a-z][0-9a-z-]{1,62}$/; // store/tenancy.py WORKSPACE_ID

export const storageKey = (workspace: string, operation: string) => `${PREFIX}${encodeURIComponent(workspace)}:${encodeURIComponent(operation)}`;

/** an Error or a DOMException (from any realm): its name, e.g. QuotaExceededError */
const nameOf = (e: unknown) => (typeof e === "object" && e !== null && typeof (e as { name?: unknown }).name === "string" ? (e as { name: string }).name : undefined);

function storage(): StoreResult<Storage> {
  try {
    const s = globalThis.sessionStorage;
    return s ? { ok: true, value: s } : { ok: false, reason: "storage_unavailable" };
  } catch (e) {
    return { ok: false, reason: "storage_unavailable", detail: nameOf(e) }; // access itself can throw (blocked storage)
  }
}

function parse(raw: string): PendingRecord | null {
  try {
    const r = JSON.parse(raw) as Partial<PendingRecord> | null;
    if (r && typeof r.key === "string" && KEY.test(r.key) && typeof r.digest === "string" && DIGEST.test(r.digest) && OUTCOMES.has(r.outcome as string))
      return { key: r.key, digest: r.digest, outcome: r.outcome as PendingOutcome, at: typeof r.at === "number" ? r.at : 0 };
  } catch {
    // damaged
  }
  return null;
}

/**
 * The stored record of this workspace and operation (`null`: there is none). A damaged record
 * is `storage_corrupted`: it is kept, never read as a key, and only an explicit new request or
 * `clearPending` replaces it.
 */
export function loadPending(workspace: string, operation: string): StoreResult<PendingRecord | null> {
  const s = storage();
  if (!s.ok) return s;
  let raw: string | null;
  try {
    raw = s.value.getItem(storageKey(workspace, operation));
  } catch (e) {
    return { ok: false, reason: "storage_unavailable", detail: nameOf(e) };
  }
  if (raw === null) return { ok: true, value: null };
  const record = parse(raw);
  return record ? { ok: true, value: record } : { ok: false, reason: "storage_corrupted" };
}

/** the record, or `null` when there is none or it cannot be read */
export function readPending(workspace: string, operation: string): PendingRecord | null {
  const r = loadPending(workspace, operation);
  return r.ok ? r.value : null;
}

/** Stores the record and reads it back. `ok` only when the stored copy is exactly this record. */
export function writePending(workspace: string, operation: string, record: Omit<PendingRecord, "at">): StoreResult<PendingRecord> {
  const s = storage();
  if (!s.ok) return s;
  const name = storageKey(workspace, operation);
  const full: PendingRecord = { key: record.key, digest: record.digest, outcome: record.outcome, at: Date.now() };
  const raw = JSON.stringify(full);
  try {
    s.value.setItem(name, raw);
  } catch (e) {
    return { ok: false, reason: "storage_write_failed", detail: nameOf(e) };
  }
  try {
    const back = s.value.getItem(name);
    const again = back === null ? null : parse(back);
    if (back !== raw || !again || again.key !== full.key || again.digest !== full.digest || again.outcome !== full.outcome)
      return { ok: false, reason: "storage_verify_failed" };
  } catch (e) {
    return { ok: false, reason: "storage_verify_failed", detail: nameOf(e) };
  }
  return { ok: true, value: full };
}

/** `false` when the record could not be removed (it then stays: a later submit reuses its key) */
export function clearPending(workspace: string, operation: string): boolean {
  const s = storage();
  if (!s.ok) return false;
  try {
    s.value.removeItem(storageKey(workspace, operation));
    return true;
  } catch {
    return false;
  }
}

/** cyrb53: a fast, well-mixed 53-bit string hash */
function cyrb53(text: string, seed: number): number {
  let h1 = 0xdeadbeef ^ seed;
  let h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

/**
 * Recognises the same request again: 2 × 53 bits, as 28 hex digits. It is synchronous so the page
 * can disable a button while it renders. It identifies a request; it does not hide one, so it is
 * not a cryptographic hash, and the request itself is never stored.
 */
export function requestDigest(fingerprint: string): string {
  return [cyrb53(fingerprint, 1), cyrb53(fingerprint, 2)].map((n) => n.toString(16).padStart(14, "0")).join("");
}

export const validWorkspaceId = (id: unknown): id is string => typeof id === "string" && WORKSPACE.test(id);

/**
 * The workspace the UI server's API key belongs to (`GET /workspace`). It is asked again for
 * every request, so a record is always stored under the workspace that receives the request.
 * It throws the API's error when the workspace cannot be read.
 */
export async function currentWorkspaceId(): Promise<string> {
  const lifecycle = api().lifecycle;
  if (!lifecycle) throw new ApiError("workspace_unavailable", "The workspace API is not available.");
  return (await lifecycle.workspace()).workspace_id;
}

/** Resolves and validates the workspace; any failure is `workspace_unavailable`. */
export async function resolveWorkspace(read: () => Promise<string | null> = currentWorkspaceId): Promise<StoreResult<string>> {
  try {
    const id = await read();
    return validWorkspaceId(id) ? { ok: true, value: id } : { ok: false, reason: "workspace_unavailable", detail: id === null ? undefined : "invalid_workspace_id" };
  } catch (e) {
    return { ok: false, reason: "workspace_unavailable", detail: e instanceof ApiError ? e.code : nameOf(e) };
  }
}
