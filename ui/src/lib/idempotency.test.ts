import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Sent } from "@/api";
import { backoffMs, IDEMPOTENCY_KEY, newIdempotencyKey, sendIdempotent, useIdempotentSend, type Persist, type RetryPolicy } from "./idempotency";
import { readPending, requestDigest, storageKey } from "./pendingKeys";

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
    await act(() => result.current.retry());
    expect(keys[1]).toBe(keys[0]);
    await act(() => result.current.submit("form-a", send)); // done before: a new intent
    expect(keys[2]).not.toBe(keys[0]);
    const other = script("network_error");
    await act(() => result.current.submit("form-a", other.send));
    // an edited form while form-a is still unconfirmed: nothing is sent behind the person's back
    expect(result.current.blocks("form-b")).toBe("other_pending");
    await act(async () => {
      expect(await result.current.submit("form-b", other.send)).toBeNull();
    });
    expect(other.keys).toHaveLength(1);
    await act(() => result.current.sendWithNewKey("form-b", other.send)); // the deliberate new request
    expect(other.keys[1]).not.toBe(other.keys[0]);
  });

  it.each([
    ["inference_ambiguous_attempt", "ambiguous"],
    ["inference_deployment_changed", "deployment_changed"],
    ["idempotency_conflict", "conflict"],
  ])("after %s, neither retry() nor submit() of the same request sends: only sendWithNewKey does", async (code, kind) => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const { send, keys } = script(code);
    await act(() => result.current.submit("x", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind });
    expect(result.current.blocks("x")).toBe("held");
    await act(async () => {
      expect(await result.current.retry()).toBeNull();
      expect(await result.current.submit("x", send)).toBeNull(); // the main button: no silent new key
    });
    expect(keys).toHaveLength(1);
    expect(result.current.state).toMatchObject({ phase: "settled", kind }); // the warning stays on screen
  });

  it("an edited request after an ambiguous one is not sent silently either: the first one may still have run", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit("x", send));
    expect(result.current.blocks("y")).toBe("other_pending");
    await act(async () => {
      expect(await result.current.submit("y", send)).toBeNull();
    });
    expect(keys).toHaveLength(1);
  });

  it.each(["inference_deployment_changed", "idempotency_conflict"])("after %s (nothing ran), an edited request is a new intent", async (code) => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const { send, keys } = script(code);
    await act(() => result.current.submit("x", send));
    expect(result.current.blocks("y")).toBeNull();
    await act(() => result.current.submit("y", send));
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
    expect(result.current.blocks("x")).toBeNull();
  });

  it("sendWithNewKey is the deliberate way to run a held request again", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit("x", send));
    await act(() => result.current.sendWithNewKey("x", send));
    expect(new Set(keys).size).toBe(2);
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "done" });
    expect(result.current.blocks("x")).toBeNull();
  });

  it("retry() resends the ORIGINAL unresolved request, not the edited form", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant));
    const bodies: string[] = [];
    const keys: string[] = [];
    let fail = true;
    const sendFor = (body: string) => async (key: string) => {
      bodies.push(body);
      keys.push(key);
      if (fail) {
        fail = false;
        throw err("network_error");
      }
      return done({ id: body }, true);
    };
    await act(() => result.current.submit("original", sendFor("original")));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "unresolved" });
    // the person edits the form; the page's retry button calls retry() with no request
    await act(() => result.current.retry());
    expect(bodies).toEqual(["original", "original"]);
    expect(keys[1]).toBe(keys[0]);
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "done", value: { id: "original" } });
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

