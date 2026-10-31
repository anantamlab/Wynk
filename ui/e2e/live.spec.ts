import { type APIRequestContext, type Page, expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/*
  Live mode against a real product API (`python -m api.product`, started by playwright.config.ts on
  a fresh data directory). Nothing here is mocked: every number on screen came from the server.
*/
const API = "http://127.0.0.1:8790/api/v1";
// #32: direct API calls carry the dev workspace's key; the browser itself never holds it (the dev
// proxy adds it), which these flows also prove
const AUTH = { Authorization: `Bearer ${process.env.WYNK_E2E_API_KEY ?? `wynk_sk_0000000000000e2e_${"E".repeat(43)}`}` };

const CSV = `ticket_id,subject,body,customer_tier,queue
T-1,Charged twice,"My card shows two charges, both for March.",pro,billing
T-2,Cannot log in,SSO redirects back to the login page.,enterprise,technical
T-3,Update company name,We rebranded; please change the account name.,,account
T-4,Where is my order,Tracking has not moved for a week.,free,shipping
T-5,Refund request,"Downgraded mid-cycle, expected a prorated refund.",pro,billing
T-6,Webhook failures,Deliveries to our endpoint time out since Monday.,enterprise,technical
T-7,Add a teammate,How do I invite a colleague to our workspace?,free,account
T-8,Damaged parcel,The box arrived crushed and the device is cracked.,pro,shipping
`;
const sha256 = (s: string | Buffer) => createHash("sha256").update(s).digest("hex");
const unique = () => `${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;

/** Fails the test if the browser ever loads mock adapter code. */
function watchForMockCode(page: Page) {
  const loaded: string[] = [];
  page.on("request", (r) => {
    if (/\/src\/api\/mock\//.test(r.url())) loaded.push(r.url());
  });
  return loaded;
}

async function apiProject(request: APIRequestContext, name: string): Promise<string> {
  const res = await request.post(`${API}/projects`, { data: { name, description: "" }, headers: AUTH });
  expect(res.status()).toBe(201);
  return (await res.json()).project_id;
}

async function upload(page: Page, name: string, content: string | Buffer) {
  await page.getByTestId("dataset-file").setInputFiles({ name, mimeType: "application/octet-stream", buffer: Buffer.from(content) });
}

test("live: create project → upload CSV → inspect → map → register → split → reload → data persists", async ({ page, request }) => {
  const mockCode = watchForMockCode(page);
  const projectName = `E2E live ${unique()}`;
  const datasetId = `tickets-${unique()}`;

  await page.goto("/projects");
  await expect(page.getByTestId("data-source").first()).toContainText("Live API");

  // Project
  // a fresh backend has no projects, so the empty state offers the same button
  await page.getByRole("button", { name: "New project" }).first().click();
  await page.getByLabel("Name").fill(projectName);
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible();
  await expect(page.getByText("No datasets in this project")).toBeVisible();

  // Upload → Inspect: the server reads every row
  await page.getByRole("link", { name: "Add dataset" }).click();
  await upload(page, "tickets.csv", CSV);
  await expect(page).toHaveURL(/\/datasets\/new\?upload=u-[0-9a-f]+/);
  await expect(page.getByTestId("inspection-rows")).toHaveText("8");
  await expect(page.getByTestId("inspection-hash")).toHaveText(sha256(CSV));
  const tier = page.getByRole("table", { name: "Columns and roles" }).getByRole("row").filter({ hasText: "customer_tier" });
  await expect(tier).toContainText("yes"); // nullable: one row has no value
  await expect(page.getByRole("table", { name: "Preview rows" }).getByRole("row")).toHaveCount(9);

  // Map columns
  await expect(page.getByLabel("Role of queue")).toHaveValue("target");
  await expect(page.getByLabel("Role of ticket_id")).toHaveValue("id");
  await page.getByLabel("Role of customer_tier").selectOption("context");

  // Register
  await page.getByLabel("Dataset id").fill(datasetId);
  await page.getByRole("button", { name: "Register dataset" }).click();
  await expect(page).toHaveURL(new RegExp(`/datasets/${datasetId}\\?version=1$`));
  await expect(page.getByTestId("version-rows")).toHaveText("8");
  await expect(page.getByTestId("version-content-hash")).toHaveText(sha256(CSV));
  const identity = (await page.getByTestId("version-identity-hash").textContent())!;
  expect(identity).toMatch(/^[0-9a-f]{64}$/);
  await expect(page.getByText("No splits for this version yet.")).toBeVisible();

  // Create splits: 8 rows, 25% validation, 25% test -> 4 / 2 / 2 (server-computed)
  await page.getByLabel("Validation (%)").fill("25");
  await page.getByLabel("Test (%)").fill("25");
  await page.getByLabel("Seed").fill("7");
  await page.getByRole("button", { name: "Create splits" }).click();
  const row = page.getByTestId("splits-row");
  await expect(row).toHaveCount(1);
  const splitsHash = (await row.getAttribute("data-splits-hash"))!;
  expect(splitsHash).toMatch(/^[0-9a-f]{64}$/);
  await expect(row.getByTestId("size-optimization")).toHaveText("4");
  await expect(row.getByTestId("size-validation")).toHaveText("2");
  await expect(row.getByTestId("size-test")).toHaveText("2");

  // Reload: everything comes back from the backend
  await page.reload();
  await expect(page.getByTestId("version-identity-hash")).toHaveText(identity);
  await expect(page.getByTestId("splits-row")).toHaveAttribute("data-splits-hash", splitsHash);
  await expect(page.getByTestId("splits-row").getByTestId("size-optimization")).toHaveText("4");

  await page.goto("/projects");
  await page.getByRole("table", { name: "Projects" }).getByRole("link", { name: projectName }).click();
  await expect(page.getByTestId("dataset-row").filter({ hasText: datasetId })).toBeVisible();

  // and the backend itself holds it
  const stored = await (await request.get(`${API}/datasets/${datasetId}/versions/1/splits/${splitsHash}`, { headers: AUTH })).json();
  expect(stored.sizes).toEqual({ optimization: 4, test: 2, validation: 2 });
  expect(stored.splits.dataset_hash).toBe(identity);

  expect(mockCode).toEqual([]);
});

test("live: backend validation errors are shown with the server's reason and code", async ({ page, request }) => {
  const mockCode = watchForMockCode(page);
  const pid = await apiProject(request, `E2E errors ${unique()}`);
  await page.goto(`/projects/${pid}/datasets/new`);

  const expectError = async (code: string, title: string, reason: RegExp, timeout?: number) => {
    const alert = page.getByRole("alert").filter({ hasText: title });
    await expect(alert).toBeVisible({ timeout });
    await expect(alert).toContainText(reason);
    await expect(alert.getByTestId("error-code")).toHaveText(code);
  };

  await upload(page, "bad.csv", "ticket_id,subject\nT-1\n");
  await expectError("malformed_csv", "The CSV file is malformed", /line 2 has 1 fields, the header has 2/);

  await upload(page, "rows.parquet", "PAR1");
  await expectError("unsupported_format", "Unsupported file format", /format must be one of: csv, jsonl/);

  await upload(page, "big.csv", Buffer.alloc(1024 * 1024 + 1, "a"));
  // refused before the server reads the body; the error must still reach the browser (not a network error)
  await expectError("payload_too_large", "The file is too large", /larger than 1048576 bytes/, 20_000);

  // duplicate ids pass inspection, and registration with that id column is refused
  await upload(page, "dupes.csv", "ticket_id,subject,queue\nT-1,a,billing\nT-1,b,account\n");
  await expect(page.getByTestId("inspection-rows")).toHaveText("2");
  await expect(page.getByLabel("Role of ticket_id")).toHaveValue("id");
  await page.getByLabel("Dataset id").fill(`dupes-${unique()}`);
  await page.getByRole("button", { name: "Register dataset" }).click();
  await expectError("duplicate_row_id", "Row ids are not unique", /row id 'T-1' appears more than once/);

  // a dataset id owned by another project
  const other = await apiProject(request, `E2E other ${unique()}`);
  const taken = `taken-${unique()}`;
  const up = await request.post(`${API}/projects/${other}/uploads?format=csv&filename=t.csv`, {
    data: Buffer.from(CSV),
    headers: { "Content-Type": "application/octet-stream", ...AUTH },
  });
  const reg = await request.post(`${API}/uploads/${(await up.json()).upload_id}/register`, {
    data: { dataset_id: taken, name: "t", input_columns: ["subject"], target_columns: ["queue"], row_ids: "generated" },
    headers: AUTH,
  });
  expect(reg.status()).toBe(201);
  await page.getByLabel("Role of ticket_id").selectOption("ignore"); // generated ids: no duplicate problem
  await page.getByLabel("Dataset id").fill(taken);
  await page.getByRole("button", { name: "Register dataset" }).click();
  await expectError("dataset_conflict", "This dataset id is already taken", /belongs to another project/);

  expect(mockCode).toEqual([]);
});

/*
  The experiment → promotion → deployment → inference lifecycle, on the product API's real job
  workers and a local deterministic fake model (tests/e2e_product_server.py: `e2e-desk` answers
  order-desk prompts, `e2e-desk-slow` answers the same after 1.5 s). The dataset is the dogfood
  order desk (tests/fixtures/dogfood/order_desk.csv), registered as tests/dogfood_support.py does.
*/
const ORDER_DESK = readFileSync(resolve(process.cwd(), "..", "tests", "fixtures", "dogfood", "order_desk.csv"));
const INSTRUCTIONS = "Name the destination city of the order, using only the order record.";
const NEW_ORDER = {
  question: "Which city does order ORD-2001 ship to?",
  order_record: "Order ORD-2001 (1 x tarp) was packed at the Reno warehouse and ships to Lincoln by Ground on 2026-04-01.",
};

const api = async (request: APIRequestContext, method: "GET" | "POST", path: string, data?: unknown) => {
  const res = method === "GET" ? await request.get(`${API}/${path}`, { headers: AUTH }) : await request.post(`${API}/${path}`, { data, headers: AUTH });
  return { status: res.status(), body: await res.json() };
};

/** A project with the order desk registered (question → answer, order record as context) and split. */
async function orderDesk(request: APIRequestContext) {
  const pid = await apiProject(request, `E2E lifecycle ${unique()}`);
  const datasetId = `desk-${unique()}`;
  const up = await request.post(`${API}/projects/${pid}/uploads?format=csv&filename=order_desk.csv`, {
    data: ORDER_DESK,
    headers: { "Content-Type": "application/octet-stream", ...AUTH },
  });
  expect(up.status()).toBe(201);
  const reg = await api(request, "POST", `uploads/${(await up.json()).upload_id}/register`, {
    dataset_id: datasetId,
    name: "Order desk",
    input_columns: ["question"],
    context_columns: ["order_record"],
    target_columns: ["answer"],
    row_ids: "column",
    id_column: "ticket_id",
  });
  expect(reg.status).toBe(201);
  const splits = await api(request, "POST", `datasets/${datasetId}/versions/1/splits`, { seed: 11, validation_bps: 2000, test_bps: 2000 });
  expect(splits.status).toBe(201);
  return { pid, datasetId };
}

async function modelHash(request: APIRequestContext, name: string): Promise<string> {
  const { body } = await api(request, "GET", "models");
  const m = body.models.find((x: { name: string }) => x.name === name);
  expect(m, `model ${name} in the E2E registry`).toBeTruthy();
  return m.model_hash;
}

/** Fills the live configure screen; returns its submit button. */
async function configure(page: Page, request: APIRequestContext, pid: string, datasetId: string, model: string, taskId: string) {
  await page.goto(`/projects/${pid}/experiments/new?dataset=${datasetId}`);
  await page.getByLabel("Model", { exact: true }).selectOption(await modelHash(request, model));
  await page.getByLabel("Task id", { exact: true }).fill(taskId);
  await page.getByLabel("Instructions").fill(INSTRUCTIONS);
  const submit = page.getByRole("button", { name: "Create experiment" });
  await expect(submit).toBeEnabled();
  return submit;
}

/** Lets the server answer the first matching POST, then drops the answer before the browser sees it. */
async function dropFirstResponse(page: Page, pattern: string) {
  const keys: string[] = [];
  await page.route(pattern, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    keys.push(route.request().headers()["idempotency-key"] ?? "");
    if (keys.length === 1) {
      await route.fetch(); // the server receives and handles it…
      return route.abort("connectionreset"); // …and the browser never sees the answer
    }
    return route.fallback();
  });
  return keys;
}

test("live: experiment → results → promotion → publish → stage → production → inference, idempotent end to end", async ({ page, request }) => {
  test.setTimeout(240_000);
  const mockCode = watchForMockCode(page);
  const { pid, datasetId } = await orderDesk(request);
  const lineage = `${datasetId}.desk`;

  // Configure and create; the first response is lost, so the UI retries with the same key and the
  // server answers with the experiment it already created
  const createKeys = await dropFirstResponse(page, "**/api/v1/experiments");
  await (await configure(page, request, pid, datasetId, "e2e-desk", "desk")).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/experiments/j-[0-9a-f]+$`), { timeout: 20_000 });
  await expect(page.getByTestId("create-replayed")).toBeVisible();
  expect(createKeys).toHaveLength(2);
  expect(createKeys[0]).toMatch(/^exp/);
  expect(createKeys[1]).toBe(createKeys[0]);
  const jobId = page.url().split("/").pop()!;
  const jobs = (await api(request, "GET", "experiments")).body.jobs.filter((j: { dataset_id: string }) => j.dataset_id === datasetId);
  expect(jobs.map((j: { job_id: string }) => j.job_id)).toEqual([jobId]); // one intent, one experiment

  // Real progress until the workers finish
  const root = page.getByTestId("live-experiment");
  await expect(root).toHaveAttribute("data-state", "COMPLETED", { timeout: 150_000 });
  await expect(page.getByTestId("unit-row")).toHaveCount(3);
  const job = (await api(request, "GET", `experiments/${jobId}`)).body;
  const evaluations = job.units.reduce((n: number, u: { candidate_evaluations: number }) => n + u.candidate_evaluations, 0);
  await expect(page.getByTestId("progress-evaluations")).toContainText(String(evaluations));

  // Results: the artifact's numbers, verified by the server
  await page.getByRole("link", { name: "Results and provenance" }).click();
  const artifact = (await api(request, "GET", `experiments/${jobId}/artifact`)).body.artifact;
  const rows = page.getByTestId("result-row");
  await expect(rows).toHaveCount(artifact.runs.length);
  for (const run of artifact.runs) {
    const score = run.champion?.validation.score_mean;
    await expect(page.locator(`[data-testid="result-row"][data-strategy="${run.strategy}"]`).getByTestId("result-score")).toHaveText(
      score === null || score === undefined ? "—" : score.toFixed(3),
    );
  }
  await expect(page.getByTestId("verified")).toContainText("Verified by the server");
  await expect(page.getByTestId("test-runs")).toContainText(": 0");

  // Promotion opens the held-out split once and records the decision
  await page.getByRole("button", { name: /Promote on the held-out test split/ }).click();
  const promotion = page.getByTestId("promotion");
  await expect(promotion).toHaveAttribute("data-decision", "PROMOTED", { timeout: 60_000 });
  const promo = (await api(request, "GET", `experiments/${jobId}/promotion`)).body;
  await expect(page.getByTestId("heldout-score")).toHaveText(promo.record.heldout.challenger.score_mean.toFixed(3));

  // Workflows: publish; then someone else stages the version first. The server refuses the page's
  // now-outdated stage (it checks the version's state before the revision fence, so with one
  // version this is invalid_deployment_transition; stale_deployment needs a second version and is
  // covered against the recorded server response in src/pages/live/live.test.tsx)
  await page.getByRole("link", { name: "Publish and deploy in Workflows" }).click();
  const panel = page.locator(`[data-testid="lineage"][data-lineage="${lineage}"]`);
  await panel.getByRole("button", { name: "Publish champion" }).click();
  const version = panel.getByTestId("version-row");
  await expect(version).toHaveAttribute("data-state", "CREATED");
  const versionId = (await version.getAttribute("data-version-id"))!;
  await expect(panel).toHaveAttribute("data-revision", "0");
  expect((await api(request, "POST", `workflows/${versionId}/stage`, { expected_revision: 0 })).status).toBe(200); // behind the UI's back
  await panel.getByRole("button", { name: "Stage" }).click();
  await expect(panel.getByTestId("deployment-error").getByTestId("error-code")).toHaveText("invalid_deployment_transition");
  await expect(panel.getByTestId("deployment-error")).toContainText("only a CREATED version can be staged");
  // a promote made against the revision the page read before the reload is fenced
  const fenced = await api(request, "POST", `workflows/${versionId}/promote`, { expected_revision: 0 });
  expect([fenced.status, fenced.body.error.code]).toEqual([409, "stale_deployment"]);
  await expect(panel).toHaveAttribute("data-revision", "1");
  await expect(version).toHaveAttribute("data-state", "STAGING");
  await panel.getByRole("button", { name: "Promote to production" }).click();
  await expect(panel.getByTestId("production-version")).toHaveText(versionId);
  await expect(panel).toHaveAttribute("data-revision", "2");

  // Inference: the first answer is lost; the retry with the same key gets the original output back
  const invokeKeys = await dropFirstResponse(page, `**/api/v1/deployments/${lineage}/invoke`);
  const invoke = panel.getByTestId("invoke-panel");
  for (const [name, value] of Object.entries(NEW_ORDER)) await invoke.getByRole("textbox", { name: new RegExp(`^${name}`) }).fill(value);
  await invoke.getByRole("button", { name: /^Run on production/ }).click();
  await expect(invoke.getByTestId("inference-output")).toContainText('"answer": "Lincoln"', { timeout: 30_000 });
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "replayed");
  expect(invokeKeys).toHaveLength(2);
  expect(invokeKeys[1]).toBe(invokeKeys[0]);
  const inferenceId = (await invoke.getByTestId("inference").getAttribute("data-inference-id"))!;
  const stored = (await api(request, "GET", `inferences/${inferenceId}`)).body;
  expect(stored.status).toBe("SUCCEEDED");
  expect(stored.workflow_version).toBe(versionId);

  // Running it again is a new intent: a new key and a new invocation
  await invoke.getByRole("button", { name: /^Run on production/ }).click();
  await expect(invoke.getByTestId("inference")).not.toHaveAttribute("data-inference-id", inferenceId);
  await expect(invoke.getByTestId("inference-output")).toContainText("Lincoln");
  expect(invokeKeys).toHaveLength(3);
  expect(invokeKeys[2]).not.toBe(invokeKeys[0]);

  expect(mockCode).toEqual([]);
});

