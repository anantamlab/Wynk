import { type APIRequestContext, type Page, type Route, expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
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

async function apiProject(request: APIRequestContext, name: string, auth: Record<string, string> = AUTH): Promise<string> {
  const res = await request.post(`${API}/projects`, { data: { name, description: "" }, headers: auth });
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

const api = async (request: APIRequestContext, method: "GET" | "POST", path: string, data?: unknown, auth: Record<string, string> = AUTH) => {
  const res = method === "GET" ? await request.get(`${API}/${path}`, { headers: auth }) : await request.post(`${API}/${path}`, { data, headers: auth });
  return { status: res.status(), body: await res.json() };
};

/** A project with the order desk registered (question → answer, order record as context) and split. */
async function orderDesk(request: APIRequestContext, auth: Record<string, string> = AUTH, datasetId = `desk-${unique()}`) {
  const pid = await apiProject(request, `E2E lifecycle ${unique()}`, auth);
  const up = await request.post(`${API}/projects/${pid}/uploads?format=csv&filename=order_desk.csv`, {
    data: ORDER_DESK,
    headers: { "Content-Type": "application/octet-stream", ...auth },
  });
  expect(up.status()).toBe(201);
  const reg = await api(
    request,
    "POST",
    `uploads/${(await up.json()).upload_id}/register`,
    {
    dataset_id: datasetId,
    name: "Order desk",
    input_columns: ["question"],
    context_columns: ["order_record"],
    target_columns: ["answer"],
    row_ids: "column",
    id_column: "ticket_id",
    },
    auth,
  );
  expect(reg.status).toBe(201);
  const splits = await api(request, "POST", `datasets/${datasetId}/versions/1/splits`, { seed: 11, validation_bps: 2000, test_bps: 2000 }, auth);
  expect(splits.status).toBe(201);
  return { pid, datasetId };
}

async function modelHash(request: APIRequestContext, name: string): Promise<string> {
  const { body } = await api(request, "GET", "models");
  const m = body.models.find((x: { name: string }) => x.name === name);
  expect(m, `model ${name} in the E2E registry`).toBeTruthy();
  return m.model_hash;
}

/** Fills the configure screen the page shows now; returns its submit button. */
async function fillConfigure(page: Page, request: APIRequestContext, model: string, taskId: string, instructions = INSTRUCTIONS) {
  await page.getByLabel("Model", { exact: true }).selectOption(await modelHash(request, model));
  await page.getByLabel("Task id", { exact: true }).fill(taskId);
  await page.getByLabel("Instructions").fill(instructions);
  return page.getByRole("button", { name: "Create experiment" });
}

/** Opens and fills the live configure screen; returns its submit button. */
async function configure(page: Page, request: APIRequestContext, pid: string, datasetId: string, model: string, taskId: string) {
  await page.goto(`/projects/${pid}/experiments/new?dataset=${datasetId}`);
  const submit = await fillConfigure(page, request, model, taskId);
  await expect(submit).toBeEnabled();
  return submit;
}

/** The uncertain idempotent requests this tab keeps across a reload (ui/src/lib/pendingKeys.ts). */
const pendingRecords = (page: Page) =>
  page.evaluate(() => Object.entries(sessionStorage).filter(([name]) => name.startsWith("wynk.pending-request.v1:")).map(([, v]) => v));

/**
 * While `dropping`, the server receives and handles every matching POST, but the browser never sees
 * an answer (the connection resets). Records each request's Idempotency-Key, dropped or not.
 */
async function dropAnswers(page: Page, pattern: string) {
  const state = { keys: [] as string[], handled: 0, dropping: true };
  await page.route(pattern, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    state.keys.push(route.request().headers()["idempotency-key"] ?? "");
    if (!state.dropping) return route.fallback();
    try {
      await route.fetch();
      state.handled++;
      await route.abort("connectionreset");
    } catch {
      // the page reloaded before the answer: it is lost either way
    }
  });
  return state;
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

/** Every POST the browser sends to a matching path, whether a route answers it or the server does. */
function posts(page: Page, path: (pathname: string) => boolean) {
  const seen: { url: string; key: string }[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && path(new URL(r.url()).pathname)) seen.push({ url: r.url(), key: r.headers()["idempotency-key"] ?? "" });
  });
  return seen;
}

