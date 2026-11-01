import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Sent } from "@/api";
import { backoffMs, IDEMPOTENCY_KEY, newIdempotencyKey, sendIdempotent, useIdempotentSend, type Persist, type RetryPolicy } from "./idempotency";
import { loadPending, readPending, requestDigest, storageKey } from "./pendingKeys";

/** the client's own codes; every other code is the Wynk API's answer (its envelope, api/product.py ERROR_STATUS) */
const CLIENT = new Set(["network_error", "bad_response", "contract_mismatch", "request_not_sent", "internal_client_error"]);
const STATUS: Record<string, number> = { unauthenticated: 401, insufficient_scope: 403, deployment_not_found: 404, quota_exceeded: 429, inference_failed: 502 };
STATUS.storage_error = STATUS.internal_error = 500;
STATUS.inference_backend_unavailable = 503;
for (const c of ["inference_ambiguous_attempt", "ambiguous_attempt", "inference_deployment_changed", "idempotency_conflict", "inference_in_progress"]) STATUS[c] = 409;
const err = (code: string, details: Record<string, string | number | null> = {}) =>
  CLIENT.has(code)
    ? new ApiError(code, `${code} (test)`, { details, status: code === "contract_mismatch" ? 200 : null })
    : new ApiError(code, `${code} (test)`, { details, status: STATUS[code] ?? 422, fromServer: true });
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
    // it may have run: kept unresolved with its key, never resent automatically (it would fail the same way)
    ["contract_mismatch", "unresolved"],
    ["internal_client_error", "unresolved"],
    ["internal_error", "unresolved"],
    // proven before dispatch
    ["request_not_sent", "refused"],
  ])("never resends after %s (%s)", async (code, kind) => {
    const { p, waits } = policy();
    const { send } = script(code);
    expect((await sendIdempotent("k-4", send, p)).kind).toBe(kind);
    expect(send).toHaveBeenCalledTimes(1);
    expect(waits).toEqual([]);
  });

  it("an unexpected client exception after the request left is unresolved, with its key", async () => {
    const send = vi.fn<(key: string) => Promise<Sent<{ id: string }>>>(async () => {
      throw new TypeError("Cannot read properties of undefined (reading 'job_id')");
    });
    expect(await sendIdempotent("k-5", send, policy().p)).toMatchObject({ kind: "unresolved", key: "k-5", error: { code: "internal_client_error" } });
    expect(send).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["unauthenticated", "unresolved"], // given before the key is read: it says nothing about the first attempt
    ["insufficient_scope", "unresolved"],
    ["deployment_not_found", "unresolved"],
    ["quota_exceeded", "unresolved"],
    ["inference_backend_unavailable", "unresolved"],
    ["contract_mismatch", "unresolved"],
    ["request_not_sent", "unresolved"], // this attempt was not sent, the first may have run
    ["invalid_inference_request", "refused"], // the same body is refused the same way: it never ran
    ["inference_failed", "refused"], // the server's answer about this key (a FAILED run replayed)
  ])("after an attempt that may have run, %s is %s", async (code, kind) => {
    const { send, keys } = script("network_error", code);
    expect((await sendIdempotent("k-6", send, policy().p)).kind).toBe(kind);
    expect(keys).toEqual(["k-6", "k-6"]);
    const resend = script(code); // a resend of an unresolved or recovered request (mayHaveRun)
    expect((await sendIdempotent("k-6", resend.send, policy().p, undefined, true)).kind).toBe(kind);
  });
});

