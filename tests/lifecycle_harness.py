"""Phase 3: a reproducible end-to-end validation of the Wynk product lifecycle.

    python -m tests.lifecycle_harness                 # 2 runs -> .wynk-validation/lifecycle/
    python -m tests.lifecycle_harness --runs 1 --out DIR

Upload -> Register -> Split -> Configure -> Optimize -> Evaluate -> Promote -> Deploy -> Invoke
-> Monitor -> Rollback, plus cross-cutting checks (recovery, provenance, tenancy and auth), each
a named stage with explicit assertions on real HTTP responses.

PROVIDER MODE: FAKE. Every run is a SIMULATED-PROVIDER run. What is real: the product API
(``api.product`` routes behind the ``api.tenancy.TenantRouter``, served by the real stdlib HTTP
adapter ``api.product.Server`` / ``make_handler`` on 127.0.0.1), the durable job worker, held-out
promotion, deployment, idempotent invocation and monitoring - wired exactly like
``python -m api.product --model-registry ...`` (``tests.dogfood_support.product``). What is fake:
the far end of the model connection (``tests.dogfood_support.FakeProvider``, a local
OpenAI-compatible server that answers deterministically). No hosted provider is ever called: the
registry lists only the local fake, websearch is not configured, and a socket guard refuses every
non-loopback connection for the duration of a run (and the report counts the refusals).

Each run uses a fresh temporary data directory and removes it - and stops every server - in a
``finally`` block. The live-provider smoke test is BLOCKED: there is no spending authorization.
See docs/lifecycle_validation.md.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.product import Server, make_handler
from core.canonical import canonical_hash
from store.jobs import AUTO_RESUMABLE
from tests.dogfood_support import (
    DATASET_ID,
    FIXTURE,
    LINEAGE,
    NEW_ORDER,
    PROVIDER_KEY,
    REGISTER,
    SPLIT_PLAN,
    FakeProvider,
    contract_for,
    entry_for,
    order_ids,
    plan_for,
    product,
    rows_of,
    write_registry,
)

SCHEMA_VERSION = "wynk-lifecycle-validation/1"
PROVIDER_MODE = "fake"
REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / ".wynk-validation" / "lifecycle"
# A fixed, public, throwaway data key: the temporary store is encrypted at rest like production
# (#33) and deleted at the end of the run. Never a real key.
HARNESS_DATA_KEYS = "lifecycle-harness-only:" + "bGlmZWN5Y2xlLWhhcm5lc3Mtb25seS1rZXktMDAwMDA"
WORKSPACE_A = "ws-lifecycle-a"
JOB_TIMEOUT_S = 180.0
EXPECTED_OUTPUT = {"answer": "Lincoln"}
# FakeProvider usage per request and the registry entry's prices (tests.dogfood_support)
PROMPT_TOKENS, COMPLETION_TOKENS = 40, 8
PROMPT_PER_M, COMPLETION_PER_M = 1.0, 2.0

LIFECYCLE = (
    "upload",
    "register",
    "split",
    "configure",
    "optimize",
    "evaluate",
    "promote",
    "deploy",
    "invoke",
    "monitor",
    "recovery",  # cancel; interrupt -> resume; feeds the second release that rollback needs
    "rollback",
    "provenance",
    "tenancy_auth",
)
BLOCKED_STAGES = {
    "live_provider_smoke": "No prior spending authorization: a hosted model provider is never "
    "called. Every stage above ran against the local fake provider (provider_mode=fake).",
}


# -- recording ---------------------------------------------------------------------------------
class StageFailed(AssertionError):
    pass


@dataclass
class Stage:
    name: str
    status: str = "NOT_RUN"
    assertions: int = 0
    failed: str | None = None
    reason: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)  # deterministic values only
    seconds: float = 0.0

    def check(self, condition: bool, what: str, **evidence: Any) -> None:
        self.assertions += 1
        if not condition:
            detail = json.dumps(evidence, default=str, sort_keys=True)[:600] if evidence else ""
            raise StageFailed(f"{what} {detail}".strip())

    def eq(self, actual: Any, expected: Any, what: str) -> None:
        self.check(actual == expected, what, actual=actual, expected=expected)

    def fact(self, name: str, value: Any) -> Any:
        self.facts[name] = value
        return value

    def report(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "assertions": self.assertions,
            "seconds": round(self.seconds, 3),
            "failure": self.failed,
            "reason": self.reason,
            "facts": self.facts,
            "digest": canonical_hash(self.facts) if self.status == "PASS" else None,
        }


# -- HTTP --------------------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any

    @property
    def code(self) -> str:
        if isinstance(self.body, dict) and "error" in self.body:
            return str(self.body["error"]["code"])
        return "ok"

    @property
    def replayed(self) -> bool:
        return self.headers.get("idempotent-replayed") == "true"


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(
    base: str,
    method: str,
    path: str,
    key: str | None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """One real HTTP request to the product API on 127.0.0.1."""
    h = dict(headers or {})
    data: bytes | None = None
    if key is not None:
        h["Authorization"] = f"Bearer {key}"
    if isinstance(body, bytes):
        data = body
        h.setdefault("Content-Type", "application/octet-stream")
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif method in ("POST", "DELETE"):
        data = b""
    req = urllib.request.Request(f"{base}/api/v1/{path}", data=data, headers=h, method=method)
    try:
        with _NO_PROXY.open(req, timeout=60) as res:
            status, raw, rh = res.status, res.read(), res.headers
    except urllib.error.HTTPError as err:
        status, raw, rh = err.code, err.read(), err.headers
    lowered = {k.lower(): v for k, v in rh.items()}
    payload: Any = raw
    if lowered.get("content-type", "").startswith("application/json"):
        payload = json.loads(raw)
    return Response(status, lowered, payload)


# -- the product under test --------------------------------------------------------------------
class ProductProcess:
    """One "process" of the product: a TenantRouter over the data dir, served over HTTP.
    ``restart`` = the process stopped and a new one started on the same data dir."""

    def __init__(self, data: Path, registry: Path) -> None:
        self.data, self.registry = data, registry
        self.router: Any = None
        self.server: Server | None = None
        self.thread: threading.Thread | None = None
        self.workers: Any = None
        self.base = ""

    def start(self, *, workers: bool) -> ProductProcess:
        self.router = product(self.data, self.registry, key=PROVIDER_KEY)
        self.server = Server(("127.0.0.1", 0), make_handler(self.router))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if workers:
            self.start_workers()
        return self

    def start_workers(self) -> None:
        """Exactly ``api.product.start_worker`` for a router: recover, then run jobs."""
        self.workers = self.router.start_workers()

    def stop(self) -> None:
        if self.workers is not None:
            self.workers.stop()
            self.workers = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=10)
            self.thread = None
        self.router = None
        gc.collect()  # drop sqlite handles before the data dir is removed (Windows)

    def restart(self, *, workers: bool) -> None:
        self.stop()
        self.start(workers=workers)


@contextmanager
def harness_environment() -> Iterator[None]:
    """The run's own environment: a throwaway data key, and no operator registry, websearch
    or data-key file can leak in. Restored afterwards."""
    names = ("WYNK_DATA_KEYS", "WYNK_DATA_KEYS_FILE", "WYNK_MODEL_REGISTRY", "WYNK_MODEL_API_KEY")
    removed = [n for n in os.environ if n.startswith("WYNK_WEBSEARCH_")]
    saved = {n: os.environ.get(n) for n in (*names, *removed)}
    for n in (*names, *removed):
        os.environ.pop(n, None)
    os.environ["WYNK_DATA_KEYS"] = HARNESS_DATA_KEYS
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


@dataclass
class LoopbackGuard:
    """Refuses any socket connection that is not to the loopback interface (no hosted
    provider, search engine or telemetry can be reached during a run)."""

    refused: list[str] = field(default_factory=list)

    @contextmanager
    def installed(self) -> Iterator[LoopbackGuard]:
        real = socket.socket.connect
        guard = self

        def connect(sock: socket.socket, address: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in ("127.0.0.1", "::1", "localhost"):
                guard.refused.append(str(host))
                raise ConnectionRefusedError(
                    f"lifecycle harness: no network beyond loopback ({host})"
                )
            return real(sock, address)

        socket.socket.connect = connect  # type: ignore[method-assign]
        try:
            yield self
        finally:
            socket.socket.connect = real  # type: ignore[method-assign]


# -- one run -------------------------------------------------------------------------------------
class Run:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.provider = FakeProvider().start()
        self.registry = root / "registry.json"
        self.entry = entry_for(self.provider.endpoint)
        write_registry(self.registry, self.entry)
        self.proc = ProductProcess(root / "data", self.registry)
        self.s: dict[str, Any] = {}  # state the stages hand on (ids, keys) - never reported
        self.secrets: list[str] = [PROVIDER_KEY]

    # helpers
    def call(self, method: str, path: str, body: Any = None, *, key: str | None = None, **h: str):
        return http(
            self.proc.base, method, path, self.s["key_a"] if key is None else key, body, h or None
        )

    def ok(self, st: Stage, method: str, path: str, body: Any = None, status: int = 200, **h: str):
        res = self.call(method, path, body, **h)
        st.check(
            res.status == status, f"{method} {path} -> {status}", got=res.status, body=res.body
        )
        return res.body

    def requests(self) -> int:
        return len(self.provider.requests)

    def wait_job(self, st: Stage, job_id: str) -> dict[str, Any]:
        """Poll until the job settles: COMPLETED / FAILED / CANCELLED, or INTERRUPTED for a
        reason a worker does not resume by itself (``store.jobs.AUTO_RESUMABLE``).
        One assertion per wait, however many polls it takes (assertion counts stay
        independent of timing)."""
        deadline = time.monotonic() + JOB_TIMEOUT_S
        while True:
            res = self.call("GET", f"experiments/{job_id}")
            view = res.body
            settled = res.status == 200 and (
                view["state"] in ("COMPLETED", "FAILED", "CANCELLED")
                or (view["state"] == "INTERRUPTED" and view["reason"] not in AUTO_RESUMABLE)
            )
            if settled or res.status != 200 or time.monotonic() >= deadline:
                st.check(settled, "the job settled in time", status=res.status, view=view)
                return view  # type: ignore[no-any-return]
            time.sleep(0.05)

    # -- stages -------------------------------------------------------------------------------
    def upload(self, st: Stage) -> None:
        self.proc.start(workers=False)  # jobs run only once Optimize starts the worker
        self.s["key_a"] = self.proc.router.create_workspace(
            "Lifecycle A", "owner-a@lifecycle.test", workspace_id=WORKSPACE_A
        )[3]
        self.secrets.append(self.s["key_a"])
        health = http(self.proc.base, "GET", "health", None)
        st.eq((health.status, health.body), (200, {"status": "ok"}), "public health endpoint")
        project = self.ok(st, "POST", "projects", {"name": "Order desk"}, 201)
        self.s["project"] = project["project_id"]
        raw = FIXTURE.read_bytes()
        up = self.ok(st, "POST", f"projects/{project['project_id']}/uploads?format=csv", raw, 201)
        self.s["upload"] = up
        st.eq(up["row_count"], 18, "upload row count")
        st.eq(up["format"], "csv", "upload format")
        st.eq(
            [c["name"] for c in up["columns"]],
            ["ticket_id", "question", "order_record", "answer"],
            "upload columns",
        )
        again = self.ok(st, "GET", f"uploads/{up['upload_id']}")
        st.eq(again["content_hash"], up["content_hash"], "upload reads back")
        st.fact("content_hash", up["content_hash"])
        st.fact("row_count", up["row_count"])
        st.fact("columns", [c["name"] for c in up["columns"]])

    def register(self, st: Stage) -> None:
        up = self.s["upload"]
        version = self.ok(st, "POST", f"uploads/{up['upload_id']}/register", REGISTER, 201)
        spec = version["spec"]
        st.eq(spec["content_hash"], up["content_hash"], "registered version pins the upload")
        st.eq(spec["row_count"], 18, "registered row count")
        st.eq(spec["dataset_version"], 1, "first dataset version")
        repeat = self.call("POST", f"uploads/{up['upload_id']}/register", REGISTER)
        st.eq(repeat.status, 200, "registering the identical version again is a 200, not a copy")
        st.eq(repeat.body["spec"], spec, "the repeat returns the same version")
        listed = self.ok(st, "GET", f"datasets/{DATASET_ID}")
        st.eq([v["spec"] for v in listed["versions"]], [spec], "the dataset has exactly 1 version")
        self.s["version"] = version
        st.fact("identity_hash", version["identity_hash"])
        st.fact("spec", spec)

    def split(self, st: Stage) -> None:
        path = f"datasets/{DATASET_ID}/versions/1/splits"
        splits = self.ok(st, "POST", path, SPLIT_PLAN, 201)
        repeat = self.call("POST", path, SPLIT_PLAN)
        st.eq(repeat.status, 200, "the same plan again is a 200")
        st.eq(repeat.body["splits_hash"], splits["splits_hash"], "the same plan, the same splits")
        roles = {r: rows_of(r, splits) for r in ("optimization", "validation", "test")}
        st.check(
            all(roles.values()), "every role has rows", sizes={r: len(v) for r, v in roles.items()}
        )
        union = [x for v in roles.values() for x in v]
        st.eq(len(union), 18, "the splits cover every row")
        st.eq(len(set(union)), 18, "the splits are disjoint")
        stored = self.ok(st, "GET", f"{path}/{splits['splits_hash']}")
        st.eq(stored["splits_hash"], splits["splits_hash"], "the splits read back by hash")
        self.s["splits"] = splits
        self.s["heldout"] = roles["test"]
        st.fact("splits_hash", splits["splits_hash"])
        self.s["split_sizes"] = st.fact("sizes", {r: len(v) for r, v in roles.items()})
        st.fact("roles", roles)

    def experiment_body(self, strategies: tuple[str, ...]) -> dict[str, Any]:
        spec = self.s["version"]["spec"]
        return {
            "dataset_id": DATASET_ID,
            "dataset_version": spec["dataset_version"],
            "splits_hash": self.s["splits"]["splits_hash"],
            "contract": contract_for(spec),
            "plan": plan_for(self.entry, strategies),
        }

    def configure(self, st: Stage) -> None:
        models = self.ok(st, "GET", "models")["models"]
        st.eq([m["model_hash"] for m in models], [self.entry.model_hash], "the registry's 1 model")
        st.check("endpoint" not in json.dumps(models), "the model list hides the endpoint")
        body = self.experiment_body(("fixed", "random", "aco"))
        key = {"Idempotency-Key": "lifecycle-create-1"}
        job = self.call("POST", "experiments", body, **key)
        st.eq(job.status, 201, "experiment created")
        st.eq(job.body["state"], "PENDING", "a new experiment is PENDING")
        st.check(not job.replayed, "a first create is not a replay")
        replay = self.call("POST", "experiments", body, **key)
        st.eq(replay.status, 201, "an idempotent replay answers 201")
        st.check(replay.replayed, "Idempotent-Replayed: true", headers=replay.headers)
        st.eq(replay.body["job_id"], job.body["job_id"], "the replay is the same experiment")
        other = self.experiment_body(("random",))
        conflict = self.call("POST", "experiments", other, **key)
        st.eq(
            (conflict.status, conflict.code), (409, "idempotency_conflict"), "same key, other body"
        )
        jobs = self.ok(st, "GET", "experiments")["jobs"]
        st.eq([j["job_id"] for j in jobs], [job.body["job_id"]], "exactly 1 experiment exists")
        st.eq(self.requests(), 0, "creating an experiment sends no provider request")
        self.s["job1"] = job.body["job_id"]
        st.fact("strategies", job.body["strategies"])
        st.fact("seeds", job.body["seeds"])
        st.fact("budget", job.body["budget"])

    def optimize(self, st: Stage) -> None:
        self.proc.start_workers()  # the production worker loop (api.product.start_worker)
        view = self.wait_job(st, self.s["job1"])
        st.eq(view["state"], "COMPLETED", "the experiment completed")
        st.check(all(u["state"] == "COMPLETED" for u in view["units"]), "every unit completed")
        st.check(view["artifact_available"], "the artifact is available")
        n = self.requests()
        st.check(n > 0, "the search called the (fake) provider")
        st.check(
            set(self.provider.authorizations) == {f"Bearer {PROVIDER_KEY}"},
            "every request carried the configured provider key",
        )
        leaked = [
            p for p in self.provider.prompts() if any(o in p for o in order_ids(self.s["heldout"]))
        ]
        st.eq(len(leaked), 0, "the search never sent a held-out row")
        self.s["search_requests"] = n
        st.fact("search_provider_requests", n)
        st.fact("units", sorted((u["strategy"], u["seed"], u["state"]) for u in view["units"]))

    def evaluate(self, st: Stage) -> None:
        job = self.s["job1"]
        verify = self.ok(st, "GET", f"experiments/{job}/verify")
        st.eq(verify["verified"], True, "the artifact verifies")
        art = self.ok(st, "GET", f"experiments/{job}/artifact")["artifact"]
        st.eq(art["synthetic"], False, "a real (non-synthetic) run")
        st.eq(art["test_runs"], 0, "no held-out row was evaluated during optimization")
        st.eq(art["model_hashes"], [self.entry.model_hash], "the pinned model ran")
        prov = self.ok(st, "GET", f"experiments/{job}/provenance")["provenance"]
        st.eq(
            prov["dataset"]["content_hash"], self.s["upload"]["content_hash"], "provenance dataset"
        )
        before = self.requests()
        reproduced = self.ok(st, "GET", f"experiments/{job}/reproduce")
        st.eq(self.requests(), before, "reproduce replays stored evidence without a model call")
        st.check(
            reproduced.get("reproduced") is True, "every strategy run reproduces", body=reproduced
        )
        self.s["artifact_id"] = verify["artifact_id"]
        st.fact("strategies_run", sorted({r["strategy"] for r in art["memory"]["runs"]}))
        st.fact("reproduce", {k: v for k, v in reproduced.items() if k in ("reproduced", "runs")})

    def promote(self, st: Stage) -> None:
        job = self.s["job1"]
        before = self.requests()
        promo = self.ok(st, "POST", f"experiments/{job}/promote", status=201)
        st.eq(promo["decision"], "PROMOTED", "held-out promotion decision")
        held = [
            p
            for p in self.provider.prompts()[before:]
            if any(o in p for o in order_ids(self.s["heldout"]))
        ]
        st.check(len(held) > 0, "promotion opened the held-out rows now")
        verify = self.ok(st, "GET", f"promotions/{promo['promotion_id']}/verify")
        st.eq(verify["decision"], "PROMOTED", "the decision re-derives from stored evidence")
        champion = promo["record"]["champion"]["champion_id"]
        st.eq(self.ok(st, "GET", f"champions/{LINEAGE}")["champion_id"], champion, "the champion")
        by_job = self.ok(st, "GET", f"experiments/{job}/promotion")
        st.eq(by_job["promotion_id"], promo["promotion_id"], "the experiment names its promotion")
        again_before = self.requests()
        again = self.call("POST", f"experiments/{job}/promote")
        st.eq(again.status, 200, "a repeated promote returns the existing decision")
        st.eq(again.body["promotion_id"], promo["promotion_id"], "the same promotion")
        st.eq(self.requests(), again_before, "a repeated promote re-sends no held-out request")
        self.s["champion1"] = champion
        st.fact("decision", promo["decision"])
        st.fact("heldout_provider_requests", self.requests() - before)
        st.fact("repeat_promote", [again.status, again.code])

    def deploy(self, st: Stage) -> None:
        wv = self.ok(st, "POST", "workflows", {"champion_id": self.s["champion1"]}, 201)
        v1 = wv["version_id"]
        st.eq(wv["state"], "CREATED", "a published version is CREATED")
        again = self.call("POST", "workflows", {"champion_id": self.s["champion1"]})
        st.eq((again.status, again.body["version_id"]), (200, v1), "publishing is idempotent")
        dep = self.ok(st, "GET", f"deployments/{LINEAGE}")
        st.eq((dep["production_version_id"], dep["revision"]), (None, 0), "nothing in production")
        staged = self.ok(st, "POST", f"workflows/{v1}/stage", {"expected_revision": 0})
        st.eq(staged["staging_version_id"], v1, "staged")
        st.eq(staged["production_version_id"], None, "staging is not production")
        early = self.call("POST", f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER})
        st.eq((early.status, early.code), (409, "no_production_version"), "no production yet")
        stale = self.call("POST", f"workflows/{v1}/promote", {"expected_revision": 0})
        st.eq((stale.status, stale.code), (409, "stale_deployment"), "revision fence")
        prod = self.ok(st, "POST", f"workflows/{v1}/promote", {"expected_revision": 1})
        st.eq((prod["production_version_id"], prod["revision"]), (v1, 2), "v1 in production")
        self.s["v1"], self.s["doc1"] = v1, wv["document"]
        st.fact("revisions", [dep["revision"], staged["revision"], prod["revision"]])
        st.fact("refusals", [early.code, stale.code])

    def invoke(self, st: Stage) -> None:
        v1, path = self.s["v1"], f"deployments/{LINEAGE}/invoke"
        k1 = {"Idempotency-Key": "lifecycle-invoke-1"}
        before = self.requests()
        first = self.call("POST", path, {"inputs": NEW_ORDER}, **k1)
        st.eq(first.status, 200, "production invoke")
        inf = first.body
        st.eq((inf["status"], inf["output"]), ("SUCCEEDED", EXPECTED_OUTPUT), "the answer")
        st.eq((inf["workflow_version"], inf["addressed_by"]), (v1, "production"), "served by v1")
        st.check(not first.replayed, "a first invoke is not a replay")
        calls = self.requests() - before
        st.check(calls > 0, "the invoke called the provider")
        st.eq(inf["usage"]["model_calls"], calls, "usage.model_calls = provider requests")
        mark = self.requests()
        replay = self.call("POST", path, {"inputs": NEW_ORDER}, **k1)
        st.eq(replay.status, 200, "replay 200")
        st.check(replay.replayed, "Idempotent-Replayed: true", headers=replay.headers)
        st.eq(replay.body["inference_id"], inf["inference_id"], "the replay is the same invocation")
        st.eq(replay.body["output"], inf["output"], "the replay returns the same output")
        other = {**NEW_ORDER, "question": "Which city does order ORD-2001 go to?"}
        conflict = self.call("POST", path, {"inputs": other}, **k1)
        st.eq(
            (conflict.status, conflict.code), (409, "idempotency_conflict"), "same key, other body"
        )
        st.eq(self.requests(), mark, "replay and conflict send no provider request")
        by_version = self.ok(st, "POST", f"workflows/{v1}/invoke", {"inputs": NEW_ORDER})
        st.eq(
            (by_version["addressed_by"], by_version["output"]),
            ("version", EXPECTED_OUTPUT),
            "by version",
        )
        mark = self.requests()
        bad = self.call("POST", path, {"inputs": {"question": "no record"}})
        st.eq(
            (bad.status, bad.code),
            (422, "invalid_inference_request"),
            "an input the pinned schema refuses",
        )
        st.eq(self.requests(), mark, "a refused input sends no provider request")
        record = self.ok(st, "GET", f"inferences/{inf['inference_id']}")
        st.eq(record["output"], None, "the stored record keeps no plaintext output")
        st.eq(record["output_sha256"], canonical_hash(EXPECTED_OUTPUT), "the record keeps its hash")
        st.eq(record["versions"], self.s["doc1"]["versions"], "the record pins v1's versions")
        self.s["inv1"] = inf
        self.s["invoke_calls"] = self.requests() - before
        self.s["served_v1"] = 2  # the keyed production invoke and the by-version invoke
        st.fact("output", inf["output"])
        st.fact("output_sha256", record["output_sha256"])
        st.fact("provider_requests", self.s["invoke_calls"])
        st.fact("refusals", [conflict.code, bad.code])

    def monitor(self, st: Stage) -> None:
        v1, calls = self.s["v1"], self.s["invoke_calls"]
        m = self.ok(st, "GET", f"workflows/{v1}/monitoring")
        served = self.s["served_v1"]
        st.eq(
            m["inferences"]["count"], served, "every served invocation is counted, a replay never"
        )
        st.eq(m["inferences"]["succeeded"], served, "all succeeded")
        st.eq(m["inferences"]["failed"], 0, "none failed")
        st.eq(m["inferences"]["rejected_inputs"], 1, "the refused input is counted apart")
        st.eq(m["model_calls"]["total"], calls, "model calls = real provider requests")
        st.eq(m["tokens"]["prompt_tokens"]["total"], PROMPT_TOKENS * calls, "prompt tokens")
        st.eq(
            m["tokens"]["completion_tokens"]["total"],
            COMPLETION_TOKENS * calls,
            "completion tokens",
        )
        cost = calls * (PROMPT_TOKENS * PROMPT_PER_M + COMPLETION_TOKENS * COMPLETION_PER_M) / 1e6
        st.eq(m["cost"]["authoritative"], True, "the registry prices the model")
        st.check(
            abs(m["cost"]["total"] - cost) < 1e-9,
            "cost = tokens x registry prices",
            got=m["cost"],
            want=cost,
        )
        st.eq(m["pins"]["model_hash"], self.entry.model_hash, "monitoring pins the served model")
        st.eq(m["pins"]["source_job_id"], self.s["job1"], "monitoring names the source experiment")
        quota = self.ok(st, "GET", "workspace/quota")["usage"]
        want = {"projects": 1, "uploads": 1, "dataset_versions": 1, "workflow_versions": 1}
        st.eq({k: quota[k] for k in want}, want, "workspace usage counts")
        st.fact("inferences", m["inferences"])
        st.fact("model_calls", m["model_calls"])
        st.fact("tokens", m["tokens"])
        st.fact("cost", m["cost"])
        st.fact("usage", {k: quota[k] for k in want})

    def recovery(self, st: Stage) -> None:
        # (a) cancel: a job cancelled before any worker ran it never runs and cannot resume
        self.proc.restart(workers=False)
        mark = self.requests()
        doomed = self.ok(st, "POST", "experiments", self.experiment_body(("random",)), 201)
        cancelled = self.ok(st, "POST", f"experiments/{doomed['job_id']}/cancel")
        st.check(cancelled["cancel_requested_at"] is not None, "the cancel is recorded")
        # (b) interrupt: the operator re-pins the model; the job cannot bind and is INTERRUPTED
        job2 = self.ok(st, "POST", "experiments", self.experiment_body(("aco_warm",)), 201)
        write_registry(self.registry, entry_for(self.provider.endpoint, revision="2026-11-01"))
        self.proc.restart(workers=True)
        view = self.wait_job(st, job2["job_id"])
        st.eq(
            (view["state"], view["reason"]), ("INTERRUPTED", "runtime_unavailable"), "interrupted"
        )
        gone = self.wait_job(st, doomed["job_id"])
        st.eq(gone["state"], "CANCELLED", "the cancelled job ends CANCELLED")
        refused = self.call("POST", f"experiments/{doomed['job_id']}/resume")
        st.eq((refused.status, refused.code), (409, "job_not_resumable"), "CANCELLED is final")
        st.eq(self.requests(), mark, "cancel and interrupt sent no provider request")
        # (c) the pinned model is back: the SAME job resumes and completes
        write_registry(self.registry, self.entry)
        self.proc.restart(workers=True)
        resumed = self.ok(st, "POST", f"experiments/{job2['job_id']}/resume")
        # the unit becomes auto-resumable (INTERRUPTED / resume_requested) until a worker
        # claims it; a fast worker may already have done so
        st.check(
            resumed["reason"] == "resume_requested" or resumed["state"] in ("RUNNING", "COMPLETED"),
            "resume accepted",
            state=resumed["state"],
            reason=resumed["reason"],
            units=[(u["strategy"], u["state"], u.get("reason")) for u in resumed["units"]],
        )
        done = self.wait_job(st, job2["job_id"])
        st.eq(done["state"], "COMPLETED", "the resumed job completed")
        art = self.ok(st, "GET", f"experiments/{job2['job_id']}/artifact")["artifact"]
        (run,) = art["memory"]["runs"]
        st.eq(run["mode"], "warm_dataset_champion", "the resumed job warm-started from memory")
        self.s["job2"] = job2["job_id"]
        st.fact("cancel", [gone["state"], refused.code])
        st.fact("interrupted", [view["state"], view["reason"]])
        st.fact("resumed", done["state"])
        st.fact("resumed_provider_requests", self.requests() - mark)

    def rollback(self, st: Stage) -> None:
        v1 = self.s["v1"]
        promo = self.ok(st, "POST", f"experiments/{self.s['job2']}/promote", status=201)
        st.eq(promo["decision"], "PROMOTED", "the second experiment is promoted")
        champion2 = promo["record"]["champion"]["champion_id"]
        st.check(champion2 != self.s["champion1"], "a new champion")
        v2 = self.ok(st, "POST", "workflows", {"champion_id": champion2}, 201)["version_id"]
        st.check(v2 != v1, "a second immutable version")
        rev = self.ok(st, "GET", f"deployments/{LINEAGE}")["revision"]
        self.ok(st, "POST", f"workflows/{v2}/stage", {"expected_revision": rev})
        prod = self.ok(st, "POST", f"workflows/{v2}/promote", {"expected_revision": rev + 1})
        st.eq(prod["production_version_id"], v2, "v2 in production")
        k2 = {"Idempotency-Key": "lifecycle-invoke-2"}
        inv2 = self.call("POST", f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER}, **k2)
        st.eq((inv2.status, inv2.body["workflow_version"]), (200, v2), "v2 serves")
        back = self.ok(
            st, "POST", f"deployments/{LINEAGE}/rollback", {"expected_revision": rev + 2}
        )
        st.eq((back["production_version_id"], back["revision"]), (v1, rev + 3), "rolled back to v1")
        after = self.ok(st, "POST", f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER})
        st.eq(
            (after["workflow_version"], after["output"]), (v1, EXPECTED_OUTPUT), "v1 serves again"
        )
        st.eq(self.ok(st, "GET", f"workflows/{v1}")["document"], self.s["doc1"], "v1 is unchanged")
        mark = self.requests()
        r1 = self.call(
            "POST", f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER},
            **{"Idempotency-Key": "lifecycle-invoke-1"},
        )  # fmt: skip
        st.check(r1.replayed and r1.status == 200, "a pre-rollback invocation still replays")
        st.eq(r1.body["inference_id"], self.s["inv1"]["inference_id"], "the same invocation")
        st.eq(r1.body["output"], EXPECTED_OUTPUT, "the same output")
        r2 = self.call("POST", f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER}, **k2)
        st.check(r2.replayed and r2.status == 200, "the v2 invocation replays after the rollback")
        st.eq(r2.body["workflow_version"], v2, "a replay keeps the version it was bound to")
        st.eq(self.requests(), mark, "replays after the rollback send no provider request")
        history = self.ok(st, "GET", f"deployments/{LINEAGE}/history")
        st.check("rollback" in json.dumps(history).lower(), "the history records the rollback")
        m = self.ok(st, "GET", f"workflows/{v1}/monitoring")
        st.eq(
            m["inferences"]["count"], self.s["served_v1"] + 1, "v1 monitoring counts the new call"
        )
        self.s["v2"] = v2
        st.fact("revisions", [rev, prod["revision"], back["revision"]])
        st.fact("outputs", [inv2.body["output"], after["output"]])
        st.fact("replays", [r1.replayed, r2.replayed])

    def provenance(self, st: Stage) -> None:
        """Production -> workflow version -> champion / promotion -> experiment -> dataset
        version -> splits -> upload, read only through the API."""
        v1 = self.ok(st, "GET", f"deployments/{LINEAGE}")["production_version_id"]
        st.eq(v1, self.s["v1"], "production is v1 (after the rollback)")
        doc = self.ok(st, "GET", f"workflows/{v1}")["document"]
        st.eq(doc["provenance"]["artifact_id"], self.s["artifact_id"], "version -> artifact")
        st.eq(doc["champion"]["champion_id"], self.s["champion1"], "version -> champion")
        promotion_id = doc["champion"]["promotion_id"]
        promotion = self.ok(st, "GET", f"promotions/{promotion_id}")
        st.eq(promotion["decision"], "PROMOTED", "version -> promotion decision")
        inf = self.ok(st, "GET", f"inferences/{self.s['inv1']['inference_id']}")
        p = inf["provenance"]
        st.eq(p["promotion_id"], promotion_id, "inference -> promotion")
        st.eq(p["artifact_id"], self.s["artifact_id"], "inference -> artifact")
        job = p["job_id"]
        st.eq(job, self.s["job1"], "inference -> experiment")
        jv = self.ok(st, "GET", f"experiments/{job}")
        st.eq((jv["dataset_id"], jv["dataset_version"]), (DATASET_ID, 1), "experiment -> dataset")
        st.eq(jv["experiment_id"], p["experiment_id"], "the same experiment identity")
        prov = self.ok(st, "GET", f"experiments/{job}/provenance")["provenance"]
        dataset, splits = prov["dataset"], prov["splits"]
        st.eq(dataset["content_hash"], self.s["upload"]["content_hash"], "-> the uploaded bytes")
        st.eq(
            dataset["identity_hash"], self.s["version"]["identity_hash"], "-> the dataset version"
        )
        st.eq(splits["splits_hash"], self.s["splits"]["splits_hash"], "-> the stored splits")
        st.eq(splits["rows"], self.s["split_sizes"], "-> the split sizes")
        st.eq(inf["versions"]["model_hash"], self.entry.model_hash, "inference -> pinned model")
        st.fact(
            "dataset", {k: dataset[k] for k in ("dataset_id", "dataset_version", "content_hash")}
        )
        st.fact("splits", {k: splits[k] for k in ("splits_hash", "method", "rows")})

    def tenancy_auth(self, st: Stage) -> None:
        mark = self.requests()
        a_before = self.ok(st, "GET", f"deployments/{LINEAGE}")
        created = self.ok(st, "POST", "workspaces", {"name": "Lifecycle B"}, 201)
        key_b = created["api_key"]["secret"]
        self.secrets.append(key_b)
        st.check(created["workspace"]["workspace_id"] != WORKSPACE_A, "B is another workspace")
        b_jobs = self.call("GET", "experiments", key=key_b)
        st.eq((b_jobs.status, b_jobs.body["jobs"]), (200, []), "B lists none of A's experiments")
        seen = []
        fake_job, fake_v = "j-" + "0" * 24, "wv-" + "0" * 24
        inf_id = self.s["inv1"]["inference_id"]
        fake_inf = inf_id[:-6] + "000000"
        probes = (
            ("GET", "experiments/{}", self.s["job1"], fake_job, None),
            ("GET", "experiments/{}/artifact", self.s["job1"], fake_job, None),
            ("GET", "workflows/{}", self.s["v1"], fake_v, None),
            ("POST", "workflows/{}/invoke", self.s["v1"], fake_v, {"inputs": NEW_ORDER}),
            ("GET", "workflows/{}/monitoring", self.s["v1"], fake_v, None),
            ("GET", "deployments/{}", LINEAGE, "never.deployed", None),
            ("POST", "deployments/{}/invoke", LINEAGE, "never.deployed", {"inputs": NEW_ORDER}),
            ("GET", "inferences/{}", inf_id, fake_inf, None),
            ("GET", "uploads/{}", self.s["upload"]["upload_id"], "u-" + "0" * 32, None),
        )
        for method, path, real, fake, body in probes:
            got = self.call(method, path.format(real), body, key=key_b)
            never = self.call(method, path.format(fake), body, key=key_b)
            st.check(got.status in (404, 409), f"B cannot reach A's {path}", got=got.status)
            seen.append([method, path, got.status, got.code])
            st.eq(
                (got.status, json.dumps(got.body).replace(real, "ID")),
                (never.status, json.dumps(never.body).replace(fake, "ID")),
                f"B naming A's {path} looks exactly like a missing id",
            )
        # auth: missing, malformed, wrong-secret and revoked keys are 401; under-scoped keys 403
        unauth = (401, "unauthenticated")
        refusals: list[list[Any]] = []
        no_header = http(self.proc.base, "GET", "projects", None)
        st.eq((no_header.status, no_header.code), unauth, "no Authorization header")
        refusals.append(["no Authorization header", no_header.status, no_header.code])
        bad = http(self.proc.base, "GET", "projects", "not-a-key")
        st.eq((bad.status, bad.code), unauth, "malformed key")
        refusals.append(["malformed key", bad.status, bad.code])
        a = self.s["key_a"]
        forged = a[:-4] + ("AAAA" if not a.endswith("AAAA") else "BBBB")
        wrong = http(self.proc.base, "GET", "projects", forged)
        st.eq((wrong.status, wrong.code), unauth, "wrong secret")
        refusals.append(["wrong secret", wrong.status, wrong.code])
        reader = self.ok(st, "POST", "workspace/keys", {"name": "reader", "scopes": ["read"]}, 201)
        invoker = self.ok(
            st, "POST", "workspace/keys", {"name": "invoker", "scopes": ["invoke"]}, 201
        )
        r, i = reader["secret"], invoker["secret"]
        self.secrets += [r, i]
        denied = (403, "insufficient_scope")
        inv = f"deployments/{LINEAGE}/invoke"
        for key, method, path, body in (
            (r, "POST", inv, {"inputs": NEW_ORDER}),
            (r, "POST", "experiments", self.experiment_body(("fixed",))),
            (r, "POST", f"deployments/{LINEAGE}/rollback", {"expected_revision": 0}),
            (i, "GET", f"deployments/{LINEAGE}", None),
            (i, "GET", f"inferences/{inf_id}", None),
        ):
            res = http(self.proc.base, method, path, key, body)
            st.eq((res.status, res.code), denied, f"under-scoped {method} {path}")
            refusals.append([method, path.split("/")[0], res.status, res.code])
        st.eq(
            http(self.proc.base, "GET", f"deployments/{LINEAGE}", r).status, 200, "read key reads"
        )
        ok = http(self.proc.base, "POST", inv, i, {"inputs": NEW_ORDER})
        st.eq((ok.status, ok.body["output"]), (200, EXPECTED_OUTPUT), "invoke key invokes")
        self.ok(st, "POST", f"workspace/keys/{reader['key']['key_id']}/revoke")
        revoked = http(self.proc.base, "GET", f"deployments/{LINEAGE}", r)
        st.eq((revoked.status, revoked.code), unauth, "revoked key")
        refusals.append(["revoked key", revoked.status, revoked.code])
        st.eq(
            self.requests() - mark, ok.body["usage"]["model_calls"], "only the allowed invoke ran"
        )
        st.eq(self.ok(st, "GET", f"deployments/{LINEAGE}"), a_before, "A is unchanged by B")
        st.fact("cross_workspace", seen)
        st.fact("refusals", refusals)


@dataclass
class RunResult:
    index: int
    stages: list[Stage]
    seconds: float
    provider_requests: int
    refused_connections: list[str]
    teardown: dict[str, Any]
    secrets: list[str]

    def report(self) -> dict[str, Any]:
        return {
            "run": self.index,
            "provider_mode": PROVIDER_MODE,
            "seconds": round(self.seconds, 3),
            "provider_requests_total": self.provider_requests,
            "non_loopback_connections_refused": len(self.refused_connections),
            "teardown": self.teardown,
            "verdict": verdict(self.stages, self.teardown, len(self.refused_connections)),
            "stages": [s.report() for s in self.stages],
        }


def verdict(stages: list[Stage], teardown: dict[str, Any], refused: int) -> str:
    required = [s for s in stages if s.name in LIFECYCLE]
    ok = all(s.status == "PASS" for s in required) and teardown["clean"] and refused == 0
    return "PASS" if ok else "FAIL"


def run_stages(run: Run, stages: list[Stage]) -> None:
    failed: str | None = None
    for st in stages:
        if failed is not None:
            st.status, st.reason = "BLOCKED", f"depends on the failed stage {failed!r}"
            continue
        t0 = time.perf_counter()
        try:
            getattr(run, st.name)(st)
            st.status = "PASS"
        except Exception as exc:  # a failed assertion or an unexpected error
            st.status, failed = "FAIL", st.name
            st.failed = f"{type(exc).__name__}: {exc}"[:1200]
        st.seconds = time.perf_counter() - t0


def run_once(index: int) -> RunResult:
    """One run from a fresh temporary store; the store is removed and every server stopped
    whatever happens."""
    root = Path(tempfile.mkdtemp(prefix="wynk-lifecycle-"))
    guard = LoopbackGuard()
    stages = [Stage(n) for n in LIFECYCLE]
    run: Run | None = None
    requests, secrets, problems = 0, [PROVIDER_KEY], []
    started = time.perf_counter()
    with harness_environment(), guard.installed():
        try:
            run = Run(root)
            run_stages(run, stages)
        except Exception as exc:  # the run could not even be set up
            for st in stages:
                st.status, st.reason = "BLOCKED", f"setup failed: {type(exc).__name__}: {exc}"
        finally:
            if run is not None:
                requests, secrets = len(run.provider.requests), list(run.secrets)
                for stop in (run.proc.stop, run.provider.stop):
                    try:
                        stop()
                    except Exception as exc:
                        problems.append(f"{type(exc).__name__}: {exc}")
            servers_stopped = run is None or (run.proc.server is None and run.proc.thread is None)
            run = None
            gc.collect()
            try:
                remove_tree(root)
            except OSError as exc:
                problems.append(f"{type(exc).__name__}: {exc}")
    for name, reason in BLOCKED_STAGES.items():
        stages.append(Stage(name, status="BLOCKED", reason=reason))
    left = (
        sorted(str(f.relative_to(root)) for f in root.rglob("*") if f.is_file())[:10]
        if root.exists()
        else []
    )
    teardown = {
        "store_removed": not root.exists(),
        "servers_stopped": servers_stopped,
        "left_behind": left,
        "errors": problems[:5],
        "clean": not root.exists() and servers_stopped and not problems,
    }
    return RunResult(
        index, stages, time.perf_counter() - started, requests, guard.refused, teardown, secrets
    )


def remove_tree(root: Path) -> None:
    """``rmtree`` that also removes read-only files (a blob store's ``TENANT`` binding is
    written 0o444, which Windows refuses to delete as is)."""

    def writable_then_retry(func: Callable[..., Any], path: str, *_: Any) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=writable_then_retry)
    else:
        shutil.rmtree(root, onerror=writable_then_retry)


# -- reports -----------------------------------------------------------------------------------
def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_report(results: list[RunResult]) -> dict[str, Any]:
    runs = [r.report() for r in results]

    def signature(run: dict[str, Any], *fields: str) -> str:
        return json.dumps([[s[f] for f in ("name", "status", *fields)] for s in run["stages"]])

    same_verdicts = len({signature(r) for r in runs}) == 1
    same_digests = len({signature(r, "digest") for r in runs}) == 1
    same_assertions = len({signature(r, "assertions") for r in runs}) == 1
    deterministic = len(runs) >= 2 and same_verdicts and same_digests and same_assertions
    passed = all(r["verdict"] == "PASS" for r in runs) and (len(runs) < 2 or deterministic)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider_mode": PROVIDER_MODE,
        "simulated": True,
        "commit": _git("rev-parse", "HEAD"),
        "worktree_dirty": _git("status", "--porcelain") not in ("", "unknown"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "verdict": "PASS" if passed else "FAIL",
        "determinism": {
            "runs": len(runs),
            "same_verdicts": same_verdicts,
            "same_digests": same_digests,
            "same_assertion_counts": same_assertions,
            "deterministic": deterministic,
        },
        "blocked": BLOCKED_STAGES,
        "runs": runs,
    }


def markdown(report: dict[str, Any]) -> str:
    runs = report["runs"]
    lines = [
        "# Wynk product lifecycle validation",
        "",
        "**Provider mode: FAKE (simulated provider; no hosted model was called).**",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Schema: `{report['schema_version']}`",
        f"- Commit: `{report['commit']}`"
        + (" (dirty worktree)" if report["worktree_dirty"] else ""),
        f"- Python: {report['python']} on {report['platform']}",
        f"- Runs: {len(runs)}; same verdicts: {report['determinism']['same_verdicts']}; "
        f"same digests: {report['determinism']['same_digests']}; "
        f"same assertion counts: {report['determinism']['same_assertion_counts']}",
        "",
        "| Stage | "
        + " | ".join(f"Run {r['run']}" for r in runs)
        + " | Assertions | Digest (run 1) |",
        "|---|" + "---|" * len(runs) + "---|---|",
    ]
    for i, stage in enumerate(runs[0]["stages"]):
        cells = [r["stages"][i]["status"] for r in runs]
        digest = (stage["digest"] or "")[:12]
        lines.append(
            f"| {stage['name']} | " + " | ".join(cells) + f" | {stage['assertions']} | `{digest}` |"
        )
    lines += ["", "## Runs", ""]
    for r in runs:
        lines.append(
            f"- Run {r['run']}: {r['verdict']}, {r['seconds']} s, "
            f"{r['provider_requests_total']} fake-provider requests, "
            f"{r['non_loopback_connections_refused']} non-loopback connections refused, "
            f"teardown clean: {r['teardown']['clean']}"
        )
    problems = [(r["run"], s) for r in runs for s in r["stages"] if s["status"] != "PASS"]
    if problems:
        lines += ["", "## Not passed", ""]
        for run_no, s in problems:
            lines.append(
                f"- Run {run_no}, `{s['name']}`: {s['status']} - {s['failure'] or s['reason']}"
            )
    lines.append("")
    return "\n".join(lines)


def scrub_check(text: str, secrets: list[str]) -> None:
    leaked = [s for s in secrets if s and s in text]
    if leaked or "wynk_sk_" in text:
        raise AssertionError("a credential reached the report")


def run_harness(runs: int = 2, out: Path | None = DEFAULT_OUT) -> dict[str, Any]:
    results = [run_once(i + 1) for i in range(runs)]
    report = build_report(results)
    text = json.dumps(report, indent=2, sort_keys=True)
    md = markdown(report)
    for r in results:
        scrub_check(text + md, r.secrets)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "lifecycle-report.json").write_text(text + "\n", encoding="utf-8")
        (out / "lifecycle-report.md").write_text(md, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--runs", type=int, default=2, help="consecutive runs to compare (default 2)")
    p.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"report dir (default {DEFAULT_OUT})"
    )
    args = p.parse_args(argv)
    report = run_harness(max(1, args.runs), args.out)
    print(markdown(report))
    print(f"reports: {args.out / 'lifecycle-report.json'} and lifecycle-report.md")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