/**
 * Every request the fake model servers received whose body contains `marker`
 * (tests/e2e_product_server.py WYNK_E2E_PROVIDER_LOG): provider executions, counted at the source.
 */
function providerCalls(marker: string): number {
  const log = process.env.WYNK_E2E_PROVIDER_LOG;
  if (!log || !existsSync(log)) return 0;
  return readFileSync(log, "utf8")
    .split("\n")
    .filter((line) => line.includes(marker)).length;
}

/**
 * The server receives and runs the first matching POST and answers 200, but the browser gets a
 * body that does not match the contract (`contract_mismatch` in ui/src/api/live.ts). Records each
 * request's Idempotency-Key and the real answers.
 */
/** A new workspace with its own owner key (the bootstrap key holds `admin`), so a test owns its quota. */
async function freshWorkspace(request: APIRequestContext): Promise<Record<string, string>> {
  const created = await api(request, "POST", "workspaces", { name: `E2E workspace ${unique()}` });
  expect(created.status).toBe(201);
  return { Authorization: `Bearer ${created.body.api_key.secret}` };
}

/** Sends the browser's request to the real API with `auth`, as if the UI server's proxy held that key. */
function forward(route: Route, auth?: Record<string, string>) {
  if (!auth) return route.fetch();
  const url = new URL(route.request().url());
  const browser = Object.entries(route.request().headers()).filter(([name]) => name !== "host" && name !== "authorization");
  return route.fetch({ url: `http://127.0.0.1:8790${url.pathname}${url.search}`, headers: { ...Object.fromEntries(browser), authorization: auth.Authorization } });
}

/** Every API request of this page reaches the real API with `auth` (`only`: some paths). */
async function asWorkspace(page: Page, auth: Record<string, string>, only: (pathname: string) => boolean = (p) => p.startsWith("/api/v1/")) {
  const match = (url: URL) => only(url.pathname);
  await page.route(match, async (route) => route.fulfill({ response: await forward(route, auth) }));
  return () => page.unroute(match);
}

async function garbleFirstAnswer(page: Page, path: (pathname: string) => boolean, garbled: unknown, auth?: Record<string, string>) {
  const state = { keys: [] as string[], real: [] as unknown[], status: [] as number[] };
  await page.route(
    (url) => path(url.pathname),
    async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      state.keys.push(route.request().headers()["idempotency-key"] ?? "");
      if (state.keys.length > 1) return route.fallback();
      const response = await forward(route, auth); // the server runs it…
      state.status.push(response.status()); // the test asserts it: a refusal is never hidden
      state.real.push(await response.json());
      await route.fulfill({ status: 200, json: garbled }); // …and the browser cannot read the answer
    },
  );
  return state;
}

/**
 * The next POST to `path` reaches the server and gets its real answer, then the client fails
 * unexpectedly while it reads that answer (a TypeError from `Headers.get`, not a contract error).
 */
