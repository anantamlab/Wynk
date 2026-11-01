import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setApi } from "@/api";
import { createLiveApi } from "@/api/live";
import { createMockApi } from "@/api/mock/mockApi";
import { routes } from "@/router";
import { LIFECYCLE, fail, lc, ok, recorded, server, v1, type Call, type Reply } from "@/test/lifecycleApi";
import { body as v1Body } from "@/test/productApi";

/*
  The live screens against the product API's real lifecycle responses. Each test edits a recorded
  response only to put the server in the state under test (interrupted, ambiguous, stale…).
*/
const P = "/api/v1";
const PID = v1Body("create_project").project_id as string;
const JOB = lc("get_experiment").job_id as string;
const LINEAGE = lc("promote_experiment").lineage_id as string; // order-desk.order-desk
const VERSION = lc("publish_workflow").version_id as string;
const T = { timeout: 3000 };

/** the project's dataset list, renamed to the lifecycle fixture's dataset (order-desk) */
const datasetsAs = (id: string) => ok(JSON.parse(JSON.stringify(v1Body("list_datasets")).replaceAll("support-tickets", id)));

const shell = {
  [`GET ${P}/projects`]: v1("list_projects"),
  [`GET ${P}/projects/${PID}`]: v1("get_project"),
  [`GET ${P}/workspace`]: recorded("current_workspace"),
  [`GET ${P}/projects/${PID}/datasets`]: datasetsAs("order-desk"),
};

function renderLive(path: string, table: Parameters<typeof server>[0]) {
  const s = server({ ...shell, ...table });
  setApi(createLiveApi({ fetch: s.fetch }));
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  return s;
}

const jobWith = (edit: (j: ReturnType<typeof lc>) => void) => {
  const j = lc("get_experiment");
  edit(j);
  return j;
};
const posts = (calls: Call[], route: string) => calls.filter((c) => c.route === `POST ${P}${route}`);

afterEach(() => {
  setApi(null);
  vi.unstubAllGlobals();
});