test("live: a running experiment shows real progress and can be cancelled", async ({ page, request }) => {
  test.setTimeout(120_000);
  const mockCode = watchForMockCode(page);
  const { pid, datasetId } = await orderDesk(request);
  await (await configure(page, request, pid, datasetId, "e2e-desk-slow", "slow")).click();
  const root = page.getByTestId("live-experiment");
  await expect(root).toHaveAttribute("data-state", /PENDING|RUNNING/, { timeout: 20_000 });
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(root).toHaveAttribute("data-state", "CANCELLED", { timeout: 60_000 });
  const jobId = (await root.getAttribute("data-job-id"))!;
  expect((await api(request, "GET", `experiments/${jobId}`)).body.state).toBe("CANCELLED");
  await expect(page.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Results and provenance" })).toHaveCount(0);

  // the experiment list shows the same server state
  await page.goto(`/projects/${pid}/experiments`);
  await expect(page.locator(`[data-testid="job-row"][data-job-id="${jobId}"]`).getByTestId("job-state")).toHaveAttribute("data-state", "CANCELLED");
  expect(mockCode).toEqual([]);
});

test("live: experiment screens use the product API, never a simulation, and name the workspace", async ({ page, request }) => {
  const mockCode = watchForMockCode(page);
  const apiCalls: string[] = [];
  const credentials: string[] = [];
  page.on("request", (r) => {
    const url = new URL(r.url());
    if (url.pathname.startsWith("/api/")) apiCalls.push(`${r.method()} ${url.pathname}`);
    if (r.headers()["authorization"]) credentials.push(url.pathname); // the browser never holds the key
  });
  const pid = await apiProject(request, `E2E experiments ${unique()}`);
  await page.goto(`/projects/${pid}/experiments`);
  await expect(page.getByText("No experiments in this project")).toBeVisible();
  await expect(page.getByTestId("workspace")).toContainText("Workspace");
  await page.goto(`/projects/${pid}/experiments/new`);
  await expect(page.getByText("Register a dataset first.")).toBeVisible();
  await page.goto(`/projects/${pid}/workflows`);
  await expect(page.getByText("No workflow lineages yet")).toBeVisible();
  await expect(page.getByTestId("simulated-notice")).toHaveCount(0);
  await expect(page.getByTestId("experiment-backend-required")).toHaveCount(0);
  expect(apiCalls).toContain("GET /api/v1/experiments");
  expect(apiCalls).toContain("GET /api/v1/models");
  expect(apiCalls).toContain("GET /api/v1/workspace");
  expect(credentials).toEqual([]);
  expect(mockCode).toEqual([]);
});