describe("useIdempotentSend across a reload (sessionStorage, per workspace and operation)", () => {
  const instant: RetryPolicy = { retries: 0, baseMs: 0, maxMs: 0, sleep: async () => {} };
  const BODY = JSON.stringify({ inputs: { question: "Who is the customer?", card: "4111 1111 1111 1111" } });
  const persist = (workspace = "ws-a", operation = "invoke:desk.qa") => ({ operation, workspace: async () => workspace });
  /** the tab before and after a reload: a hook unmounted, then a new one mounted */
  const mount = (p: Persist = persist()) => renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, p));
  const stored = () => Object.entries(sessionStorage).filter(([k]) => k.startsWith("wynk.pending-request.v1:"));
  beforeEach(() => sessionStorage.clear());

  it("keeps only the key, a digest and the stage, scoped to workspace and operation, before the request leaves", async () => {
    const { result } = mount();
    let seen: [string, string][] = [];
    const send = vi.fn<(key: string) => Promise<Sent<{ id: string }>>>(async () => {
      seen = stored();
      throw err("network_error");
    });
    await act(() => result.current.submit(BODY, send));
    expect(seen).toHaveLength(1); // written while the request was in flight
    expect(seen[0][0]).toBe(storageKey("ws-a", "invoke:desk.qa"));
    const record = JSON.parse(seen[0][1]);
    expect(Object.keys(record).sort()).toEqual(["at", "digest", "key", "outcome"]);
    expect(record).toMatchObject({ key: send.mock.calls[0][0], digest: requestDigest(BODY), outcome: "sending" });
    expect(seen[0][1]).not.toMatch(/customer|4111/); // no inputs, outputs or credentials
    expect(readPending("ws-a", "invoke:desk.qa")).toMatchObject({ outcome: "unresolved" });
  });

  it("a reload after a lost answer recovers the SAME key for the same values, and the server's replay clears it", async () => {
    const first = mount();
    const { send, keys } = script("network_error");
    await act(() => first.result.current.submit(BODY, send));
    first.unmount(); // the page reloads

    const after = mount();
    await waitFor(() => expect(after.result.current.state).toMatchObject({ phase: "recovered", key: keys[0], outcome: "unresolved" }));
    expect(after.result.current.blocks(BODY)).toBeNull();
    await act(() => after.result.current.submit(BODY, send)); // the person submits the same values again
    expect(keys).toEqual([keys[0], keys[0]]);
    expect(after.result.current.state).toMatchObject({ phase: "settled", kind: "done", replayed: true });
    expect(stored()).toEqual([]);
  });

  it("a request still in flight at the reload is recovered too", async () => {
    const first = mount();
    const keys: string[] = [];
    void act(() => void first.result.current.submit(BODY, (key) => (keys.push(key), new Promise<Sent<{ id: string }>>(() => {}))));
    await waitFor(() => expect(readPending("ws-a", "invoke:desk.qa")).toMatchObject({ outcome: "sending" }));
    first.unmount();
    const after = mount();
    await waitFor(() => expect(after.result.current.state).toMatchObject({ phase: "recovered", key: keys[0], outcome: "sending" }));
  });

  it("after a reload, different values are never sent silently with a new key: only the deliberate new request", async () => {
    const first = mount();
    await act(() => first.result.current.submit(BODY, script("network_error").send));
    const oldKey = readPending("ws-a", "invoke:desk.qa")!.key;
    first.unmount();

    const after = mount();
    await waitFor(() => expect(after.result.current.state.phase).toBe("recovered"));
    const { send, keys } = script();
    expect(after.result.current.blocks("other values")).toBe("other_pending");
    await act(async () => {
      expect(await after.result.current.submit("other values", send)).toBeNull();
    });
    expect(keys).toEqual([]);
    await act(() => after.result.current.sendWithNewKey("other values", send));
    expect(keys).toHaveLength(1);
    expect(keys[0]).not.toBe(oldKey);
    expect(stored()).toEqual([]); // the new request was confirmed
  });

  it("a recovered ambiguous request is held: the same values need a new key, deliberately", async () => {
    const first = mount();
    await act(() => first.result.current.submit(BODY, script("inference_ambiguous_attempt").send));
    first.unmount();
    const after = mount();
    await waitFor(() => expect(after.result.current.state).toMatchObject({ phase: "recovered", outcome: "ambiguous" }));
    const { send, keys } = script();
    expect(after.result.current.blocks(BODY)).toBe("held");
    await act(async () => {
      expect(await after.result.current.submit(BODY, send)).toBeNull();
    });
    expect(keys).toEqual([]);
  });

  it("a submit right after the reload waits for the stored record, so it cannot pick a new key first", async () => {
    const first = mount();
    const lost = script("network_error");
    await act(() => first.result.current.submit(BODY, lost.send));
    first.unmount();
    let answer!: (ws: string) => void;
    const slow = { operation: "invoke:desk.qa", workspace: () => new Promise<string>((r) => (answer = r)) };
    const after = mount(slow);
    const { send, keys } = script();
    let submitted!: Promise<unknown>;
    act(() => void (submitted = after.result.current.submit(BODY, send)));
    answer("ws-a"); // GET /workspace answers only now
    await act(() => submitted);
    expect(keys).toEqual([lost.keys[0]]);
  });

  it("a workspace that answers only at submit time still finds the stored key", async () => {
    const first = mount();
    const lost = script("network_error");
    await act(() => first.result.current.submit(BODY, lost.send));
    first.unmount();
    let calls = 0;
    const flaky = { operation: "invoke:desk.qa", workspace: async () => (calls++ === 0 ? null : "ws-a") }; // GET /workspace failed at mount
    const after = mount(flaky);
    await act(async () => {});
    expect(after.result.current.state.phase).toBe("idle");
    const { send, keys } = script();
    await act(() => after.result.current.submit(BODY, send));
    expect(keys).toEqual([lost.keys[0]]);
  });

  it.each([
    ["another workspace", persist("ws-b")],
    ["another operation", persist("ws-a", "invoke:other.qa")],
  ])("%s never sees the record", async (_, other) => {
    const first = mount();
    await act(() => first.result.current.submit(BODY, script("network_error").send));
    first.unmount();
    const elsewhere = mount(other);
    await act(async () => {});
    expect(elsewhere.result.current.state.phase).toBe("idle");
    const { send, keys } = script();
    await act(() => elsewhere.result.current.submit(BODY, send));
    expect(keys[0]).not.toBe(readPending("ws-a", "invoke:desk.qa")!.key);
  });

  it.each(["inference_deployment_changed", "idempotency_conflict", "invalid_inference_request"])("a conclusive %s removes the record", async (code) => {
    const { result } = mount();
    await act(() => result.current.submit(BODY, script(code).send));
    expect(stored()).toEqual([]);
  });

  it("without a workspace (GET /workspace refused) nothing is stored and requests still go out", async () => {
    const { result } = mount({ operation: "invoke:desk.qa", workspace: async () => null });
    const { send, keys } = script("network_error");
    await act(() => result.current.submit(BODY, send));
    expect(keys).toHaveLength(1);
    expect(stored()).toEqual([]);
  });

  it("a damaged or foreign record is dropped, never used as a key", () => {
    sessionStorage.setItem(storageKey("ws-a", "invoke:desk.qa"), JSON.stringify({ key: "-bad key", digest: "x", outcome: "sending" }));
    expect(readPending("ws-a", "invoke:desk.qa")).toBeNull();
    expect(stored()).toEqual([]);
    sessionStorage.setItem(storageKey("ws-a", "invoke:desk.qa"), "{not json");
    expect(readPending("ws-a", "invoke:desk.qa")).toBeNull();
  });

  it("reset() forgets the record (an explicit action)", async () => {
    const { result } = mount();
    await act(() => result.current.submit(BODY, script("network_error").send));
    await act(() => result.current.reset());
    expect(stored()).toEqual([]);
    expect(result.current.state.phase).toBe("idle");
  });
});
