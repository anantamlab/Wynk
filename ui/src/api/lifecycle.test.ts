import { describe, expect, it } from "vitest";
import { fail, lc, ok, recorded, server } from "@/test/lifecycleApi";
import * as decode from "./contract/lifecycle";
import { createLiveApi } from "./live";

/*
  The lifecycle half of the live adapter (experiments, promotion, deployment, inference) against
  responses the Python product API really sent (product-api.lifecycle.v1.json).
*/
const JOB = lc("get_experiment").job_id as string;
const LINEAGE = lc("promote_experiment").lineage_id as string;
const VERSION = lc("publish_workflow").version_id as string;
const P = "/api/v1";

const live = (routes: Parameters<typeof server>[0]) => {
  const s = server(routes);
  return { l: createLiveApi({ fetch: s.fetch }).lifecycle!, ...s };
};

describe("lifecycle decoders accept every recorded server response", () => {
  it.each([
    ["list_models", decode.modelList],
    ["get_experiment", decode.job],
    ["create_experiment", decode.job],
    ["list_experiments", decode.jobList],
    ["get_artifact", decode.artifact],
    ["get_provenance", decode.provenance],
    ["verify_artifact", decode.verify],
    ["promote_experiment", decode.promotion],
    ["get_champion", decode.champion],
    ["publish_workflow", decode.workflowVersionOne],
    ["list_workflows", decode.workflowVersionList],
    ["get_deployment", decode.deployment],
    ["stage_workflow", decode.deployment],
    ["promote_workflow", decode.deployment],
    ["get_deployment_history", decode.deploymentHistory],
    ["invoke_production", decode.inference],
    ["invoke_production_replay", decode.inference],
    ["current_workspace", decode.currentWorkspace],
  ] as const)("%s", (name, read) => {
    expect(() => (read as (v: unknown) => unknown)(lc(name))).not.toThrow();
  });

  it("reads the facts the screens show", () => {
    const j = decode.job(lc("get_experiment"));
    expect(j.state).toBe("COMPLETED");
    expect(j.units.map((u) => u.strategy).sort()).toEqual(["aco", "fixed", "random"]);
    const inv = decode.inference(lc("invoke_production"));
    expect(inv.output).toEqual({ answer: "Lincoln" });
    expect(decode.inference(lc("invoke_production_replay")).inference_id).toBe(inv.inference_id);
    const ws = decode.currentWorkspace(lc("current_workspace"));
    expect(ws.role).toBe("OWNER");
    expect(decode.modelList(lc("list_models"))[0]).not.toHaveProperty("endpoint");
  });

  it("refuses a drifted compact view instead of guessing", () => {
    expect(() => decode.deployment({ ...lc("get_deployment"), owner: "x" })).toThrow(/unexpected owner/);
    const noState = lc("get_experiment");
    delete noState.state;
    expect(() => decode.job(noState)).toThrow(/state/);
    expect(() => decode.job({ ...lc("get_experiment"), state: "DONE" })).toThrow(/state/);
  });
});

describe("live adapter: lifecycle requests", () => {
  it("creates an experiment with the Idempotency-Key and reads Idempotent-Replayed; never sends credentials", async () => {
    const { l, calls } = live({
      [`POST ${P}/experiments`]: [recorded("create_experiment"), recorded("get_experiment", { "Idempotent-Replayed": "true" })],
    });
    const body = { dataset_id: "d" } as unknown as decode.CreateExperimentWire;
    const first = await l.createJob(body, "exp-key-1");
    const again = await l.createJob(body, "exp-key-1");
    expect(first.replayed).toBe(false);
    expect(again.replayed).toBe(true);
    expect(again.value.job_id).toBe(first.value.job_id);
    for (const c of calls) {
      expect(c.headers.get("Idempotency-Key")).toBe("exp-key-1");
      expect(c.headers.get("Authorization")).toBeNull();
      expect(c.headers.get("X-Wynk-Workspace")).toBeNull();
      expect(c.body).toEqual({ dataset_id: "d" });
    }
  });

  it("invokes production by lineage and a version by id, with the key and an {inputs} body", async () => {
    const inputs = { question: "q", order_record: "r" };
    const { l, calls } = live({
      [`POST ${P}/deployments/${LINEAGE}/invoke`]: recorded("invoke_production"),
      [`POST ${P}/workflows/${VERSION}/invoke`]: recorded("invoke_production_replay", { "Idempotent-Replayed": "true" }),
    });
    const prod = await l.invoke({ lineage: LINEAGE }, inputs, "inf-a");
    const ver = await l.invoke({ versionId: VERSION }, inputs, "inf-b");
    expect(prod).toMatchObject({ replayed: false, value: { output: { answer: "Lincoln" } } });
    expect(ver.replayed).toBe(true);
    expect(calls.map((c) => [c.headers.get("Idempotency-Key"), c.body])).toEqual([
      ["inf-a", { inputs }],
      ["inf-b", { inputs }],
    ]);
  });

  it("fences every deployment move with the revision it was given", async () => {
    const { l, calls } = live({
      [`POST ${P}/workflows/${VERSION}/stage`]: recorded("stage_workflow"),
      [`POST ${P}/workflows/${VERSION}/promote`]: recorded("promote_workflow"),
      [`POST ${P}/deployments/${LINEAGE}/rollback`]: recorded("error_stale_deployment"),
    });
    expect((await l.stage(VERSION, 0)).revision).toBe(1);
    expect((await l.promoteVersion(VERSION, 1)).production_version_id).toBe(VERSION);
    await expect(l.rollback(LINEAGE, 1)).rejects.toMatchObject({ code: "stale_deployment", status: 409 });
    expect(calls.map((c) => c.body)).toEqual([{ expected_revision: 0 }, { expected_revision: 1 }, { expected_revision: 1 }]);
    expect(calls.every((c) => c.headers.get("Idempotency-Key") === null)).toBe(true);
  });

  it("maps the server's lifecycle errors to their codes and keeps their details", async () => {
    const { l } = live({
      [`POST ${P}/experiments/${JOB}/cancel`]: recorded("error_job_not_cancellable"),
      [`GET ${P}/champions/${LINEAGE}`]: recorded("error_champion_not_found"),
      [`GET ${P}/deployments/${LINEAGE}`]: recorded("error_deployment_not_found"),
      [`POST ${P}/deployments/${LINEAGE}/invoke`]: fail(409, "inference_in_progress", "still running", { state: "RUNNING", inference_id: "inf-1" }),
      [`GET ${P}/models`]: fail(503, "experiment_backend_unavailable"),
    });
    await expect(l.cancelJob(JOB)).rejects.toMatchObject({ code: "job_not_cancellable", status: 409 });
    await expect(l.getChampion(LINEAGE)).rejects.toMatchObject({ code: "champion_not_found", status: 404 });
    await expect(l.getDeployment(LINEAGE)).rejects.toMatchObject({ code: "deployment_not_found" });
    await expect(l.invoke({ lineage: LINEAGE }, {}, "k")).rejects.toMatchObject({
      code: "inference_in_progress",
      details: { state: "RUNNING", inference_id: "inf-1" },
    });
    await expect(l.listModels()).rejects.toMatchObject({ code: "experiment_backend_unavailable", kind: "unavailable" });
  });

  it("reports a contract mismatch on a drifted lifecycle response", async () => {
    const { l } = live({ [`GET ${P}/deployments/${LINEAGE}`]: ok({ ...lc("get_deployment"), extra: 1 }) });
    await expect(l.getDeployment(LINEAGE)).rejects.toMatchObject({ code: "contract_mismatch" });
  });
});
