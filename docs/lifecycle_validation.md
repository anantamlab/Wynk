# Product lifecycle validation harness

`tests/lifecycle_harness.py` runs the whole Wynk product lifecycle through the real product
API and checks every stage with explicit assertions on HTTP responses:

Upload → Register → Split → Configure → Optimize → Evaluate → Promote → Deploy → Invoke →
Monitor → Rollback, plus Recovery, Provenance and Tenancy/auth checks.

**Provider mode: fake.** Every run is a simulated-provider run. The far end of the model
connection is `tests.dogfood_support.FakeProvider`, a local OpenAI-compatible server with fixed
answers and fixed token usage. No hosted model or search provider is called. "Deploy" means the
product API's deployment step in a temporary local store. Nothing is deployed anywhere else.

## Run it

```bash
python -m tests.lifecycle_harness                    # 2 runs, reports in .wynk-validation/lifecycle/
python -m tests.lifecycle_harness --runs 1 --out DIR # 1 run, reports in DIR
python -m pytest tests/test_lifecycle_harness.py     # the CI entry point (2 runs, tmp dir)
```

Two `cannot bind job ... is not pinned by any registry entry` warnings on stderr are expected: the
recovery stage re-pins the model on purpose.

Requirements: the project installed with the workflow runtime (`pip install -e ".[dev,maf]"`),
run from the repository root. One run takes about 15-20 s. The exit code is 0 only when every
stage passes in every run and the runs agree.

The pytest entry point runs in the normal `python -m pytest` job of CI
(`.github/workflows/ci.yml`, Tests on Python 3.11 and 3.13).

## What is real and what is fake

| Part | Real or fake |
| --- | --- |
| HTTP server | Real: `api.product.Server` and `make_handler` on `127.0.0.1`, random port |
| Product API | Real: `api.tenancy.TenantRouter` and `api.product.ProductAPI`, wired like `python -m api.product --model-registry ...` (`tests.dogfood_support.product`) |
| Job worker | Real: `TenantRouter.start_workers` (what `api.product.start_worker` calls) |
| Model client | Real: `OpenAICompatibleClient`, real HTTP |
| Model provider | **Fake**: `FakeProvider` on `127.0.0.1` |
| Data store | Real stores in a new temporary directory, encrypted at rest with a fixed, public, throwaway data key |
| First workspace | Created in-process with `TenantRouter.create_workspace` (the operator path; there is no HTTP route for the first workspace). Every other workspace and key is created over HTTP. |

Safety measures for each run:

- The harness removes `WYNK_MODEL_REGISTRY`, `WYNK_MODEL_API_KEY`, `WYNK_DATA_KEYS_FILE` and
  every `WYNK_WEBSEARCH_*` variable, and restores them at the end.
- A socket guard refuses every connection that is not to loopback. The report counts the
  refusals. A run with a refusal fails.
- The run removes its temporary store and stops its servers in a `finally` block, also when a
  stage fails.
- The report is checked for credentials (the fake provider key and every workspace API key,
  and any `wynk_sk_` prefix) before it is written. A match stops the run with an error, and no
  report is written.

## Stages