describe("useIdempotentSend", () => {
  const instant: RetryPolicy = { retries: 0, baseMs: 0, maxMs: 0, sleep: async () => {} };
  const P: Persist = { operation: "op", workspace: async () => "ws-alpha" };
  beforeEach(() => sessionStorage.clear());

  it("one user intent keeps one key: resubmitting an unresolved request reuses it", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("exp", instant, P));
    const { send, keys } = script("network_error");
    await act(() => result.current.submit("same-form", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "unresolved" });
    await act(() => result.current.submit("same-form", send));
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "done", replayed: true });
    expect(keys[0]).toBe(keys[1]);
  });

  it("an explicit retry reuses the key; a changed request or a finished one takes a new key", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("exp", instant, P));
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
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
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
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit("x", send));
    expect(result.current.blocks("y")).toBe("other_pending");
    await act(async () => {
      expect(await result.current.submit("y", send)).toBeNull();
    });
    expect(keys).toHaveLength(1);
  });

  it.each(["inference_deployment_changed", "idempotency_conflict"])("after %s (nothing ran), an edited request is a new intent", async (code) => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
    const { send, keys } = script(code);
    await act(() => result.current.submit("x", send));
    expect(result.current.blocks("y")).toBeNull();
    await act(() => result.current.submit("y", send));
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
    expect(result.current.blocks("x")).toBeNull();
  });

  it("sendWithNewKey is the deliberate way to run a held request again", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit("x", send));
    await act(() => result.current.sendWithNewKey("x", send));
    expect(new Set(keys).size).toBe(2);
    expect(result.current.state).toMatchObject({ phase: "settled", kind: "done" });
    expect(result.current.blocks("x")).toBeNull();
  });

  it("retry() resends the ORIGINAL unresolved request, not the edited form", async () => {
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
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
    const { result } = renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, P));
    let release!: () => void;
    const send = vi.fn<() => Promise<Sent<{ id: string }>>>(() => new Promise((r) => (release = () => r(done({ id: "r" })))));
    await act(async () => {
      const a = result.current.submit("x", send);
      const b = result.current.submit("x", send);
      expect(await b).toBeNull();
      await vi.waitFor(() => expect(send).toHaveBeenCalled()); // the record is stored first, then the request leaves
      release();
      await a;
    });
    expect(send).toHaveBeenCalledTimes(1);
  });
});

describe("useIdempotentSend across a reload (sessionStorage, per workspace and operation)", () => {
  const instant: RetryPolicy = { retries: 0, baseMs: 0, maxMs: 0, sleep: async () => {} };
  const BODY = JSON.stringify({ inputs: { question: "Who is the customer?", card: "4111 1111 1111 1111" } });
  const persist = (workspace = "ws-alpha", operation = "invoke:desk.qa") => ({ operation, workspace: async () => workspace });
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
    expect(seen[0][0]).toBe(storageKey("ws-alpha", "invoke:desk.qa"));
    const record = JSON.parse(seen[0][1]);
    expect(Object.keys(record).sort()).toEqual(["at", "digest", "key", "outcome"]);
    expect(record).toMatchObject({ key: send.mock.calls[0][0], digest: requestDigest(BODY), outcome: "sending" });
    expect(seen[0][1]).not.toMatch(/customer|4111/); // no inputs, outputs or credentials
    expect(readPending("ws-alpha", "invoke:desk.qa")).toMatchObject({ outcome: "unresolved" });
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
    await waitFor(() => expect(readPending("ws-alpha", "invoke:desk.qa")).toMatchObject({ outcome: "sending" }));
    first.unmount();
    const after = mount();
    await waitFor(() => expect(after.result.current.state).toMatchObject({ phase: "recovered", key: keys[0], outcome: "sending" }));
  });

  it("another operation on the same screen (another lineage) never takes over the first one's key", async () => {
    const { result, rerender } = renderHook(({ operation }) => useIdempotentSend<{ id: string }>("inf", instant, persist("ws-alpha", operation)), {
      initialProps: { operation: "invoke:desk.qa" },
    });
    const { send, keys } = script("network_error");
    await act(() => result.current.submit(BODY, send));
    expect(readPending("ws-alpha", "invoke:desk.qa")).toMatchObject({ key: keys[0], outcome: "unresolved" });

    rerender({ operation: "invoke:desk.other" }); // the person picks another lineage
    await act(() => result.current.submit(BODY, send)); // the same body for the other lineage
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]); // its own key: never the first lineage's
    expect(readPending("ws-alpha", "invoke:desk.qa")).toMatchObject({ key: keys[0], outcome: "unresolved" }); // kept
  });

  it("after a reload, different values are never sent silently with a new key: only the deliberate new request", async () => {
    const first = mount();
    await act(() => first.result.current.submit(BODY, script("network_error").send));
    const oldKey = readPending("ws-alpha", "invoke:desk.qa")!.key;
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
    const late = new Promise<string>((r) => (answer = r));
    const slow = { operation: "invoke:desk.qa", workspace: () => late };
    const after = mount(slow);
    const { send, keys } = script();
    let submitted!: Promise<unknown>;
    act(() => void (submitted = after.result.current.submit(BODY, send)));
    answer("ws-alpha"); // GET /workspace answers only now
    await act(() => submitted);
    expect(keys).toEqual([lost.keys[0]]);
  });

  it("a workspace that answers only at submit time still finds the stored key", async () => {
    const first = mount();
    const lost = script("network_error");
    await act(() => first.result.current.submit(BODY, lost.send));
    first.unmount();
    let calls = 0;
    const flaky = { operation: "invoke:desk.qa", workspace: async () => (calls++ === 0 ? null : "ws-alpha") }; // GET /workspace failed at mount
    const after = mount(flaky);
    await act(async () => {});
    expect(after.result.current.state.phase).toBe("idle");
    const { send, keys } = script();
    await act(() => after.result.current.submit(BODY, send));
    expect(keys).toEqual([lost.keys[0]]);
  });

  it.each([
    ["another workspace", persist("ws-beta")],
    ["another operation", persist("ws-alpha", "invoke:other.qa")],
  ])("%s never sees the record", async (_, other) => {
    const first = mount();
    await act(() => first.result.current.submit(BODY, script("network_error").send));
    first.unmount();
    const elsewhere = mount(other);
    await act(async () => {});
    expect(elsewhere.result.current.state.phase).toBe("idle");
    const { send, keys } = script();
    await act(() => elsewhere.result.current.submit(BODY, send));
    expect(keys[0]).not.toBe(readPending("ws-alpha", "invoke:desk.qa")!.key);
  });

  it.each(["inference_deployment_changed", "idempotency_conflict", "invalid_inference_request"])("a conclusive %s removes the record", async (code) => {
    const { result } = mount();
    await act(() => result.current.submit(BODY, script(code).send));
    expect(stored()).toEqual([]);
  });

  it("a damaged record is kept and reported, never used as a key; only the deliberate new request replaces it", async () => {
    const name = storageKey("ws-alpha", "invoke:desk.qa");
    sessionStorage.setItem(name, JSON.stringify({ key: "-bad key", digest: "x", outcome: "sending" }));
    expect(loadPending("ws-alpha", "invoke:desk.qa")).toEqual({ ok: false, reason: "storage_corrupted" });
    expect(readPending("ws-alpha", "invoke:desk.qa")).toBeNull();
    sessionStorage.setItem(name, "{not json");
    expect(loadPending("ws-alpha", "invoke:desk.qa")).toMatchObject({ ok: false, reason: "storage_corrupted" });

    const { result } = mount();
    const { send, keys } = script();
    await act(() => result.current.submit(BODY, send));
    expect(keys).toEqual([]); // an earlier request may have run: no silent new key
    expect(result.current.state).toMatchObject({ phase: "not_sent", reason: "storage_corrupted" });
    expect(sessionStorage.getItem(name)).toBe("{not json");
    await act(() => result.current.sendWithNewKey(BODY, send));
    expect(keys).toHaveLength(1);
    expect(stored()).toEqual([]);
  });

  it("reset() forgets the record (an explicit action)", async () => {
    const { result } = mount();
    await act(() => result.current.submit(BODY, script("network_error").send));
    await act(() => result.current.reset());
    expect(stored()).toEqual([]);
    expect(result.current.state.phase).toBe("idle");
  });
});

