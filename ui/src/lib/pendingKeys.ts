import { api } from "@/api";

/*
  An uncertain idempotent request survives a reload of the tab (sessionStorage: this tab only,
  gone when the tab closes). There is one record per (workspace, operation). A record holds:
    key      the Idempotency-Key that was sent
    digest   a digest of the request, to recognise the same request again. It is never the
             request itself: no inputs, outputs or credentials are stored.
    outcome  how far it got: "sending" (sent, no answer yet), "unresolved" (no confirmed answer
             after the retries) or "ambiguous" (a model call may have reached the provider)
  A confirmed or conclusive outcome removes the record. Storage that is unavailable (blocked,
  full) only loses the reload protection; it never blocks a request.
*/

export type PendingOutcome = "sending" | "unresolved" | "ambiguous";
export interface PendingRecord {
  key: string;
  digest: string;
  outcome: PendingOutcome;
  /** epoch ms when it was written */
  at: number;
}

const PREFIX = "wynk.pending-request.v1:";
const OUTCOMES = new Set<string>(["sending", "unresolved", "ambiguous"]);
const KEY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/; // api/product.py IDEMPOTENCY_KEY

export const storageKey = (workspace: string, operation: string) => `${PREFIX}${encodeURIComponent(workspace)}:${encodeURIComponent(operation)}`;

function storage(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null; // access itself can throw (blocked storage)
  }
}

export function readPending(workspace: string, operation: string): PendingRecord | null {
  const s = storage();
  if (!s) return null;
  const name = storageKey(workspace, operation);
  try {
    const raw = s.getItem(name);
    if (raw === null) return null;
    const r = JSON.parse(raw) as Partial<PendingRecord>;
    if (typeof r.key === "string" && KEY.test(r.key) && typeof r.digest === "string" && /^[0-9a-f]{28}$/.test(r.digest) && OUTCOMES.has(r.outcome as string))
      return { key: r.key, digest: r.digest, outcome: r.outcome as PendingOutcome, at: typeof r.at === "number" ? r.at : 0 };
    s.removeItem(name); // not ours or damaged: never guess a key from it
    return null;
  } catch {
    return null;
  }
}

/** Returns false when the record could not be stored. */
export function writePending(workspace: string, operation: string, record: Omit<PendingRecord, "at">): boolean {
  try {
    const s = storage();
    if (!s) return false;
    s.setItem(storageKey(workspace, operation), JSON.stringify({ ...record, at: Date.now() }));
    return true;
  } catch {
    return false;
  }
}

export function clearPending(workspace: string, operation: string): void {
  try {
    storage()?.removeItem(storageKey(workspace, operation));
  } catch {
    // nothing to protect
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

let workspaceId: string | null = null;
/**
 * The workspace the UI server's API key belongs to (`GET /workspace`), cached after the first
 * answer. `null` when it cannot be read: records are then not kept (memory only).
 */
export async function currentWorkspaceId(): Promise<string | null> {
  if (workspaceId) return workspaceId;
  const lifecycle = api().lifecycle;
  if (!lifecycle) return null;
  try {
    workspaceId = (await lifecycle.workspace()).workspace_id;
  } catch {
    return null; // asked again next time
  }
  return workspaceId;
}

/** tests only */
export function forgetWorkspaceId(): void {
  workspaceId = null;
}
