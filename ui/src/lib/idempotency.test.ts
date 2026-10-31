import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Sent } from "@/api";
import { backoffMs, IDEMPOTENCY_KEY, newIdempotencyKey, sendIdempotent, useIdempotentSend, type RetryPolicy } from "./idempotency";

const err = (code: string, details: Record<string, string | number | null> = {}) => new ApiError(code, `${code} (test)`, { details });
const done = <T>(value: T, replayed = false): Sent<T> => ({ value, replayed });

/** a policy whose waits are recorded, not slept */
const policy = (retries = 3) => {
  const waits: number[] = [];
  const p: RetryPolicy = { retries, baseMs: 100, maxMs: 400, sleep: async (ms) => void waits.push(ms) };
  return { p, waits };
};

/** a send that fails with the given codes in order, then succeeds as a replay */
const script = (...codes: string[]) => {
  const keys: string[] = [];
  const send = vi.fn<(key: string) => Promise<Sent<{ id: string }>>>(async (key) => {
    keys.push(key);
    const code = codes.shift();
    if (code) throw err(code);
    return done({ id: "r-1" }, keys.length > 1);
  });
  return { send, keys };
};

describe("idempotency keys", () => {
  it("are valid for the server (1-128 of A-Z a-z 0-9 _ . : -, starting alphanumeric) and unique", () => {
    const keys = new Set(Array.from({ length: 50 }, () => newIdempotencyKey("inf")));
    expect(keys.size).toBe(50);
    for (const k of keys) expect(k).toMatch(IDEMPOTENCY_KEY);
    expect("-bad").not.toMatch(IDEMPOTENCY_KEY);
    expect("a".repeat(129)).not.toMatch(IDEMPOTENCY_KEY);
  });

  it("back off exponentially up to a ceiling", () => {
    expect([0, 1, 2, 3, 4].map((a) => backoffMs(a, { baseMs: 100, maxMs: 400 }))).toEqual([100, 200, 400, 400, 400]);
  });
});

describe("sendIdempotent", () => {
  it("retries a lost response with the SAME key and reports the server's replay", async () => {
    const { p, waits } = policy();
    const { send, keys } = script("network_error", "bad_response");
    const out = await sendIdempotent("k-1", send, p);
    expect(out).toEqual({ kind: "done", key: "k-1", value: { id: "r-1" }, replayed: true });
    expect(keys).toEqual(["k-1", "k-1", "k-1"]);
    expect(waits).toEqual([100, 200]);
  });

  it("polls inference_in_progress with backoff and the same key", async () => {
    const { p, waits } = policy(5);
    const { send, keys } = script("inference_in_progress", "inference_in_progress", "inference_in_progress");
    const onWait = vi.fn<(error: ApiError, attempt: number, ms: number) => void>();
    expect((await sendIdempotent("k-2", send, p, onWait)).kind).toBe("done");
    expect(new Set(keys)).toEqual(new Set(["k-2"]));
    expect(waits).toEqual([100, 200, 400]);
    expect(onWait.mock.calls.map((c) => [c[0].code, c[1]])).toEqual([
      ["inference_in_progress", 1],
      ["inference_in_progress", 2],
      ["inference_in_progress", 3],
    ]);
  });

  it("stops after the retries as unresolved, keeping the key for a later retry", async () => {
    const { p } = policy(2);
    const { send } = script("network_error", "network_error", "network_error", "network_error");
    const out = await sendIdempotent("k-3", send, p);
    expect(out).toMatchObject({ kind: "unresolved", key: "k-3", error: { code: "network_error" } });
    expect(send).toHaveBeenCalledTimes(3);
  });

  it.each([
    ["inference_ambiguous_attempt", "ambiguous"],
    ["ambiguous_attempt", "ambiguous"],
    ["inference_deployment_changed", "deployment_changed"],
    ["idempotency_conflict", "conflict"],
    ["invalid_inference_request", "refused"],
    ["unauthenticated", "refused"],
    ["insufficient_scope", "refused"],
    ["inference_failed", "refused"],
  ])("never resends after %s (%s)", async (code, kind) => {
    const { p, waits } = policy();
    const { send } = script(code);
    expect((await sendIdempotent("k-4", send, p)).kind).toBe(kind);
    expect(send).toHaveBeenCalledTimes(1);
    expect(waits).toEqual([]);
  });
});

describe("useIdempotentSend", () => {
  const instant: RetryPolicy = { retries: 0, baseMs: 0, maxMs: 0, sleep: async () => {} };

  it("one user intent keeps one key: resubmitting an unresolved request reuses it", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("exp", instant));
    const { send, keys } = script("network_error");
    await act(() => result.current.submit("same-form", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "unresolved" });
    await act(() => result.current.submit("same-form", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "done", replayed: true });
    expect(keys[0]).toBe(keys[1]);
  });

  it("an explicit retry reuses the key; a changed request or a finished one takes a new key", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("exp", instant));
    const { send, keys } = script("network_error");
    await act(() => result.current.submit("form-a", send));
    await act(() => result.current.retry(send));
    expect(keys[1]).toBe(keys[0]);
    await act(() => result.current.submit("form-a", send)); // done before: a new intent
    expect(keys[2]).not.toBe(keys[0]);
    const other = script("network_error");
    await act(() => result.current.submit("form-a", other.send));
    await act(() => result.current.submit("form-b", other.send)); // edited form: not the same request
    expect(other.keys[1]).not.toBe(other.keys[0]);
  });

  it("an ambiguous outcome is never retried: retry() does nothing, only sendWithNewKey sends again", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit("x", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "ambiguous" });
    await act(async () => {
      expect(await result.current.retry(send)).toBeNull();
    });
    await act(() => result.current.submit("x", send)); // the same request again is a new key, never the ambiguous one
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
    await act(() => result.current.sendWithNewKey("x", send));
    expect(new Set(keys).size).toBe(3);
  });

  it("a double click sends once", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    let release!: () => void;
    const send = vi.fn<() => Promise<Sent<{ id: string }>>>(() => new Promise((r) => (release = () => r(done({ id: "r" })))));
    await act(async () => {
      const a = result.current.submit("x", send);
      const b = result.current.submit("x", send);
      expect(await b).toBeNull();
      release();
      await a;
    });
    expect(send).toHaveBeenCalledTimes(1);
  });
});