/*
  No durable recovery record → no request. Every case below counts the requests that left (the
  `send` spy is the only way out of the hook): it must be zero, not only an error on screen.
*/
describe("useIdempotentSend fails closed without a durable recovery record", () => {
  const instant: RetryPolicy = { retries: 0, baseMs: 0, maxMs: 0, sleep: async () => {} };
  const BODY = JSON.stringify({ inputs: { question: "Who is the customer?" } });
  const OP = "invoke:desk.qa";
  const persist = (workspace: () => Promise<string | null> = async () => "ws-alpha"): Persist => ({ operation: OP, workspace });
  const mount = (p: Persist = persist()) => renderHook(() => useIdempotentSend<{ id: string }>("inf", instant, p));
  const dom = (name: string) => new DOMException(`${name} (test)`, name);
  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  /** every action a person can take: none of them may send */
  const everyAction = async (result: { current: ReturnType<typeof useIdempotentSend<{ id: string }>> }, send: (key: string) => Promise<Sent<{ id: string }>>) => {
    await act(async () => {
      expect(await result.current.submit(BODY, send)).toBeNull();
      expect(await result.current.sendWithNewKey(BODY, send)).toBeNull();
      expect(await result.current.retry()).toBeNull();
    });
  };

  describe("storage that is missing or denied", () => {
    let saved: PropertyDescriptor | undefined;
    beforeEach(() => (saved = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage")));
    afterEach(() => {
      if (saved) Object.defineProperty(globalThis, "sessionStorage", saved);
      else delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
    });

    it.each([
      ["access throws SecurityError", () => Object.defineProperty(globalThis, "sessionStorage", { configurable: true, get: () => { throw dom("SecurityError"); } }), "SecurityError"],
      ["sessionStorage is missing", () => Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: undefined }), undefined],
    ])("%s: zero requests, storage_unavailable", async (_, block, detail) => {
      block();
      const { result } = mount();
      const { send, keys } = script();
      await everyAction(result, send);
      expect(keys).toEqual([]);
      expect(send).not.toHaveBeenCalled();
      expect(result.current.state).toEqual({ phase: "not_sent", reason: "storage_unavailable", detail });
    });
  });

  it.each(["QuotaExceededError", "SecurityError"])("setItem() throwing %s: zero requests, storage_write_failed", async (name) => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw dom(name);
    });
    const { result } = mount();
    const { send } = script();
    await everyAction(result, send);
    expect(send).not.toHaveBeenCalled();
    expect(result.current.state).toEqual({ phase: "not_sent", reason: "storage_write_failed", detail: name });
  });

  it.each([
    ["the write is silently dropped", () => vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {})],
    [
      "another value reads back",
      () => {
        const real = Storage.prototype.setItem;
        vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k: string, v: string) {
          real.call(this, k, v.replace(/"outcome":"sending"/, '"outcome":"unresolved"'));
        });
      },
    ],
    [
      "the readback throws",
      () => {
        let written = false;
        const real = { set: Storage.prototype.setItem, get: Storage.prototype.getItem };
        vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k: string, v: string) {
          written = true;
          real.set.call(this, k, v);
        });
        vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, k: string) {
          if (written) throw dom("SecurityError");
          return real.get.call(this, k);
        });
      },
    ],
  ])("a write whose readback fails (%s): zero requests, storage_verify_failed", async (_, inject) => {
    inject();
    const { result } = mount();
    const { send } = script();
    await act(async () => {
      expect(await result.current.submit(BODY, send)).toBeNull();
      expect(await result.current.sendWithNewKey(BODY, send)).toBeNull();
    });
    expect(send).not.toHaveBeenCalled();
    expect(result.current.state).toMatchObject({ phase: "not_sent", reason: "storage_verify_failed" });
  });

  it.each([
    ["401", () => Promise.reject(new ApiError("unauthenticated", "no key", { status: 401 })), "unauthenticated"],
    ["403", () => Promise.reject(new ApiError("forbidden", "no scope", { status: 403 })), "forbidden"],
    ["500", () => Promise.reject(new ApiError("internal_error", "boom", { status: 500 })), "internal_error"],
    ["a network failure", () => Promise.reject(new ApiError("network_error", "offline")), "network_error"],
    ["no workspace", async () => null, undefined],
    ["an invalid workspace id", async () => "default", "invalid_workspace_id"],
  ])("GET /workspace failing with %s: zero requests, nothing stored, workspace_unavailable", async (_, workspace, detail) => {
    const { result } = mount(persist(workspace as () => Promise<string | null>));
    const { send } = script();
    await everyAction(result, send);
    expect(send).not.toHaveBeenCalled();
    expect(Object.keys(sessionStorage)).toEqual([]);
    expect(result.current.state).toEqual({ phase: "not_sent", reason: "workspace_unavailable", detail });
  });

  it("a failure keeps the uncertain request it knows: it is not forgotten and no new key replaces it", async () => {
    const first = mount();
    const lost = script("network_error");
    await act(() => first.result.current.submit(BODY, lost.send));
    first.unmount();
    const after = mount();
    await waitFor(() => expect(after.result.current.state.phase).toBe("recovered"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw dom("QuotaExceededError");
    });
    const { send } = script();
    await act(() => after.result.current.submit(BODY, send));
    expect(send).not.toHaveBeenCalled();
    expect(after.result.current.blocks("other values")).toBe("other_pending"); // still known
    vi.restoreAllMocks(); // storage works again: the same values recover the SAME key
    await act(() => after.result.current.submit(BODY, send));
    expect(send.mock.calls.map(([k]) => k)).toEqual([lost.keys[0]]);
  });

  it("storage that fails AFTER the request left keeps the 'sending' record, so a reload still reuses the key", async () => {
    const real = Storage.prototype.setItem;
    let writes = 0;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k: string, v: string) {
      if (writes++ > 0) throw dom("QuotaExceededError"); // the first write (before the request) works
      real.call(this, k, v);
    });
    const { result } = mount();
    const { send, keys } = script("inference_ambiguous_attempt");
    await act(() => result.current.submit(BODY, send));
    expect(keys).toHaveLength(1);
    expect(readPending("ws-alpha", OP)).toMatchObject({ key: keys[0], outcome: "sending" });
  });

  it("concurrent actions while the workspace is still resolving send once", async () => {
    let answer!: (ws: string) => void;
    const late = new Promise<string>((r) => (answer = r));
    const { result } = mount(persist(() => late));
    const { send } = script();
    await act(async () => {
      const a = result.current.submit(BODY, send);
      const b = result.current.submit(BODY, send);
      const c = result.current.sendWithNewKey(BODY, send);
      expect(await b).toBeNull();
      expect(await c).toBeNull();
      answer("ws-alpha");
      await a;
    });
    expect(send).toHaveBeenCalledTimes(1);
  });

  describe("a workspace change (the UI server's key now belongs to workspace B)", () => {
    /** workspace A has an unresolved request with key kA (the answer was lost) */
    const unresolvedInA = async () => {
      let ws = "ws-alpha";
      const hook = mount(persist(async () => ws));
      const lost = script("network_error");
      await act(() => hook.result.current.submit(BODY, lost.send));
      expect(hook.result.current.state).toMatchObject({ phase: "settled", kind: "unresolved" });
      return { hook, kA: lost.keys[0], to: (w: string) => void (ws = w) };
    };

    it("neither ordinary submit nor retry sends in B: workspace_changed, zero requests, A's record kept", async () => {
      const { hook, kA, to } = await unresolvedInA();
      to("ws-beta");
      const { send } = script();
      await act(async () => {
        expect(await hook.result.current.submit(BODY, send)).toBeNull(); // the same values
        expect(await hook.result.current.submit("other values", send)).toBeNull(); // or others
        expect(await hook.result.current.retry()).toBeNull();
      });
      expect(send).not.toHaveBeenCalled();
      expect(hook.result.current.state).toEqual({ phase: "not_sent", reason: "workspace_changed" });
      expect(readPending("ws-alpha", OP)).toMatchObject({ key: kA, outcome: "unresolved" });
      expect(readPending("ws-beta", OP)).toBeNull();
    });

    it("only the explicit new request runs in B, with B's own key; back in A the original key is recovered", async () => {
      const { hook, kA, to } = await unresolvedInA();
      to("ws-beta");
      const inB = script();
      let storedInB: unknown = null;
      const sendB = vi.fn<(key: string) => Promise<Sent<{ id: string }>>>(async (key) => {
        storedInB = readPending("ws-beta", OP);
        return inB.send(key);
      });
      await act(() => hook.result.current.sendWithNewKey(BODY, sendB));
      const kB = sendB.mock.calls[0][0];
      expect(kB).not.toBe(kA);
      expect(storedInB).toMatchObject({ key: kB, outcome: "sending" }); // B's own record, before it left
      expect(readPending("ws-beta", OP)).toBeNull(); // confirmed
      expect(readPending("ws-alpha", OP)).toMatchObject({ key: kA, outcome: "unresolved" }); // untouched

      to("ws-alpha"); // the original key is back
      const inA = script();
      await act(() => hook.result.current.submit(BODY, inA.send));
      expect(inA.keys).toEqual([kA]); // recovered, never a new key
      expect(hook.result.current.state).toMatchObject({ phase: "settled", kind: "done" });
      expect(readPending("ws-alpha", OP)).toBeNull();
    });

    it("a reload in B never sends A's key there, and a reload back in A recovers it", async () => {
      const { hook, kA } = await unresolvedInA();
      hook.unmount();
      const inB = mount(persist(async () => "ws-beta"));
      await act(async () => {});
      const { send, keys } = script();
      await act(() => inB.result.current.submit(BODY, send)); // nothing uncertain is known in B
      expect(keys).toHaveLength(1);
      expect(keys[0]).not.toBe(kA);
      inB.unmount();
      const inA = mount();
      await waitFor(() => expect(inA.result.current.state).toMatchObject({ phase: "recovered", key: kA }));
    });

    it("a workspace that changes while the lookup is in flight is caught by the fresh check: zero requests", async () => {
      let answer!: (ws: string) => void;
      let calls = 0;
      const hook = mount(persist(() => (calls++ < 2 ? Promise.resolve("ws-alpha") : new Promise<string>((r) => (answer = r)))));
      const lost = script("network_error");
      await act(() => hook.result.current.submit(BODY, lost.send)); // lookups 1 (mount) and 2: A
      let retried!: Promise<unknown>;
      act(() => void (retried = hook.result.current.retry())); // lookup 3 is in flight...
      answer("ws-beta"); // ...and answers B: the key changed meanwhile
      await act(async () => {
        expect(await retried).toBeNull();
      });
      expect(lost.keys).toHaveLength(1); // the original request was not redirected to B
      expect(hook.result.current.state).toEqual({ phase: "not_sent", reason: "workspace_changed" });
    });

    it("an independent request that was confirmed in A does not hold B back", async () => {
      let ws = "ws-alpha";
      const hook = mount(persist(async () => ws));
      const first = script();
      await act(() => hook.result.current.submit(BODY, first.send));
      ws = "ws-beta";
      const second = script();
      await act(() => hook.result.current.submit(BODY, second.send));
      expect(second.keys).toHaveLength(1);
      expect(second.keys[0]).not.toBe(first.keys[0]);
    });
  });

  describe("an answer that does not decode (the server may have committed it)", () => {
    it("keeps the key and the record: the same values recover it with the same key, also after a reload", async () => {
      const first = mount();
      const { send, keys } = script("contract_mismatch");
      await act(() => first.result.current.submit(BODY, send));
      expect(first.result.current.state).toMatchObject({ phase: "settled", kind: "unresolved", error: { code: "contract_mismatch" } });
      expect(keys).toHaveLength(1); // no automatic resend
      expect(readPending("ws-alpha", OP)).toMatchObject({ key: keys[0], digest: requestDigest(BODY), outcome: "unresolved" });
      expect(first.result.current.blocks("other values")).toBe("other_pending"); // never a silent new key
      first.unmount(); // the page reloads
      const after = mount();
      await waitFor(() => expect(after.result.current.state).toMatchObject({ phase: "recovered", key: keys[0], outcome: "unresolved" }));
      await act(() => after.result.current.submit(BODY, send));
      expect(keys).toEqual([keys[0], keys[0]]);
      expect(after.result.current.state).toMatchObject({ phase: "settled", kind: "done", replayed: true });
      expect(readPending("ws-alpha", OP)).toBeNull();
    });

    it("retry() resends the original request with its key", async () => {
      const { result } = mount();
      const { send, keys } = script("contract_mismatch");
      await act(() => result.current.submit(BODY, send));
      await act(() => result.current.retry());
      expect(keys).toEqual([keys[0], keys[0]]);
      expect(result.current.state).toMatchObject({ phase: "settled", kind: "done", replayed: true });
    });

    it("a later 401 on the same key does not end it: the record and the key stay", async () => {
      const { result } = mount();
      const { send, keys } = script("contract_mismatch", "unauthenticated");
      await act(() => result.current.submit(BODY, send));
      await act(() => result.current.retry());
      expect(result.current.state).toMatchObject({ phase: "settled", kind: "unresolved", error: { code: "unauthenticated" } });
      expect(readPending("ws-alpha", OP)).toMatchObject({ key: keys[0], outcome: "unresolved" });
      await act(() => result.current.submit(BODY, send)); // the key is valid again
      expect(new Set(keys)).toEqual(new Set([keys[0]]));
    });

    it("a first-attempt 401 (it never ran) is still a refusal and clears the record", async () => {
      const { result } = mount();
      await act(() => result.current.submit(BODY, script("unauthenticated").send));
      expect(result.current.state).toMatchObject({ phase: "settled", kind: "refused" });
      expect(readPending("ws-alpha", OP)).toBeNull();
    });
  });

  it("an ambiguous request, then the explicit new request: a new key, stored and verified before it leaves", async () => {
    const { result } = mount();
    const first = script("inference_ambiguous_attempt");
    await act(() => result.current.submit(BODY, first.send));
    expect(readPending("ws-alpha", OP)).toMatchObject({ key: first.keys[0], outcome: "ambiguous" });
    let seen: unknown = null;
    const again = vi.fn<(key: string) => Promise<Sent<{ id: string }>>>(async (key) => {
      seen = readPending("ws-alpha", OP);
      return done({ id: key });
    });
    await act(() => result.current.sendWithNewKey(BODY, again));
    expect(again).toHaveBeenCalledTimes(1);
    expect(again.mock.calls[0][0]).not.toBe(first.keys[0]);
    expect(seen).toMatchObject({ key: again.mock.calls[0][0], outcome: "sending" });
  });
});