| Stage | What it asserts |
| --- | --- |
| `upload` | Public health endpoint. `POST projects` 201. CSV upload 201 with 18 rows, the 4 expected columns and a content hash. The upload reads back. |
| `register` | Register 201 pins the upload's content hash, 18 rows, version 1. The same request again is 200 with the same version. The dataset has 1 version. |
| `split` | Splits 201. The same plan again is 200 with the same `splits_hash`. The three roles are non-empty, disjoint and cover 18 rows. The splits read back by hash. |
| `configure` | `GET models` lists only the fake entry and hides its endpoint. `POST experiments` with `Idempotency-Key` is 201 PENDING. The replay is 201 with `Idempotent-Replayed: true` and the same job. The same key with another body is `409 idempotency_conflict`. Exactly 1 job exists. 0 provider requests. |
| `optimize` | The production worker loop runs the job to COMPLETED (fixed, random and ACO units). The provider got requests with the configured key. No held-out row is in any prompt. |
| `evaluate` | `verify` is verified. The artifact is not synthetic, has 0 test runs and the pinned model hash. Provenance names the uploaded content. `reproduce` reproduces with 0 provider requests. |
| `promote` | Held-out promotion is PROMOTED and opens held-out rows only now. `promotions/{id}/verify` re-derives the decision. The champion and the experiment's promotion read back. A repeated promote is 200 with the same promotion and 0 new requests. |
| `deploy` | Publish 201 CREATED; again 200, same version. Stage at revision 0. Invoke before production is `409 no_production_version`. A stale revision is `409 stale_deployment`. Promote at revision 1 gives production v1, revision 2. |
| `invoke` | Production invoke with `Idempotency-Key`: 200, output `{"answer": "Lincoln"}`, served by v1. `usage.model_calls` equals the counted provider requests. The replay is 200 with `Idempotent-Replayed: true`, the same inference id and output, and 0 provider requests. The same key with another body is `409 idempotency_conflict`. Invoke by version works. A refused input is `422 invalid_inference_request` with 0 requests. The stored record keeps no output, only its `canonical_hash`. |
| `monitor` | `GET workflows/{v1}/monitoring`: inference count = 2 served invocations (a replay is never counted), 0 failed, 1 rejected input. `model_calls.total` = counted provider requests. Tokens = 40 and 8 per request. Cost = tokens × registry prices, authoritative. Pins name the model and the source job. `GET workspace/quota` usage counts. |
| `recovery` | A job cancelled before any worker ran it ends CANCELLED; `resume` is `409 job_not_resumable`. The operator re-pins the model and the process restarts: the next job is INTERRUPTED `runtime_unavailable`, with 0 provider requests. The pinned model is back and the process restarts: `resume` on the same job completes it (warm start from memory). |
| `rollback` | The resumed job is promoted and published as v2 and goes to production. A keyed invoke on v2 works. Rollback to v1 (revision 5). v1 serves again and its document is unchanged. The pre-rollback v1 invocation and the v2 invocation both replay with 0 provider requests; the v2 replay stays bound to v2. The history records the rollback. v1 monitoring counts the new invocation. |
| `provenance` | Production → workflow version → champion and promotion → artifact → experiment → dataset version (identity hash), uploaded content hash, `splits_hash` and split sizes, only through the API. |
| `tenancy_auth` | A second workspace (created over HTTP) lists no experiments of A. For 9 routes, B naming A's id answers exactly like B naming an id that never existed. A's deployment is unchanged. No header, a malformed key, a wrong secret and a revoked key are `401 unauthenticated`. A read-only key cannot invoke, create or roll back, and an invoke-only key cannot read (`403 insufficient_scope`). The allowed calls work. |
| `live_provider_smoke` | **BLOCKED**: no spending authorization. It is never run. |

A failed stage records its first failed assertion and the evidence. The stages after it are
BLOCKED ("depends on the failed stage"). They are not run.

## Reports

A run writes two files (default `.wynk-validation/lifecycle/`, git-ignored):

- `lifecycle-report.json` (machine-readable)
- `lifecycle-report.md` (human-readable stage table)

JSON fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `wynk-lifecycle-validation/1` |
| `provider_mode`, `simulated` | Always `"fake"` and `true` |
| `commit`, `worktree_dirty` | `git rev-parse HEAD` and whether the worktree had changes |
| `python`, `platform` | The interpreter and OS |
| `verdict` | `PASS` only if every stage passed in every run, teardown was clean, no connection was refused, and (for 2+ runs) the runs agree |
| `determinism` | `same_verdicts`, `same_digests` and `same_assertion_counts` across the runs |
| `blocked` | Stages never run, with the reason |
| `runs[]` | Per run: `seconds`, `provider_requests_total`, `non_loopback_connections_refused`, `teardown`, `verdict`, `stages[]` |
| `stages[]` | `name`, `status` (`PASS`, `FAIL`, `BLOCKED`, `NOT_SUPPORTED`), `assertions`, `seconds`, `failure`, `reason`, `facts`, `digest` |

`NOT_SUPPORTED` is reserved in the schema for a step without backend support. No stage uses it
today: the product API supports every stage, monitoring included.

`facts` holds only deterministic values (content and split hashes, row ids, counts, states,
error codes, outputs, usage). `digest` is the `canonical_hash` of `facts`. Two runs with equal
digests produced the same observable results. Ids that the product generates at random (job,
workflow version and inference ids, API keys) and values that depend on the fake provider's
random port (the model hash and what pins it) are not facts. Timings are reported but never
compared.

## Limits

- The harness does not drive the browser. The live UI lifecycle is covered by
  `ui/e2e/live.spec.ts` (Playwright, on `tests/e2e_product_server.py`).
- On a Windows checkout with `core.autocrlf=true` the fixture CSV has CRLF line endings, so its
  content hash differs from an LF checkout. Runs on one checkout still agree.
- The monitoring window is the default policy window ending now (wall clock). The counts do not
  depend on the clock because every invocation of the run is inside it.
- Drift, feedback, triggers, re-optimization, dataset deletion and crashes with a request in
  flight are not repeated here. `tests/test_dogfood_lifecycle.py` and
  `tests/test_inference_idempotency.py` cover them.