const clientFaultAfterNextPost = (page: Page, path: string) =>
  page.evaluate((p) => {
    const real = window.fetch.bind(window);
    let armed = true;
    window.fetch = async (input, init) => {
      const response = await real(input, init);
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
      if (!armed || init?.method !== "POST" || url.pathname !== p) return response;
      armed = false;
      return new Proxy(response, {
        get(target, name) {
          if (name === "headers")
            return {
              get() {
                throw new TypeError("injected client fault after the server answered (e2e)");
              },
            };
          const value = Reflect.get(target, name, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
    };
  }, path);

/** experiments over `datasetId` in the workspace that `auth` belongs to */
const jobsOf = async (request: APIRequestContext, datasetId: string, auth: Record<string, string> = AUTH) =>
  (await api(request, "GET", "experiments", undefined, auth)).body.jobs.filter((j: { dataset_id: string }) => j.dataset_id === datasetId) as { job_id: string }[];


/** `GET /api/v1/workspace` only (not `/workspace/keys`) */
const isWorkspace = (url: URL) => url.pathname === "/api/v1/workspace";
const refuseWorkspace = (status: number, code: string) => (page: Page) =>
  page.route(isWorkspace, (route) => route.fulfill({ status, json: { error: { code, message: `${code} (e2e)`, details: {} } } }));

/** ways the tab can lose its durable recovery record; each must send zero mutation requests */
const NO_RECOVERY: { name: string; reason: string; detail?: string; inject: (page: Page) => Promise<unknown> }[] = [
  {
    name: "sessionStorage access is denied",
    reason: "storage_unavailable",
    detail: "SecurityError",
    inject: (page) =>
      page.addInitScript(() =>
        Object.defineProperty(window, "sessionStorage", {
          configurable: true,
          get() {
            throw new DOMException("denied (e2e)", "SecurityError");
          },
        }),
      ),
  },
  ...["QuotaExceededError", "SecurityError"].map((name) => ({
    name: `setItem() throws ${name}`,
    reason: "storage_write_failed",
    detail: name,
    inject: (page: Page) =>
      page.addInitScript((n) => {
        Storage.prototype.setItem = function () {
          throw new DOMException("refused (e2e)", n);
        };
      }, name),
  })),
  {
    name: "a write that does not read back",
    reason: "storage_verify_failed",
    inject: (page) =>
      page.addInitScript(() => {
        Storage.prototype.setItem = function () {}; // accepted, never kept
      }),
  },
  { name: "GET /workspace answers 401", reason: "workspace_unavailable", detail: "unauthenticated", inject: refuseWorkspace(401, "unauthenticated") },
  { name: "GET /workspace answers 403", reason: "workspace_unavailable", detail: "forbidden", inject: refuseWorkspace(403, "forbidden") },
  { name: "GET /workspace answers 500", reason: "workspace_unavailable", detail: "internal_error", inject: refuseWorkspace(500, "internal_error") },
  { name: "GET /workspace fails on the network", reason: "workspace_unavailable", detail: "network_error", inject: (page) => page.route(isWorkspace, (route) => route.abort("connectionreset")) },
];

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

  // Inference: the server runs it, the answer is lost, and the page reloads before any retry
  // succeeds. The same values go out again with the SAME key, and the server answers with the
  // original output; nothing runs twice.
  const invoke = panel.getByTestId("invoke-panel");
  const fillOrder = async () => {
    for (const [name, value] of Object.entries(NEW_ORDER)) await invoke.getByRole("textbox", { name: new RegExp(`^${name}`) }).fill(value);
  };
  const runButton = invoke.getByRole("button", { name: /^Run on production/ });

  // No durable recovery record, no invocation: storage that refuses the record, then a workspace
  // that cannot be read. Zero POST /invoke leave the browser in both cases.
  const invokePosts = posts(page, (path) => path.endsWith(`/deployments/${lineage}/invoke`));
  await fillOrder();
  await page.evaluate(() => {
    const w = window as unknown as { realSetItem?: Storage["setItem"] };
    w.realSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function () {
      throw new DOMException("full (e2e)", "QuotaExceededError");
    };
  });
  await runButton.click();
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-reason", "storage_write_failed");
  await expect(invoke.getByTestId("send-status")).toContainText("nothing was sent");
  await page.evaluate(() => {
    Storage.prototype.setItem = (window as unknown as { realSetItem: Storage["setItem"] }).realSetItem;
  });
  await refuseWorkspace(500, "internal_error")(page);
  await runButton.click();
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-reason", "workspace_unavailable");
  await page.unroute(isWorkspace);
  await page.waitForTimeout(1_000); // longer than a first retry backoff: nothing goes out late
  expect(invokePosts).toEqual([]);
  expect(await pendingRecords(page)).toEqual([]);

  const invokeDrop = await dropAnswers(page, `**/api/v1/deployments/${lineage}/invoke`);
  await runButton.click();
  await expect.poll(() => invokeDrop.handled, { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
  const firstKey = invokeDrop.keys[0];
  const records = await pendingRecords(page);
  expect(records.map((r) => JSON.parse(r).key)).toEqual([firstKey]);
  for (const value of Object.values(NEW_ORDER)) expect(records[0]).not.toContain(value); // the key, never the inputs
  invokeDrop.dropping = false;
  await page.reload();
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "recovered");
  await expect(invoke.getByTestId("idempotency-key")).toHaveText(firstKey);
  await fillOrder();
  await invoke.getByRole("button", { name: /^Run on production/ }).click();
  await expect(invoke.getByTestId("inference-output")).toContainText('"answer": "Lincoln"', { timeout: 30_000 });
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "replayed");
  expect(new Set(invokeDrop.keys)).toEqual(new Set([firstKey])); // every attempt, before and after the reload
  expect(await pendingRecords(page)).toEqual([]);
  const inferenceId = (await invoke.getByTestId("inference").getAttribute("data-inference-id"))!;
  const stored = (await api(request, "GET", `inferences/${inferenceId}`)).body;
  expect(stored.status).toBe("SUCCEEDED");
  expect(stored.workflow_version).toBe(versionId);

  // Running it again is a new intent: a new key and a new invocation
  const sent = invokeDrop.keys.length;
  await invoke.getByRole("button", { name: /^Run on production/ }).click();
  await expect(invoke.getByTestId("inference")).not.toHaveAttribute("data-inference-id", inferenceId);
  await expect(invoke.getByTestId("inference-output")).toContainText("Lincoln");
  expect(invokeDrop.keys).toHaveLength(sent + 1);
  expect(invokeDrop.keys[sent]).not.toBe(firstKey);

  // The server runs the inference and answers 200, but the answer does not decode. The outcome
  // stays unresolved with its key (no automatic resend). After a reload the same values get the
  // ORIGINAL output back under that key, and the provider receives no second request.
  const marker = Object.values(NEW_ORDER)[0];
  const providerBefore = providerCalls(marker);
  const garbledInvoke = await garbleFirstAnswer(page, (p) => p === `/api/v1/deployments/${lineage}/invoke`, { inference_id: 42 });
  await runButton.click();
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "unresolved");
  await expect(invoke.getByTestId("send-status")).toContainText("contract_mismatch");
  await page.waitForTimeout(1_000); // longer than the first retry backoff: nothing resends on its own
  const lostKey = garbledInvoke.keys[0];
  expect(garbledInvoke.keys).toEqual([lostKey]);
  const ranOnce = providerCalls(marker) - providerBefore;
  expect(ranOnce).toBeGreaterThan(0); // the server did run it
  expect(garbledInvoke.status).toEqual([200]); // the server ran it and answered 200
  const original = garbledInvoke.real[0] as { inference_id: string; status: string };
  expect(original.status).toBe("SUCCEEDED");
  expect((await pendingRecords(page)).map((r) => JSON.parse(r))).toMatchObject([{ key: lostKey, outcome: "unresolved" }]);
  await page.reload();
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "recovered");
  await expect(invoke.getByTestId("idempotency-key")).toHaveText(lostKey);
  await fillOrder();
  await runButton.click();
  await expect(invoke.getByTestId("inference-output")).toContainText('"answer": "Lincoln"', { timeout: 30_000 });
  await expect(invoke.getByTestId("send-status")).toHaveAttribute("data-phase", "replayed");
  await expect(invoke.getByTestId("inference")).toHaveAttribute("data-inference-id", original.inference_id); // the original invocation
  expect(garbledInvoke.keys).toEqual([lostKey, lostKey]);
  expect(providerCalls(marker) - providerBefore).toBe(ranOnce); // no second provider execution
  expect(await pendingRecords(page)).toEqual([]);

  expect(mockCode).toEqual([]);
});