describe("live experiments", () => {
  it("lists the workspace's real jobs over this project's datasets, never a simulated notice", async () => {
    const jobs = lc("list_experiments");
    const foreign = { ...jobs.jobs[0], job_id: "j-000000000000000000000000", dataset_id: "another-project-dataset" };
    const s = renderLive(`/projects/${PID}/experiments`, { [`GET ${P}/experiments`]: ok({ jobs: [...jobs.jobs, foreign] }) });
    const rows = await screen.findAllByTestId("job-row", {}, T);
    expect(rows.map((r) => r.dataset.jobId)).toEqual([JOB]);
    expect(within(rows[0]).getByTestId("job-state")).toHaveAttribute("data-state", "COMPLETED");
    expect(within(rows[0]).getByText(LINEAGE)).toBeInTheDocument();
    expect(screen.queryByTestId("simulated-notice")).not.toBeInTheDocument();
    expect(screen.queryByTestId("experiment-backend-required")).not.toBeInTheDocument();
    expect(await screen.findByTestId("workspace")).toHaveTextContent("Workspace Fixture · owner");
    // the UI never names a workspace or carries a credential: the server scopes by the proxy's key
    for (const c of s.calls) {
      expect(c.headers.get("Authorization")).toBeNull();
      expect(c.route).not.toMatch(/ws-|workspace_id/);
    }
  });

  it("creates one experiment per intent: a dropped response is retried with the same key and the replay opens that job", async () => {
    const versionOne = v1Body("list_splits").splits[0].splits_hash as string;
    const s = renderLive(`/projects/${PID}/experiments/new?dataset=support-tickets`, {
      [`GET ${P}/projects/${PID}/datasets`]: v1("list_datasets"),
      [`GET ${P}/models`]: recorded("list_models"),
      [`GET ${P}/datasets/support-tickets/versions/1`]: v1("get_version"),
      [`GET ${P}/datasets/support-tickets/versions/1/splits`]: v1("list_splits"),
      [`POST ${P}/experiments`]: [new TypeError("Failed to fetch"), recorded("create_experiment", { "Idempotent-Replayed": "true" })],
      [`GET ${P}/experiments/${JOB}`]: recorded("get_experiment"),
    });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Dataset version", {}, T), "1");
    await user.type(screen.getByLabelText("Instructions"), "Route the ticket.");
    const submit = screen.getByRole("button", { name: "Create experiment" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    expect(await screen.findByTestId("create-replayed", {}, T)).toHaveTextContent("No second experiment was created");
    expect(screen.getByTestId("live-experiment")).toHaveAttribute("data-job-id", JOB);
    const sent = posts(s.calls, "/experiments");
    expect(sent).toHaveLength(2);
    const [k1, k2] = sent.map((c) => c.headers.get("Idempotency-Key"));
    expect(k1).toMatch(/^exp/);
    expect(k2).toBe(k1);
    expect(sent[1].body).toEqual(sent[0].body);
    const b = sent[0].body as { dataset_version: number; splits_hash: string; plan: { expected_model_hash: string }; contract: { constraints: object } };
    expect(b.dataset_version).toBe(1);
    expect(b.splits_hash).toBe(versionOne);
    expect(b.plan.expected_model_hash).toBe(lc("list_models").models[0].model_hash);
    expect(b.contract.constraints).toMatchObject({ maximum_model_calls: expect.any(Number), maximum_retries: expect.any(Number) });
  });

  it("an interrupted job shows the server's reason and unproven calls; Resume is a deliberate action", async () => {
    let resumed = false;
    const interrupted = jobWith((j) => {
      j.state = "INTERRUPTED";
      j.reason = "process_lost";
      j.units[0].state = "INTERRUPTED";
      j.units[0].attempts.in_flight = 1;
    });
    const s = renderLive(`/projects/${PID}/experiments/${JOB}`, {
      [`GET ${P}/experiments/${JOB}`]: () => (resumed ? recorded("get_experiment") : ok(interrupted)),
      [`POST ${P}/experiments/${JOB}/resume`]: () => {
        resumed = true;
        return ok(jobWith((j) => (j.state = "RUNNING")));
      },
    });
    const reason = await screen.findByTestId("job-reason", {}, T);
    expect(reason).toHaveAttribute("data-reason", "process_lost");
    expect(screen.getByTestId("in-flight")).toHaveTextContent("1 model call started without a stored result");
    expect(posts(s.calls, `/experiments/${JOB}/resume`)).toHaveLength(0);
    await userEvent.setup().click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(screen.getByTestId("live-experiment")).toHaveAttribute("data-state", "COMPLETED"));
    expect(posts(s.calls, `/experiments/${JOB}/resume`)).toHaveLength(1);
  });

  it("a resume refused as ambiguous says so and is not resent", async () => {
    const interrupted = jobWith((j) => {
      j.state = "INTERRUPTED";
      j.reason = "ambiguous_attempt";
    });
    const s = renderLive(`/projects/${PID}/experiments/${JOB}`, {
      [`GET ${P}/experiments/${JOB}`]: ok(interrupted),
      [`POST ${P}/experiments/${JOB}/resume`]: fail(409, "ambiguous_attempt", "a model call may have reached the provider", { run_id: "r-1" }),
    });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Resume" }, T));
    const alert = await screen.findByText("Could not resume this experiment");
    expect(alert.closest("[role=alert]")).toHaveTextContent("ambiguous_attempt");
    expect(posts(s.calls, `/experiments/${JOB}/resume`)).toHaveLength(1);
  });

  it("keeps the last state on screen and says so while the API is unreachable", async () => {
    const running = jobWith((j) => (j.state = "RUNNING"));
    renderLive(`/projects/${PID}/experiments/${JOB}`, {
      [`GET ${P}/experiments/${JOB}`]: [ok(running), new TypeError("Failed to fetch")],
    });
    expect(await screen.findByTestId("live-experiment", {}, T)).toHaveAttribute("data-state", "RUNNING");
    expect(await screen.findByTestId("stale", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByTestId("live-experiment")).toHaveAttribute("data-state", "RUNNING");
  });
});

describe("live results and promotion", () => {
  it("shows the server's per-strategy comparison and provenance, and promotes on request", async () => {
    let promoted = false;
    const s = renderLive(`/projects/${PID}/experiments/${JOB}/results`, {
      [`GET ${P}/experiments/${JOB}/artifact`]: recorded("get_artifact"),
      [`GET ${P}/experiments/${JOB}/verify`]: recorded("verify_artifact"),
      [`GET ${P}/experiments/${JOB}/provenance`]: recorded("get_provenance"),
      [`GET ${P}/experiments/${JOB}/promotion`]: () => (promoted ? recorded("promote_experiment") : fail(404, "promotion_not_found")),
      [`POST ${P}/experiments/${JOB}/promote`]: () => {
        promoted = true;
        return recorded("promote_experiment");
      },
    });
    const rows = await screen.findAllByTestId("result-row", {}, T);
    const runs = lc("get_artifact").artifact.runs as { strategy: string; champion: { validation: { score_mean: number } } }[];
    expect(rows.map((r) => r.dataset.strategy)).toEqual(runs.map((r) => r.strategy));
    rows.forEach((r, i) => expect(within(r).getByTestId("result-score")).toHaveTextContent(runs[i].champion.validation.score_mean.toFixed(3)));
    expect(screen.getByTestId("verified")).toHaveTextContent("Verified by the server");
    expect(screen.getByTestId("test-runs")).toHaveTextContent("Test-split runs during the search: 0");
    expect(screen.getByTestId("artifact-id")).toHaveTextContent(lc("get_artifact").artifact_id);

    await userEvent.setup().click(await screen.findByRole("button", { name: /Promote on the held-out test split/ }));
    const p = await screen.findByTestId("promotion");
    expect(p).toHaveAttribute("data-decision", "PROMOTED");
    expect(within(p).getByTestId("heldout-score")).toHaveTextContent(lc("promote_experiment").record.heldout.challenger.score_mean.toFixed(3));
    expect(posts(s.calls, `/experiments/${JOB}/promote`)).toHaveLength(1);
  });
});

/** A one-lineage deployment server: moves are fenced by revision exactly like store/deployments.py. */
function deploymentServer(start: "published" | "production") {
  const version = lc("publish_workflow");
  const events = lc("get_deployment_history").events as { revision_after: number }[];
  const state = {
    version: { ...version, state: start === "production" ? "PRODUCTION" : "CREATED" },
    dep: start === "production" ? lc("promote_workflow") : lc("get_deployment"),
  };
  const move = (call: Call, to: string, next: string): Reply => {
    const rev = (call.body as { expected_revision: number }).expected_revision;
    if (rev !== state.dep.revision) return recorded("error_stale_deployment");
    state.version.state = to;
    state.dep = lc(next);
    return ok(state.dep);
  };
  const table = {
    [`GET ${P}/experiments`]: recorded("list_experiments"),
    [`GET ${P}/champions/${LINEAGE}`]: recorded("get_champion"),
    [`GET ${P}/workflows?lineage=${encodeURIComponent(LINEAGE)}`]: () => ok({ versions: [state.version] }),
    [`GET ${P}/deployments/${LINEAGE}/history`]: () => {
      // the history view carries the pointers and events, not the deployment's timestamps
      const { lineage_id, revision, staging_version_id, production_version_id } = state.dep;
      return ok({ lineage_id, revision, staging_version_id, production_version_id, events: events.filter((e) => e.revision_after <= revision) });
    },
    [`POST ${P}/workflows/${VERSION}/stage`]: (c: Call) => move(c, "STAGING", "stage_workflow"),
    [`POST ${P}/workflows/${VERSION}/promote`]: (c: Call) => move(c, "PRODUCTION", "promote_workflow"),
  };
  return { state, table };
}

describe("live workflows and deployment", () => {
  it("stages and promotes with the revision it read", async () => {
    const d = deploymentServer("published");
    const s = renderLive(`/projects/${PID}/workflows`, d.table);
    const user = userEvent.setup();
    const lineage = await screen.findByTestId("lineage", {}, T);
    expect(lineage).toHaveAttribute("data-lineage", LINEAGE);
    expect(within(lineage).getByTestId("lineage-champion")).toHaveTextContent(lc("get_champion").champion_id);
    await user.click(within(lineage).getByRole("button", { name: "Stage" }));
    await user.click(await within(lineage).findByRole("button", { name: "Promote to production" }));
    await waitFor(() => expect(within(lineage).getByTestId("production-version")).toHaveTextContent(VERSION));
    expect(within(lineage).getByTestId("deployment-revision")).toHaveTextContent("2");
    expect(s.calls.filter((c) => c.route.startsWith("POST")).map((c) => [c.route, c.body])).toEqual([
      [`POST ${P}/workflows/${VERSION}/stage`, { expected_revision: 0 }],
      [`POST ${P}/workflows/${VERSION}/promote`, { expected_revision: 1 }],
    ]);
  });

  it("shows stale_deployment when someone moved the lineage first, changes nothing, and shows the current revision", async () => {
    const d = deploymentServer("published");
    renderLive(`/projects/${PID}/workflows`, d.table);
    const lineage = await screen.findByTestId("lineage", {}, T);
    await within(lineage).findByRole("button", { name: "Stage" });
    // another operator stages the version behind this page's back
    d.state.dep = lc("stage_workflow");
    d.state.version.state = "CREATED";
    await userEvent.setup().click(within(lineage).getByRole("button", { name: "Stage" }));
    const error = await within(lineage).findByTestId("deployment-error");
    expect(error).toHaveTextContent("Someone changed this deployment first");
    expect(within(error).getByTestId("error-code")).toHaveTextContent("stale_deployment");
    await waitFor(() => expect(lineage).toHaveAttribute("data-revision", "1"));
  });
});

describe("live invocation (Phase 1 idempotent contract)", () => {
  const fill = async () => {
    const user = userEvent.setup();
    const panel = await screen.findByTestId("invoke-panel", {}, T);
    for (const [name, value] of Object.entries(LIFECYCLE.new_order)) await user.type(within(panel).getByRole("textbox", { name: new RegExp(`^${name}`) }), value);
    return { user, panel };
  };
  const invokes = (calls: Call[]) => posts(calls, `/deployments/${LINEAGE}/invoke`);

  it("an ambiguous invocation is never resent; only the explicit new-key action runs it again", async () => {
    const d = deploymentServer("production");
    const s = renderLive(`/projects/${PID}/workflows`, {
      ...d.table,
      [`POST ${P}/deployments/${LINEAGE}/invoke`]: [
        fail(409, "inference_ambiguous_attempt", "The model call may have reached the provider.", { inference_id: "inf-1", state: "RUNNING" }),
        recorded("invoke_production"),
      ],
    });
    const { user, panel } = await fill();
    await user.click(within(panel).getByRole("button", { name: /^Run on production/ }));
    const status = await within(panel).findByTestId("send-status");
    await waitFor(() => expect(status).toHaveAttribute("data-phase", "ambiguous"));
    expect(status).toHaveTextContent("Outcome unknown");
    await new Promise((r) => setTimeout(r, 700)); // longer than the first backoff: nothing resends
    expect(invokes(s.calls)).toHaveLength(1);
    // the main button cannot send the same request again with a silent new key
    expect(within(panel).getByRole("button", { name: /^Run on production/ })).toBeDisabled();

    await user.click(within(panel).getByRole("button", { name: "Run again with a new key" }));
    expect(await within(panel).findByTestId("inference-output")).toHaveTextContent('"answer": "Lincoln"');
    const [k1, k2] = invokes(s.calls).map((c) => c.headers.get("Idempotency-Key"));
    expect(k1).toMatch(/^inf/);
    expect(k2).not.toBe(k1);
    expect(invokes(s.calls).map((c) => c.body)).toEqual([{ inputs: LIFECYCLE.new_order }, { inputs: LIFECYCLE.new_order }]);
  });

  it("polls inference_in_progress with the same key and shows the server's replayed output", async () => {
    const d = deploymentServer("production");
    const s = renderLive(`/projects/${PID}/workflows`, {
      ...d.table,
      [`POST ${P}/deployments/${LINEAGE}/invoke`]: [
        fail(409, "inference_in_progress", "still running", { inference_id: "inf-1", state: "RUNNING" }),
        recorded("invoke_production_replay", { "Idempotent-Replayed": "true" }),
      ],
    });
    const { user, panel } = await fill();
    await user.click(within(panel).getByRole("button", { name: /^Run on production/ }));
    expect(await within(panel).findByText(/still running on the server; checking again with the same key/)).toBeInTheDocument();
    expect(await within(panel).findByTestId("inference-output", {}, T)).toHaveTextContent("Lincoln");
    expect(within(panel).getByTestId("send-status")).toHaveAttribute("data-phase", "replayed");
    expect(within(panel).getByTestId("inference")).toHaveAttribute("data-inference-id", lc("invoke_production_replay").inference_id);
    const keys = invokes(s.calls).map((c) => c.headers.get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it.each([
    ["inference_deployment_changed", "deployment_changed", "Run on the current deployment (new key)"],
    ["idempotency_conflict", "conflict", "Send as a new request (new key)"],
  ])("%s stops and offers only a deliberate new key", async (code, phase, action) => {
    const d = deploymentServer("production");
    const s = renderLive(`/projects/${PID}/workflows`, { ...d.table, [`POST ${P}/deployments/${LINEAGE}/invoke`]: fail(409, code) });
    const { user, panel } = await fill();
    await user.click(within(panel).getByRole("button", { name: /^Run on production/ }));
    await waitFor(() => expect(within(panel).getByTestId("send-status")).toHaveAttribute("data-phase", phase));
    expect(within(panel).getByRole("button", { name: action })).toBeInTheDocument();
    expect(invokes(s.calls)).toHaveLength(1);
  });
});

describe("authorization errors", () => {
  it("401: says the UI server has no valid key; nothing was read", async () => {
    renderLive(`/projects/${PID}/experiments`, {
      [`GET ${P}/workspace`]: fail(401, "unauthenticated", "Authentication required."),
      [`GET ${P}/experiments`]: fail(401, "unauthenticated", "Authentication required."),
    });
    expect(await screen.findByText(/sent no valid API key/, {}, T)).toBeInTheDocument();
    expect(await screen.findByTestId("workspace")).toHaveAttribute("data-error-code", "unauthenticated");
  });

  it("403: says the key lacks the permission and that nothing changed", async () => {
    const interrupted = jobWith((j) => (j.state = "INTERRUPTED"));
    renderLive(`/projects/${PID}/experiments/${JOB}`, {
      [`GET ${P}/experiments/${JOB}`]: ok(interrupted),
      [`POST ${P}/experiments/${JOB}/resume`]: fail(403, "insufficient_scope", "This key lacks the write scope."),
    });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Resume" }, T));
    expect(await screen.findByText(/lacks this permission\. Nothing was changed/)).toBeInTheDocument();
  });
});

describe("mock mode stays separate", () => {
  it("never renders a live screen or sends a request, and labels every run as simulated", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    vi.stubGlobal("fetch", fetch);
    setApi(createMockApi({ latencyMs: 0 }));
    render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/projects/p-support/experiments/e-triage-1"] })} />);
    expect(await screen.findByTestId("simulated-notice", {}, T)).toBeInTheDocument();
    expect(screen.queryByTestId("live-experiment")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspace")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
});
