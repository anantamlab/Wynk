import { ApiError, type LifecycleApi, type WynkApi } from "./client";
import * as decode from "./contract/decode";
import * as lc from "./contract/lifecycle";
import { WireShapeError } from "./contract/decode";
import {
  ContractMappingError,
  fromDatasetVersion,
  fromDatasetView,
  fromProject,
  fromSplitsRecord,
  fromUpload,
  toRegisterDataset,
  splitPlanWire,
} from "./contract/mapping";
import type { NewProjectWire } from "./contract/wire";

/*
  The product API v1 adapter (api/product.py). Every method is one documented route:

    listProjects        GET  /projects                         createProject  POST /projects
    getProject          GET  /projects/{id}
    uploadDataset       POST /projects/{id}/uploads?format=&filename=   (raw file bytes)
    getUpload           GET  /uploads/{id}                     registerDataset POST /uploads/{id}/register
    listDatasets        GET  /projects/{id}/datasets           getDataset     GET  /datasets/{id}
    getDatasetVersion   GET  /datasets/{id}/versions/{n}
    createSplits        POST /datasets/{id}/versions/{n}/splits
    listSplits          GET  /datasets/{id}/versions/{n}/splits
    getSplits           GET  /datasets/{id}/versions/{n}/splits/{hash}

  `lifecycle` holds the experiment → promotion → deployment → inference routes (see `LifecycleApi`
  and the route list in api/product.py), checked by `contract/lifecycle.ts`.

  Responses are checked against the wire shapes (`contract/decode.ts`) and mapped to view models
  (`contract/mapping.ts`). Errors keep the server's stable code and message. Nothing here falls
  back to mock data. The simulated-experiment methods (the mock's own view of a run) refuse
  without a request: live screens use `lifecycle`.

  Authentication: the browser never holds an API key. The dev / E2E proxy adds the server-side
  `Authorization` header (vite.config.ts), so this adapter never sends one, and the workspace is
  whatever that key belongs to; no workspace id is ever sent.
*/

export const DEFAULT_BASE_URL = "/api/v1";

export const SIMULATED_ONLY =
  "This view of a run is simulated by the mock demo only. With the live API, experiments come from the product API's experiment routes.";

export interface LiveOptions {
  /** where the product API is mounted; default `/api/v1` (the dev server proxies `/api`) */
  baseUrl?: string;
  /** injectable for tests */
  fetch?: typeof fetch;
}

type Body = { json: unknown } | { bytes: Blob } | undefined;
const REPLAYED = "Idempotent-Replayed";
/** api/tenancy.py: the workspace precondition (never authorization: the key decides) */
export const EXPECTED_WORKSPACE = "Wynk-Expected-Workspace";