test("live: a lost create answer and a reload: the same values recover the same key, one experiment; other values are not sent", async ({ page, request }) => {
  test.setTimeout(120_000);
  const mockCode = watchForMockCode(page);
  const { pid, datasetId } = await orderDesk(request);
  const drop = await dropAnswers(page, "**/api/v1/experiments");
  await (await configure(page, request, pid, datasetId, "e2e-desk-slow", "reload")).click();
  await expect.poll(() => drop.handled, { timeout: 20_000 }).toBeGreaterThanOrEqual(1); // the server created it; the browser never heard
  const key = drop.keys[0];
  const records = await pendingRecords(page);
  expect(records.map((r) => JSON.parse(r).key)).toEqual([key]);
  expect(records[0]).not.toContain(INSTRUCTIONS); // the key and a digest, never the request

  drop.dropping = false; // the network is back
  await page.reload();
  await expect(page.getByTestId("send-status")).toHaveAttribute("data-phase", "recovered");
  await expect(page.getByTestId("idempotency-key")).toHaveText(key);

  // different values while the first creation is unconfirmed: nothing is sent behind the person's back
  const sent = drop.keys.length;
  const submit = await fillConfigure(page, request, "e2e-desk-slow", "reload", `${INSTRUCTIONS} Be brief.`);
  await expect(submit).toBeDisabled();
  await expect(page.getByTestId("send-blocked")).toContainText(key);

  // the same values again: the same key, and the server answers with the experiment it created
  await fillConfigure(page, request, "e2e-desk-slow", "reload");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/experiments/j-[0-9a-f]+$`), { timeout: 20_000 });
  await expect(page.getByTestId("create-replayed")).toBeVisible();
  expect(drop.keys.length).toBe(sent + 1);
  expect(new Set(drop.keys)).toEqual(new Set([key]));
  const jobId = page.url().split("/").pop()!;
  const jobs = (await api(request, "GET", "experiments")).body.jobs.filter((j: { dataset_id: string }) => j.dataset_id === datasetId);
  expect(jobs.map((j: { job_id: string }) => j.job_id)).toEqual([jobId]); // one intent, one experiment
  expect(await pendingRecords(page)).toEqual([]);
  expect((await api(request, "POST", `experiments/${jobId}/cancel`)).status).toBe(200); // the slow model need not finish
  expect(mockCode).toEqual([]);
});

for (const failure of NO_RECOVERY) {
  test(`live: ${failure.name}: zero experiments are created, and the person is told why`, async ({ page, request }) => {
    const { pid, datasetId } = await orderDesk(request);
    const creates = posts(page, (path) => path === "/api/v1/experiments");
    await failure.inject(page);
    await (await configure(page, request, pid, datasetId, "e2e-desk", "closed")).click();
    const status = page.getByTestId("send-status");
    await expect(status).toHaveAttribute("data-phase", "not_sent");
    await expect(status).toHaveAttribute("data-reason", failure.reason);
    if (failure.detail) await expect(status).toContainText(`${failure.reason} · ${failure.detail}`);
    await expect(page.getByRole("button", { name: "Create experiment" })).toBeEnabled(); // the person can fix it and submit again
    await page.waitForTimeout(1_000); // longer than a first retry backoff: nothing goes out late
    expect(creates).toEqual([]); // not one request left the browser…
    const jobs = (await api(request, "GET", "experiments")).body.jobs.filter((j: { dataset_id: string }) => j.dataset_id === datasetId);
    expect(jobs).toEqual([]); // …and the server has no experiment for it
  });
}

/*
  A POST the server ran, whose answer the browser could not use: the outcome is unresolved, the
  original key and record stay, and the same values (after a reload) recover the SAME experiment.
*/
const POST_DISPATCH_FAULTS: { name: string; code: string; inject: (page: Page, auth: Record<string, string>) => Promise<unknown> }[] = [
  {
    name: "a 200 whose body does not decode",
    code: "contract_mismatch",
    inject: (page, auth) => garbleFirstAnswer(page, (p) => p === "/api/v1/experiments", { job: { job_id: 7 } }, auth),
  },
  { name: "an unexpected client exception after the server answered", code: "internal_client_error", inject: (page) => clientFaultAfterNextPost(page, "/api/v1/experiments") },
];

for (const fault of POST_DISPATCH_FAULTS) {
  test(`live: ${fault.name}: the create stays unresolved with its key; a reload and the same values recover the one experiment`, async ({ page, request }) => {
    test.setTimeout(120_000);
    const C = await freshWorkspace(request); // its own active_jobs quota
    await asWorkspace(page, C);
    const { pid, datasetId } = await orderDesk(request, C);
    const creates = posts(page, (path) => path === "/api/v1/experiments");
    const keyOf = () => creates.map((c) => c.key);
    const submit = await configure(page, request, pid, datasetId, "e2e-desk-slow", "fault");
    await fault.inject(page, C);
    await submit.click();
    const status = page.getByTestId("send-status");
    await expect(status).toHaveAttribute("data-phase", "unresolved");
    await expect(status).toContainText(fault.code);
    await page.waitForTimeout(1_500); // longer than the first retry backoff: nothing resends on its own
    expect(creates).toHaveLength(1);
    const key = keyOf()[0];
    const [createdJob] = await jobsOf(request, datasetId, C);
    expect(createdJob, "the server created the experiment").toBeTruthy();
    expect(await jobsOf(request, datasetId, C)).toHaveLength(1);
    // the slow model need not finish: cancel it now; the replay still returns this job
    expect((await api(request, "POST", `experiments/${createdJob.job_id}/cancel`, undefined, C)).status).toBe(200);
    expect((await pendingRecords(page)).map((r) => JSON.parse(r))).toMatchObject([{ key, outcome: "unresolved" }]);

    await page.reload(); // the person reloads
    await expect(status).toHaveAttribute("data-phase", "recovered");
    await expect(page.getByTestId("idempotency-key")).toHaveText(key);
    await (await fillConfigure(page, request, "e2e-desk-slow", "fault")).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/experiments/j-[0-9a-f]+$`), { timeout: 20_000 });
    await expect(page.getByTestId("create-replayed")).toBeVisible();
    expect(keyOf()).toEqual([key, key]); // the same key, never a new one
    const jobs = await jobsOf(request, datasetId, C);
    expect(jobs.map((j) => j.job_id)).toEqual([page.url().split("/").pop()]); // one intent, one experiment
    expect(await pendingRecords(page)).toEqual([]);
    expect(jobs[0].job_id).toBe(createdJob.job_id); // the original experiment
  });
}

