import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api";
import { useResource } from "./useResource";

const offline = () => new ApiError("network_error", "GET /api/v1/experiments/j failed: Failed to fetch.");
const flush = () => act(async () => {});
const advance = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));

describe("useResource polling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("keeps the last answer on screen while the connection is down, backs off, and recovers", async () => {
    const answers: (string | Error)[] = ["RUNNING", offline(), offline(), offline(), "COMPLETED"];
    const times: number[] = [];
    const load = vi.fn<() => Promise<string>>(async () => {
      times.push(Date.now());
      const a = answers.shift()!;
      if (a instanceof Error) throw a;
      return a;
    });
    const { result } = renderHook(() => useResource(load, [], { pollMs: 100, maxPollMs: 300, shouldPoll: (s: string) => s !== "COMPLETED" }));
    await flush();
    expect(result.current).toMatchObject({ state: "ready", data: "RUNNING", stale: null });

    await advance(100); // first poll fails: the data stays, marked stale
    expect(result.current).toMatchObject({ state: "ready", data: "RUNNING" });
    expect(result.current.state === "ready" && result.current.stale).toBeInstanceOf(ApiError);

    await advance(200 + 300 + 300); // backoff 200, then capped at 300
    expect(result.current).toMatchObject({ state: "ready", data: "COMPLETED", stale: null });
    expect(times.slice(1).map((t, i) => t - times[i])).toEqual([100, 200, 300, 300]);

    await advance(5000); // terminal: polling stops
    expect(load).toHaveBeenCalledTimes(5);
  });

  it("a first load that fails is an error with its cause, retried with backoff while polling", async () => {
    const load = vi.fn<() => Promise<string>>().mockRejectedValueOnce(offline()).mockResolvedValue("RUNNING");
    const { result } = renderHook(() => useResource(load, [], { pollMs: 100, shouldPoll: () => false }));
    await flush();
    expect(result.current.state).toBe("error");
    expect(result.current.state === "error" && result.current.cause).toMatchObject({ code: "network_error" });
    await advance(200);
    expect(result.current).toMatchObject({ state: "ready", data: "RUNNING" });
  });

  it("a null answer is an answer, not an error", async () => {
    const { result } = renderHook(() => useResource(async () => null, []));
    await flush();
    expect(result.current).toMatchObject({ state: "ready", data: null });
  });
});