export function createLiveApi(options: LiveOptions = {}): WynkApi {
  const base = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  const doFetch = options.fetch ?? ((input: RequestInfo | URL, init?: RequestInit) => globalThis.fetch(input, init));

  async function call<T>(
    method: "GET" | "POST",
    path: string,
    body: Body,
    read: (raw: unknown) => T,
    extra: Record<string, string> = {},
  ): Promise<{ value: T; status: number; replayed: boolean }> {
    const init: RequestInit = { method, headers: { Accept: "application/json", ...extra } };
    if (body && "json" in body) {
      init.headers = { ...init.headers, "Content-Type": "application/json" };
      try {
        init.body = JSON.stringify(body.json);
      } catch (err) {
        // proven before dispatch: nothing left the browser
        throw new ApiError("request_not_sent", `${method} ${path}: the request body could not be encoded (${err instanceof Error ? err.message : String(err)}).`);
      }
    } else if (body) {
      // raw bytes, never a form: the server refuses multipart bodies
      init.headers = { ...init.headers, "Content-Type": "application/octet-stream" };
      init.body = body.bytes;
    }
    const url = `${base}${path}`;
    let res: Response;
    try {
      res = await doFetch(url, init);
    } catch (err) {
      throw new ApiError("network_error", `${method} ${url} failed: ${err instanceof Error ? err.message : String(err)}. Is the Wynk API running?`);
    }
    let raw: unknown = undefined;
    const text = await res.text().catch(() => "");
    try {
      raw = text ? JSON.parse(text) : undefined;
    } catch {
      raw = undefined;
    }
    if (!res.ok) {
      const envelope = decode.errorResponse(raw);
      if (envelope)
        throw new ApiError(envelope.error.code, envelope.error.message, { status: res.status, details: envelope.error.details, fromServer: true });
      throw new ApiError("bad_response", `${method} ${url} answered HTTP ${res.status} without a Wynk API error body. Is the Wynk API running behind this address?`, {
        status: res.status,
      });
    }
    if (raw === undefined)
      throw new ApiError("bad_response", `${method} ${url} answered HTTP ${res.status} without a JSON body.`, { status: res.status });
    try {
      return { value: read(raw), status: res.status, replayed: res.headers?.get?.(REPLAYED) === "true" };
    } catch (err) {
      // the server answered 2xx: for a POST, the request may have run even though its answer is unreadable
      if (err instanceof WireShapeError || err instanceof ContractMappingError)
        throw new ApiError("contract_mismatch", `${method} ${url}: ${err.message}`, { status: res.status });
      throw err;
    }
  }

  const get = async <T>(path: string, read: (raw: unknown) => T) => (await call("GET", path, undefined, read)).value;
  /** 201: created now; 200: the identical resource already existed */
  const create = async <T>(path: string, body: Body, read: (raw: unknown) => T) => {
    const { value, status } = await call("POST", path, body, read);
    return { value, created: status === 201 };
  };
  const seg = encodeURIComponent;
  const versionPath = (datasetId: string, version: number) => `/datasets/${seg(datasetId)}/versions/${version}`;
  const simulatedOnly = (): Promise<never> => Promise.reject(new ApiError("simulated_only", SIMULATED_ONLY));
  const post = async <T>(path: string, json: unknown, read: (raw: unknown) => T) =>
    (await call("POST", path, json === undefined ? undefined : { json }, read)).value;
  const sent = async <T>(path: string, json: unknown, read: (raw: unknown) => T, key: string, workspace?: string) => {
    // the workspace the caller just read is a precondition the server enforces (api/tenancy.py)
    const headers: Record<string, string> = { "Idempotency-Key": key, ...(workspace ? { [EXPECTED_WORKSPACE]: workspace } : {}) };
    const { value, replayed } = await call("POST", path, { json }, read, headers);
    return { value, replayed };
  };

  const lifecycle: LifecycleApi = {
    workspace: () => get("/workspace", lc.currentWorkspace),
    listModels: () => get("/models", lc.modelList),
    datasetSpec: (datasetId, version) => get(versionPath(datasetId, version), (r) => decode.datasetVersion(r).spec),
    listJobs: () => get("/experiments", lc.jobList),
    getJob: (id) => get(`/experiments/${seg(id)}`, (r) => lc.job(r)),
    createJob: (body, key, workspace) => sent("/experiments", body, (r) => lc.job(r), key, workspace),
    cancelJob: (id) => post(`/experiments/${seg(id)}/cancel`, undefined, (r) => lc.job(r)),
    resumeJob: (id) => post(`/experiments/${seg(id)}/resume`, undefined, (r) => lc.job(r)),
    getArtifact: (id) => get(`/experiments/${seg(id)}/artifact`, lc.artifact),
    getProvenance: (id) => get(`/experiments/${seg(id)}/provenance`, lc.provenance),
    verifyArtifact: (id) => get(`/experiments/${seg(id)}/verify`, lc.verify),
    promote: (id) => post(`/experiments/${seg(id)}/promote`, undefined, lc.promotion),
    getPromotion: (id) => get(`/experiments/${seg(id)}/promotion`, lc.promotion),
    getChampion: (l) => get(`/champions/${seg(l)}`, lc.champion),
    publish: (championId) => create("/workflows", { json: { champion_id: championId } }, lc.workflowVersionOne),
    listWorkflowVersions: (l) => get(`/workflows?${new URLSearchParams({ lineage: l })}`, lc.workflowVersionList),
    getDeployment: (l) => get(`/deployments/${seg(l)}`, lc.deployment),
    getDeploymentHistory: (l) => get(`/deployments/${seg(l)}/history`, lc.deploymentHistory),
    stage: (versionId, rev) => post(`/workflows/${seg(versionId)}/stage`, { expected_revision: rev }, lc.deployment),
    promoteVersion: (versionId, rev) => post(`/workflows/${seg(versionId)}/promote`, { expected_revision: rev }, lc.deployment),
    rollback: (l, rev) => post(`/deployments/${seg(l)}/rollback`, { expected_revision: rev }, lc.deployment),
    invoke: (target, inputs, key, workspace) =>
      sent(
        "lineage" in target ? `/deployments/${seg(target.lineage)}/invoke` : `/workflows/${seg(target.versionId)}/invoke`,
        { inputs },
        lc.inference,
        key,
        workspace,
      ),
  };

  return {
    mode: "live",
    experiments: "live",
    lifecycle,

    listProjects: () => get("/projects", (r) => decode.projectList(r).projects.map(fromProject)),
    getProject: (id) => get(`/projects/${seg(id)}`, (r) => fromProject(decode.project(r))),
    createProject: async (input) => {
      const body: NewProjectWire = { name: input.name.trim(), description: input.description.trim() };
      return (await create("/projects", { json: body }, (r) => fromProject(decode.project(r)))).value;
    },

    uploadDataset: (projectId, file) => {
      const query = new URLSearchParams({ format: file.format, filename: file.name });
      return create(`/projects/${seg(projectId)}/uploads?${query}`, { bytes: file.bytes }, (r) => fromUpload(decode.upload(r)));
    },
    getUpload: (uploadId) => get(`/uploads/${seg(uploadId)}`, (r) => fromUpload(decode.upload(r))),
    registerDataset: (uploadId, input) =>
      create(`/uploads/${seg(uploadId)}/register`, { json: toRegisterDataset(input) }, (r) => fromDatasetVersion(decode.datasetVersion(r))),
    listDatasets: (projectId) => get(`/projects/${seg(projectId)}/datasets`, (r) => decode.datasetList(r).datasets.map(fromDatasetView)),
    getDataset: (datasetId) => get(`/datasets/${seg(datasetId)}`, (r) => fromDatasetView(decode.datasetView(r))),
    getDatasetVersion: (datasetId, version) => get(versionPath(datasetId, version), (r) => fromDatasetVersion(decode.datasetVersion(r))),
    createSplits: (datasetId, version, plan) =>
      create(`${versionPath(datasetId, version)}/splits`, { json: splitPlanWire(plan) }, (r) => fromSplitsRecord(decode.splitsRecord(r))),
    listSplits: (datasetId, version) => get(`${versionPath(datasetId, version)}/splits`, (r) => decode.splitsList(r).splits.map(fromSplitsRecord)),
    getSplits: (datasetId, version, splitsHash) =>
      get(`${versionPath(datasetId, version)}/splits/${seg(splitsHash)}`, (r) => fromSplitsRecord(decode.splitsRecord(r))),

    listModels: simulatedOnly,
    listExperiments: simulatedOnly,
    getExperiment: simulatedOnly,
    createExperiment: simulatedOnly,
    cancelExperiment: simulatedOnly,
    getResults: simulatedOnly,
    listWorkflows: simulatedOnly,
  };
}