/*
  The UI server's key moves from workspace A to workspace B (a real second workspace with its own
  owner key). The page's workspace lookup and its experiment POSTs then reach the real API with
  B's key, exactly as if the proxy held it.
*/
test("live: workspace A unresolved → B: ordinary submit sends nothing; only the explicit new request runs in B; back in A the key recovers A's experiment", async ({ page, request }) => {
  test.setTimeout(180_000);
  const datasetId = `desk-${unique()}`;
  const A = await freshWorkspace(request);
  const B = await freshWorkspace(request);
  await asWorkspace(page, A); // the UI server's key belongs to A
  const a = await orderDesk(request, A, datasetId);
  const b = await orderDesk(request, B, datasetId); // the same dataset id and split in B
  const splitsHash = async (auth: Record<string, string>) => (await api(request, "GET", `datasets/${datasetId}/versions/1/splits`, undefined, auth)).body.splits[0].splits_hash;
  expect(await splitsHash(B)).toBe(await splitsHash(A)); // so the same request body is valid in B
  expect(b.pid).not.toBe(a.pid);

  const creates = posts(page, (path) => path === "/api/v1/experiments");
  const keys = () => creates.map((c) => c.key);
  // in A: the server creates it, the answer does not decode → unresolved, key kA
  const garbled = await garbleFirstAnswer(page, (p) => p === "/api/v1/experiments", { job: { job_id: 7 } }, A);
  await (await configure(page, request, a.pid, datasetId, "e2e-desk-slow", "switch")).click();
  const status = page.getByTestId("send-status");
  await expect(status).toHaveAttribute("data-phase", "unresolved");
  const kA = keys()[0];
  expect(garbled.keys).toEqual([kA]);
  expect(garbled.status).toEqual([201]); // the server created it
  const [jobA] = await jobsOf(request, datasetId, A);
  expect(jobA, "the experiment in A").toBeTruthy();
  expect((await api(request, "POST", `experiments/${jobA.job_id}/cancel`, undefined, A)).status).toBe(200); // the slow model need not finish
  // the garble route answers only the first POST; later ones fall back to the workspace routes

  // the key now belongs to B (its workspace lookup and experiment POSTs)
  const backToA = await asWorkspace(page, B, (p) => p === "/api/v1/workspace" || p === "/api/v1/experiments");
  const sentBefore = creates.length;
  await page.getByRole("button", { name: "Create experiment" }).click(); // the ordinary submit, same values
  await expect(status).toHaveAttribute("data-phase", "not_sent");
  await expect(status).toHaveAttribute("data-reason", "workspace_changed");
  await page.waitForTimeout(1_500);
  expect(creates).toHaveLength(sentBefore); // zero POSTs…
  expect(await jobsOf(request, datasetId, B)).toEqual([]); // …nothing in B…
  expect(await jobsOf(request, datasetId, A)).toHaveLength(1); // …and nothing more in A
  expect((await pendingRecords(page)).map((r) => JSON.parse(r))).toMatchObject([{ key: kA, outcome: "unresolved" }]);

  // only the explicit new request runs in B, with B's own key
  await status.getByRole("button", { name: /^Send as a new request/ }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${a.pid}/experiments/j-[0-9a-f]+$`), { timeout: 20_000 });
  const kB = keys()[sentBefore];
  expect(creates).toHaveLength(sentBefore + 1);
  expect(kB).not.toBe(kA);
  const inB = await jobsOf(request, datasetId, B);
  expect(inB).toHaveLength(1);
  expect((await api(request, "POST", `experiments/${inB[0].job_id}/cancel`, undefined, B)).status).toBe(200);
  expect(await jobsOf(request, datasetId, A)).toHaveLength(1);
  expect((await pendingRecords(page)).map((r) => JSON.parse(r))).toMatchObject([{ key: kA, outcome: "unresolved" }]); // A's record kept

  // back in A: the same values recover A's experiment with kA
  await backToA();
  await page.goto(`/projects/${a.pid}/experiments/new?dataset=${datasetId}`);
  await expect(status).toHaveAttribute("data-phase", "recovered");
  await expect(page.getByTestId("idempotency-key")).toHaveText(kA);
  await (await fillConfigure(page, request, "e2e-desk-slow", "switch")).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${a.pid}/experiments/j-[0-9a-f]+$`), { timeout: 20_000 });
  await expect(page.getByTestId("create-replayed")).toBeVisible();
  expect(keys().slice(sentBefore + 1)).toEqual([kA]);
  const inA = await jobsOf(request, datasetId, A);
  expect(inA.map((j) => j.job_id)).toEqual([page.url().split("/").pop()]); // still one experiment in A
  expect(await jobsOf(request, datasetId, B)).toHaveLength(1); // and one in B
  expect(await pendingRecords(page)).toEqual([]);
  expect(inA[0].job_id).toBe(jobA.job_id); // the original experiment
});

test("live: a revoked key ends the live view: the cached experiment is removed and polling stops", async ({ page, request }) => {
  test.setTimeout(120_000);
  const { pid, datasetId } = await orderDesk(request);
  const created = await api(request, "POST", "workspace/keys", { name: `e2e-revoked-${unique()}`, scopes: ["read"] });
  expect(created.status).toBe(201);
  const secret: string = created.body.secret;
  // this page's experiment polls reach the real API with that read-only key, as if the UI server held it
  const polls: number[] = [];
  await page.route(
    (url) => /^\/api\/v1\/experiments\/j-[0-9a-f]+$/.test(url.pathname),
    async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const browser = Object.entries(route.request().headers()).filter(([name]) => name !== "host" && name !== "authorization");
      const headers = { ...Object.fromEntries(browser), authorization: `Bearer ${secret}` };
      const response = await route.fetch({ url: `http://127.0.0.1:8790${new URL(route.request().url()).pathname}`, headers });
      polls.push(response.status());
      await route.fulfill({ response });
    },
  );
  await (await configure(page, request, pid, datasetId, "e2e-desk-slow", "revoke")).click();
  const root = page.getByTestId("live-experiment");
  await expect(root).toHaveAttribute("data-state", /PENDING|RUNNING/, { timeout: 20_000 });
  const jobId = (await root.getAttribute("data-job-id"))!;
  await expect.poll(() => polls.filter((s) => s === 200).length, { timeout: 20_000 }).toBeGreaterThanOrEqual(2);

  expect((await api(request, "POST", `workspace/keys/${created.body.key.key_id}/revoke`)).status).toBe(200);
  await expect(page.locator('[data-error-code="unauthenticated"]')).toBeVisible({ timeout: 15_000 });
  await expect(root).toHaveCount(0); // the experiment this key may no longer read is gone from the screen
  expect(polls.filter((s) => s === 401)).toHaveLength(1);
  const after = polls.length;
  await page.waitForTimeout(4_000); // four healthy poll intervals (1 s)
  expect(polls).toHaveLength(after); // polling stopped
  expect((await api(request, "POST", `experiments/${jobId}/cancel`)).status).toBe(200);
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
