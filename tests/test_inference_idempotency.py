"""Production inference idempotency: ``POST /deployments/{lineage}/invoke`` (and
``/workflows/{version}/invoke``) with an ``Idempotency-Key`` runs at most one invocation per
(workspace, operation, key), bound to the version the first request resolved, and never makes a
second provider request for it.

Every test drives the real multi-tenant router of ``tests.test_dogfood_lifecycle`` (real MAF
``WorkflowRunner``, real ``OpenAICompatibleClient``, registry file re-read per invocation). The
only fake is the far end of the HTTP connection (``FakeProvider``), which records every request
it receives - so "nothing was sent again" is a count of real HTTP requests, never inferred from
database rows. Crashes are ``Crash`` (a ``BaseException``: nothing in the product catches it, as
with a killed process); "later" moves only the observing process's lease clock.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from api.product import ApiResponse
from core.canonical import canonical_json
from store.crypto import DecryptionError
from store.deployments import InvocationState, SQLiteDeploymentStore, VersionState
from store.jobs import IdempotencyClaim
from tests.conftest import TEST_DATA_KEYS
from tests.dogfood_support import LINEAGE, FakeProvider, product
from tests.test_dogfood_lifecycle import NEW_ORDER, Crash, World, release

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = f"deployments/{LINEAGE}/invoke"
OTHER_ORDER = {**NEW_ORDER, "question": "Which city does order ORD-2001 go to?"}


# -- harness ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shared_provider():
    p = FakeProvider().start()
    yield p
    p.stop()


@pytest.fixture(scope="module")
def template(tmp_path_factory, shared_provider) -> dict[str, Any]:
    """One workspace with a deployed champion v1 (production) and a published champion v2 of
    the same lineage (not deployed), built once; every test gets its own copy."""
    root = tmp_path_factory.mktemp("template")
    with pytest.MonkeyPatch.context() as m:  # module scope: before the autouse test keyring
        m.delenv("WYNK_DATA_KEYS_FILE", raising=False)
        m.setenv("WYNK_DATA_KEYS", TEST_DATA_KEYS)
        w = World(root, shared_provider)
        ws, key = w.workspace("Desk", "owner@example.test")
        data = w.dataset(key)
        v1 = release(w, ws, key, data, ("fixed", "random", "aco"))["version"]
        w.deploy(key, v1)
        v2 = release(w, ws, key, data, ("aco_warm",))["version"]
    assert v1 != v2
    return {"root": root, "ws": ws, "key": key, "v1": v1, "v2": v2}


class Desk:
    def __init__(self, w: World, t: dict[str, Any]) -> None:
        self.w, self.ws, self.key, self.v1, self.v2 = w, t["ws"], t["key"], t["v1"], t["v2"]

    @property
    def provider(self) -> FakeProvider:
        return self.w.provider

    def sent(self) -> int:
        """HTTP requests the model provider actually received."""
        return len(self.w.provider.requests)

    def router(self) -> Any:
        """Another server process over the same data directory."""
        return product(self.w.data, self.w.registry)

    def deployments(self, r: Any = None) -> Any:
        return (r or self.w.r).tenant(self.ws).api.deployments

    def later(self, r: Any = None, s: float = 3600.0) -> None:
        """This router's process observes the wall clock ``s`` seconds later."""
        self.deployments(r).lease_clock = lambda: time.time() + s

    def rows(self, table: str) -> int:
        conn = sqlite3.connect(self.deployments().store.path)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        return {t: self.rows(t) for t in ("inference_records", "inference_invocations")}

    def state(self) -> list[str]:
        conn = sqlite3.connect(self.deployments().store.path)
        try:
            return [r[0] for r in conn.execute("SELECT state FROM inference_invocations")]
        finally:
            conn.close()

    def deploy(self, version: str) -> None:
        self.w.deploy(self.key, version)


@pytest.fixture
def desk(tmp_path, template, shared_provider) -> Any:
    shutil.copytree(template["root"], tmp_path / "w")
    shared_provider.requests.clear()
    shared_provider.authorizations.clear()
    shared_provider.status = 200
    shared_provider.on_request = None
    yield Desk(World(tmp_path / "w", shared_provider), template)
    shared_provider.on_request = None


def invoke(
    r: Any,
    key: str | None,
    idem: str | None = None,
    inputs: dict[str, Any] = NEW_ORDER,
    path: str = PRODUCTION,
    raw: bytes | None = None,
) -> ApiResponse:
    body = raw if raw is not None else json.dumps({"inputs": inputs}).encode()
    h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if key is not None:
        h["Authorization"] = f"Bearer {key}"
    if idem is not None:
        h["Idempotency-Key"] = idem
    return r.handle("POST", "/api/v1/" + path, h, io.BytesIO(body))


def replayed(res: ApiResponse) -> bool:
    return dict(res.headers).get("Idempotent-Replayed") == "true"


def err(res: ApiResponse, status: int, code: str) -> dict[str, Any]:
    assert (res.status, res.body.get("error", {}).get("code")) == (status, code), res.body
    return res.body["error"]


def blocked_provider(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Every provider request waits for ``release`` (``entered`` once the first one waits)."""
    from runtime.backends.openai_compatible import OpenAICompatibleClient

    real = OpenAICompatibleClient._post_once
    entered, release_ = threading.Event(), threading.Event()

    def held(self, payload):
        entered.set()
        assert release_.wait(120)
        return real(self, payload)

    monkeypatch.setattr(OpenAICompatibleClient, "_post_once", held)
    return entered, release_


def in_thread(fn) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


# == 1. replay and conflict =====================================================================
def test_without_a_key_every_invoke_runs_as_before(desk):
    a = invoke(desk.w.r, desk.key)
    calls = desk.sent()
    b = invoke(desk.w.r, desk.key)
    assert a.status == b.status == 200 and a.headers == b.headers == ()
    assert a.body["inference_id"] != b.body["inference_id"]
    assert a.body["output"] == b.body["output"] == {"answer": "Lincoln"}
    assert desk.sent() == 2 * calls == 2 * a.body["usage"]["model_calls"] > 0
    assert desk.counts() == {"inference_records": 2, "inference_invocations": 0}


def test_a_retry_after_a_lost_response_reuses_the_invocation_and_sends_nothing(desk):
    first = invoke(desk.w.r, desk.key, "retry-1")  # the client never reads this response
    assert first.status == 200 and not replayed(first)
    assert first.body["output"] == {"answer": "Lincoln"}
    calls = desk.sent()
    assert calls == first.body["usage"]["model_calls"] > 0

    for _ in range(3):
        again = invoke(desk.w.r, desk.key, "retry-1")
        assert again.status == 200 and replayed(again)
        assert again.body["inference_id"] == first.body["inference_id"]
        assert again.body["status"] == "SUCCEEDED"
        assert again.body["output"] == first.body["output"]  # the original answer, not re-run
        assert again.body["output_sha256"] == first.body["output_sha256"]
        assert again.body["usage"] == first.body["usage"]
        assert again.body["workflow_version"] == first.body["workflow_version"] == desk.v1
    assert desk.sent() == calls  # not one more provider request
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}
    assert desk.state() == ["SUCCEEDED"]


def test_canonically_equivalent_bodies_replay_the_same_invocation(desk):
    first = invoke(desk.w.r, desk.key, "canon-1")
    calls = desk.sent()
    reordered = json.dumps({"inputs": dict(reversed(list(NEW_ORDER.items())))}, indent=3)
    again = invoke(desk.w.r, desk.key, "canon-1", raw=reordered.encode())
    assert again.status == 200 and replayed(again)
    assert again.body["inference_id"] == first.body["inference_id"]
    assert desk.sent() == calls


def test_the_same_key_with_another_request_fails_closed(desk):
    first = invoke(desk.w.r, desk.key, "conflict-1")
    calls = desk.sent()
    e = err(invoke(desk.w.r, desk.key, "conflict-1", OTHER_ORDER), 409, "idempotency_conflict")
    assert first.body["inference_id"] not in json.dumps(e) and "conflict-1" not in json.dumps(e)
    other_lineage = invoke(desk.w.r, desk.key, "conflict-1", path="deployments/other.x/invoke")
    err(other_lineage, 409, "idempotency_conflict")  # the address is part of the request
    assert desk.sent() == calls and desk.counts()["inference_records"] == 1
    again = invoke(desk.w.r, desk.key, "conflict-1")  # the binding is unchanged
    assert replayed(again) and again.body["inference_id"] == first.body["inference_id"]
    # keys are scoped to the operation: the same literal key invoking a version is its own
    by_version = invoke(desk.w.r, desk.key, "conflict-1", path=f"workflows/{desk.v1}/invoke")
    assert by_version.status == 200 and not replayed(by_version)
    assert by_version.body["inference_id"] != first.body["inference_id"]
    assert by_version.body["addressed_by"] == "version"
    assert replayed(invoke(desk.w.r, desk.key, "conflict-1", path=f"workflows/{desk.v1}/invoke"))


def test_a_failed_invocation_replays_its_error_without_running_again(desk):
    desk.provider.status = 503  # the provider fails every request
    first = invoke(desk.w.r, desk.key, "fails-1")
    e = err(first, 502, "inference_failed")
    calls = desk.sent()
    assert calls > 0 and not replayed(first)
    desk.provider.status = 200  # even once the provider is back, the key keeps its outcome
    again = invoke(desk.w.r, desk.key, "fails-1")
    e2 = err(again, 502, "inference_failed")
    assert replayed(again) and desk.sent() == calls
    assert e2["details"]["inference_id"] == e["details"]["inference_id"]
    assert e2["details"]["failure_kind"] == e["details"]["failure_kind"]
    assert desk.state() == ["FAILED"] and desk.counts()["inference_records"] == 1


def test_a_rejected_request_is_bound_once_and_never_recorded_twice(desk):
    bad = {"question": "Which city?"}  # order_record is missing
    first = invoke(desk.w.r, desk.key, "reject-1", bad)
    e = err(first, 422, "invalid_inference_request")
    again = invoke(desk.w.r, desk.key, "reject-1", bad)
    e2 = err(again, 422, "invalid_inference_request")
    assert replayed(again) and not replayed(first)
    assert e2["details"] == e["details"] and e["details"]["field"] == "inputs.order_record"
    assert desk.sent() == 0
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}
    # the next request without a key is unaffected
    assert invoke(desk.w.r, desk.key, None, bad).status == 422
    assert desk.counts()["inference_records"] == 2


# == 8. malformed keys, quota boundaries, no key ================================================
def test_a_malformed_key_is_refused_before_anything_runs(desk):
    for bad in ("", " ", "a b", "-lead", "x" * 129, "kéy", "a,b"):
        e = err(invoke(desk.w.r, desk.key, bad), 400, "invalid_request")
        assert e["details"]["field"] == "Idempotency-Key"
        if bad.strip():
            assert bad not in e["message"]  # the key is never echoed
    assert desk.sent() == 0
    assert desk.counts() == {"inference_records": 0, "inference_invocations": 0}
    assert invoke(desk.w.r, desk.key, "x" * 128).status == 200  # the bound itself is accepted


def test_an_exhausted_workspace_quota_neither_blocks_nor_is_consumed_by_a_replay(desk, monkeypatch):
    monkeypatch.setenv("WYNK_QUOTA_WORKFLOW_VERSIONS", "2")  # the two published versions
    monkeypatch.setenv("WYNK_QUOTA_ACTIVE_JOBS", "1")
    desk.w.restart()
    first = invoke(desk.w.r, desk.key, "quota-1")
    again = invoke(desk.w.r, desk.key, "quota-1")
    assert first.status == again.status == 200 and replayed(again)
    assert desk.sent() == first.body["usage"]["model_calls"]
    usage = desk.w.ok(desk.key, "GET", "workspace/quota")
    assert usage["usage"]["workflow_versions"] == 2  # invocations and keys are no quota resource


# == 3. workspace isolation and authorization ===================================================
def test_one_literal_key_is_independent_in_another_workspace(desk):
    a = invoke(desk.w.r, desk.key, "shared-key")
    calls = desk.sent()
    ws_b, key_b = desk.w.workspace("Other", "other@example.test")
    b = invoke(desk.w.r, key_b, "shared-key")  # B has no deployment: A's invocation is unseen
    e = err(b, 404, "deployment_not_found")
    assert a.body["inference_id"] not in json.dumps(b.body) and not replayed(b)
    err(
        desk.w.r.handle(  # nor can B read A's inference
            "GET",
            f"/api/v1/inferences/{a.body['inference_id']}",
            {"Authorization": f"Bearer {key_b}"},
            io.BytesIO(b""),
        ),
        404,
        "inference_not_found",
    )
    assert "inference_id" not in e["details"] and desk.sent() == calls
    assert replayed(invoke(desk.w.r, desk.key, "shared-key"))  # A's binding is unchanged


def test_unauthorized_callers_never_see_or_create_an_invocation(desk):
    first = invoke(desk.w.r, desk.key, "auth-1")
    calls = desk.sent()
    member = desk.w.ok(desk.key, "POST", "workspace/members", {"email": "ro@example.test"}, 201)
    viewer = desk.w.ok(
        desk.key,
        "POST",
        "workspace/keys",
        {"name": "viewer", "scopes": ["read"], "user_id": member["user_id"]},
        201,
    )["secret"]
    for caller, status in ((viewer, 403), (None, 401), ("wynk_sk_" + "0" * 60, 401)):
        res = invoke(desk.w.r, caller, "auth-1")
        assert res.status == status and not replayed(res), res.body
        assert first.body["inference_id"] not in json.dumps(res.body)
    assert desk.sent() == calls and desk.counts()["inference_invocations"] == 1


# == 2. concurrency: threads and processes ======================================================
def test_concurrent_requests_with_one_key_run_one_invocation(desk):
    other = desk.router()  # a second server over the same data directory
    routers = [desk.w.r] * 4 + [other] * 4
    barrier = threading.Barrier(len(routers))
    out: list[ApiResponse] = []

    def go(r: Any) -> None:
        barrier.wait()
        out.append(invoke(r, desk.key, "thread-race"))

    threads = [threading.Thread(target=go, args=(r,)) for r in routers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    assert len(out) == len(routers)
    winners = [r for r in out if r.status == 200 and not replayed(r)]
    assert len(winners) == 1, [(r.status, r.body) for r in out]
    (winner,) = winners
    inference_id = winner.body["inference_id"]
    for r in out:
        if r is winner:
            continue
        assert replayed(r), r.body
        if r.status == 200:
            assert r.body["inference_id"] == inference_id
        else:
            e = err(r, 409, "inference_in_progress")
            assert e["details"]["inference_id"] == inference_id
    assert desk.sent() == winner.body["usage"]["model_calls"]  # exactly one execution
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


CHILD = """
import io, json, pathlib, sys, time
from tests.dogfood_support import product
data, registry, ws, key, path, ready, go = sys.argv[1:8]
r = product(pathlib.Path(data), pathlib.Path(registry))
r.tenant(ws)  # open the partition before the race
body = json.dumps({"inputs": json.loads(pathlib.Path(go).with_suffix(".json").read_text())})
raw = body.encode()
headers = {"Authorization": "Bearer " + key, "Idempotency-Key": "process-race",
           "Content-Type": "application/json", "Content-Length": str(len(raw))}
pathlib.Path(ready).write_text("ready")
while not pathlib.Path(go).exists():
    time.sleep(0.001)
res = r.handle("POST", "/api/v1/" + path, headers, io.BytesIO(raw))
print(json.dumps({"status": res.status, "body": res.body, "headers": dict(res.headers)}))
"""


def test_independent_processes_with_one_key_run_one_invocation(desk, tmp_path):
    go = tmp_path / "go"
    go.with_suffix(".json").write_text(json.dumps(NEW_ORDER))
    n = 4
    args = [str(desk.w.data), str(desk.w.registry), desk.ws, desk.key, PRODUCTION]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, *args, str(tmp_path / f"ready{i}"), str(go)],
            stdout=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
        )
        for i in range(n)
    ]
    try:
        deadline = time.monotonic() + 180
        while not all((tmp_path / f"ready{i}").exists() for i in range(n)):
            assert time.monotonic() < deadline and all(p.poll() is None for p in procs)
            time.sleep(0.01)
        go.write_text("go")  # every process sends at the same instant
        out = [json.loads(p.communicate(timeout=180)[0].strip().splitlines()[-1]) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    assert all(p.returncode == 0 for p in procs)
    winners = [o for o in out if o["status"] == 200 and "Idempotent-Replayed" not in o["headers"]]
    assert len(winners) == 1, out
    inference_id = winners[0]["body"]["inference_id"]
    for o in out:
        if o is winners[0]:
            continue
        assert o["headers"].get("Idempotent-Replayed") == "true", o
        if o["status"] == 200:
            assert o["body"]["inference_id"] == inference_id
        else:
            assert o["status"] == 409 and o["body"]["error"]["code"] == "inference_in_progress"
            assert o["body"]["error"]["details"]["inference_id"] == inference_id
    # the provider (in THIS process) saw exactly one invocation's requests
    assert desk.sent() == winners[0]["body"]["usage"]["model_calls"]
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}
    again = invoke(desk.w.r, desk.key, "process-race")
    assert replayed(again) and again.body["inference_id"] == inference_id


def test_racing_store_writers_bind_a_key_once(tmp_path):
    """The store alone: check + bind is one ``BEGIN IMMEDIATE`` transaction; the CAS moves
    let exactly one process start a reservation."""
    path = tmp_path / "deployments.sqlite3"
    SQLiteDeploymentStore(path, workspace_id="ws-race")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("INSERT INTO workflow_versions VALUES ('wv-1', 'l', 'c', '{}', 'h', '2026-01-01')")
    conn.close()
    claim = IdempotencyClaim("deployments.invoke", "k" * 64, "r" * 64)
    barrier = threading.Barrier(8)
    bound: list[Any] = []

    def go(i: int) -> None:
        store = SQLiteDeploymentStore(path, workspace_id="ws-race")  # its own connections
        barrier.wait()
        bound.append(
            store.bind_invocation(
                claim,
                inference_id=f"inf-{i}",
                addressed_by="production",
                version_id="wv-1",
                lineage_id="l",
                revision=2,
                owner=f"own-{i}",
                lease_expires_at=time.time() + 30,
            )
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(b is None for b in bound) == 1  # exactly one bound it
    store = SQLiteDeploymentStore(path, workspace_id="ws-race")
    row = store.invocation(claim.operation, claim.key_hash)
    assert row is not None
    assert {b.inference_id for b in bound if b is not None} == {row.inference_id}
    starts: list[bool] = []
    barrier = threading.Barrier(8)

    def start(owner: str) -> None:
        barrier.wait()
        starts.append(store.start_invocation(row.inference_id, owner, time.time() + 30))

    owners = [row.owner] * 4 + ["own-intruder"] * 4
    threads = [threading.Thread(target=start, args=(o,)) for o in owners]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert starts.count(True) == 1  # only the owner, and only once
    assert not store.take_over_invocation(
        row.inference_id, row.owner, "own-x", time.time(), 0, frozenset(VersionState)
    )


def test_the_database_enforces_the_invocation_state_machine(desk):
    invoke(desk.w.r, desk.key, "db-1")
    path = desk.deployments().store.path
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in (
            "UPDATE inference_invocations SET state='STARTED'",  # a finished one never moves
            "UPDATE inference_invocations SET lease_expires_at=0",
            "UPDATE inference_invocations SET request_hash='x'",
            "DELETE FROM inference_invocations",
            "INSERT INTO inference_invocations SELECT workspace_id, operation, 'k2', "
            "request_hash, 'inf-new', addressed_by, version_id, lineage_id, deployment_revision, "
            "'SUCCEEDED', owner, lease_expires_at, created_at, updated_at "
            "FROM inference_invocations",  # born finished, without its record
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    finally:
        conn.close()
    assert desk.state() == ["SUCCEEDED"]


# == 5. crashes before, during and after execution; 9. restarts =================================
def test_a_crash_before_any_provider_request_is_resumed_exactly_once(desk, monkeypatch):
    with monkeypatch.context() as m:

        async def dies(self, genome, task, **kw):  # compiled, about to run: nothing sent
            raise Crash

        m.setattr("runtime.runner.WorkflowRunner.run", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "crash-before")
    assert desk.sent() == 0 and desk.state() == ["RESERVED"]

    desk.w.restart()
    soon = invoke(desk.w.r, desk.key, "crash-before")  # the lease has not expired yet
    e = err(soon, 409, "inference_in_progress")
    assert replayed(soon) and e["details"]["state"] == "RESERVED"
    inference_id = e["details"]["inference_id"]

    desk.later()  # the dead owner's lease expired: it provably sent nothing
    resumed = invoke(desk.w.r, desk.key, "crash-before")
    assert resumed.status == 200 and replayed(resumed)
    assert resumed.body["inference_id"] == inference_id  # the original invocation, run once
    assert resumed.body["output"] == {"answer": "Lincoln"}
    assert desk.sent() == resumed.body["usage"]["model_calls"] > 0
    again = invoke(desk.w.r, desk.key, "crash-before")
    assert replayed(again) and again.body["inference_id"] == inference_id
    assert desk.sent() == resumed.body["usage"]["model_calls"]
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


def test_a_resumed_reservation_is_screened_by_the_policy_in_force_now(desk, monkeypatch):
    """F2: a key bound while the workspace was unscreened is checked again when it is resumed."""
    from api.tenancy import outbound_policy_command

    carded = {**NEW_ORDER, "question": "Card 4111 1111 1111 1111: where does ORD-1001 go?"}
    outbound_policy_command(desk.w.r, desk.ws, "off")
    with monkeypatch.context() as m:

        async def dies(self, genome, task, **kw):  # compiled, about to run: nothing sent
            raise Crash

        m.setattr("runtime.runner.WorkflowRunner.run", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "carded", inputs=carded)
    assert desk.sent() == 0 and desk.state() == ["RESERVED"]

    outbound_policy_command(desk.w.r, desk.ws, "screen")
    desk.w.restart()
    desk.later()  # the dead owner's lease expired: a resume would send the card now
    refused = invoke(desk.w.r, desk.key, "carded", inputs=carded)
    e = err(refused, 422, "outbound_data_blocked")
    assert "4111" not in json.dumps(refused.body)
    assert (e["details"]["categories"], e["details"]["fields"]) == ("payment_card", "question")
    assert desk.sent() == 0 and desk.state() == ["RESERVED"]  # nothing sent, still reserved
    assert desk.counts() == {"inference_records": 0, "inference_invocations": 1}


def test_a_crash_with_a_provider_request_in_flight_is_ambiguous_and_never_resent(desk, monkeypatch):
    from runtime.backends.openai_compatible import OpenAICompatibleClient

    real = OpenAICompatibleClient._post_once

    def dies_mid_call(self, payload):
        real(self, payload)  # the provider received (and may bill) this request
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(OpenAICompatibleClient, "_post_once", dies_mid_call)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "crash-during")
    sent = desk.sent()
    assert sent == 1 and desk.state() == ["STARTED"] and desk.counts()["inference_records"] == 0

    desk.w.restart()
    e = err(invoke(desk.w.r, desk.key, "crash-during"), 409, "inference_in_progress")
    assert e["details"]["state"] == "STARTED"
    desk.later()
    for _ in range(3):  # it stays ambiguous; it is never re-sent
        res = invoke(desk.w.r, desk.key, "crash-during")
        e = err(res, 409, "inference_ambiguous_attempt")
        assert replayed(res) and e["details"]["state"] == "AMBIGUOUS"
    assert desk.sent() == sent and desk.state() == ["AMBIGUOUS"]
    # the client decides to pay again: a new key is a new invocation
    fresh = invoke(desk.w.r, desk.key, "crash-during-2")
    assert fresh.status == 200 and not replayed(fresh)
    assert fresh.body["inference_id"] != e["details"]["inference_id"]
    assert desk.sent() == sent + fresh.body["usage"]["model_calls"]


def test_a_crash_before_the_outcome_is_stored_is_ambiguous(desk, monkeypatch):
    def dies(self, *a, **k):  # every provider request returned; the outcome is never stored
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(SQLiteDeploymentStore, "finish_invocation", dies)
        with pytest.raises(Crash):
            invoke(desk.w.r, desk.key, "crash-finish")
    sent = desk.sent()
    assert sent > 0 and desk.state() == ["STARTED"]
    desk.w.restart()
    desk.later()
    err(invoke(desk.w.r, desk.key, "crash-finish"), 409, "inference_ambiguous_attempt")
    assert desk.sent() == sent and desk.counts()["inference_records"] == 0


def test_a_crash_after_completion_replays_the_stored_result_after_a_restart(desk, monkeypatch):
    real = SQLiteDeploymentStore.finish_invocation

    def dies_after(self, *a, **k):
        real(self, *a, **k)  # committed: the response never reaches the client
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(SQLiteDeploymentStore, "finish_invocation", dies_after)
        with pytest.raises(Crash):
            invoke(desk.w.r, desk.key, "crash-after")
    sent = desk.sent()
    assert sent > 0 and desk.state() == ["SUCCEEDED"]
    desk.w.restart()
    desk.later()  # however late: a finished invocation is never re-run
    again = invoke(desk.w.r, desk.key, "crash-after")
    assert again.status == 200 and replayed(again) and again.body["status"] == "SUCCEEDED"
    assert desk.sent() == sent
    assert again.body["output"] == {"answer": "Lincoln"}
    record = desk.w.ok(desk.key, "GET", f"inferences/{again.body['inference_id']}")
    assert record == again.body | {"output": None}  # the record itself keeps the hash only


def test_a_restarted_server_keeps_every_binding(desk):
    first = invoke(desk.w.r, desk.key, "restart-1")
    calls = desk.sent()
    desk.w.restart()
    again = invoke(desk.w.r, desk.key, "restart-1")
    assert replayed(again) and again.body["inference_id"] == first.body["inference_id"]
    err(invoke(desk.w.r, desk.key, "restart-1", OTHER_ORDER), 409, "idempotency_conflict")
    assert desk.sent() == calls


# == 6. a running invocation, ambiguity and its safe resolution =================================
def test_a_running_invocation_reports_its_real_state_and_is_not_started_again(desk, monkeypatch):
    entered, release_ = blocked_provider(monkeypatch)
    t, out = in_thread(lambda: invoke(desk.w.r, desk.key, "running-1"))
    assert entered.wait(120)
    other = desk.router()
    for r in (desk.w.r, other):
        res = invoke(r, desk.key, "running-1")
        e = err(res, 409, "inference_in_progress")
        assert replayed(res) and e["details"]["state"] == "STARTED"
    assert len(desk.provider.requests) == 0  # held before it reached the provider
    release_.set()
    t.join(120)
    (first,) = out
    assert first.status == 200 and not replayed(first)
    again = invoke(other, desk.key, "running-1")
    assert replayed(again) and again.body["inference_id"] == first.body["inference_id"]
    assert desk.sent() == first.body["usage"]["model_calls"]


def test_an_ambiguous_invocation_is_resolved_only_by_its_measured_outcome(desk, monkeypatch):
    """A stalled owner (its lease looks expired to another process) is reported ambiguous; when
    it does finish, its measured outcome resolves the invocation - nothing is re-sent."""
    entered, release_ = blocked_provider(monkeypatch)
    t, out = in_thread(lambda: invoke(desk.w.r, desk.key, "late-1"))
    assert entered.wait(120)
    observer = desk.router()
    desk.later(observer)
    res = invoke(observer, desk.key, "late-1")
    e = err(res, 409, "inference_ambiguous_attempt")
    assert e["details"]["state"] == "AMBIGUOUS" and desk.state() == ["AMBIGUOUS"]
    release_.set()
    t.join(120)
    (first,) = out
    assert first.status == 200 and first.body["inference_id"] == e["details"]["inference_id"]
    assert desk.state() == ["SUCCEEDED"]
    again = invoke(observer, desk.key, "late-1")
    assert again.status == 200 and replayed(again) and again.body["status"] == "SUCCEEDED"
    assert desk.sent() == first.body["usage"]["model_calls"]  # one invocation, measured once
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


# == 4. deployment changes between requests =====================================================
def test_a_retry_never_runs_a_version_promoted_after_the_first_request(desk):
    first = invoke(desk.w.r, desk.key, "version-1")
    calls = desk.sent()
    assert first.body["workflow_version"] == desk.v1
    desk.deploy(desk.v2)
    again = invoke(desk.w.r, desk.key, "version-1")
    assert replayed(again) and again.body["workflow_version"] == desk.v1
    assert again.body["deployment_revision"] == first.body["deployment_revision"]
    assert desk.sent() == calls
    fresh = invoke(desk.w.r, desk.key, "version-2")  # a new key resolves production now
    assert fresh.body["workflow_version"] == desk.v2


def test_an_invocation_in_flight_during_a_promotion_records_its_bound_version(desk, monkeypatch):
    entered, release_ = blocked_provider(monkeypatch)
    t, out = in_thread(lambda: invoke(desk.w.r, desk.key, "flight-1"))
    assert entered.wait(120)
    other = desk.router()
    rev = desk.deployments(other).deployment(LINEAGE).revision
    desk.deployments(other).stage(desk.v2, rev)
    desk.deployments(other).promote(desk.v2, rev + 1)
    release_.set()
    t.join(120)
    (first,) = out
    assert first.status == 200 and first.body["workflow_version"] == desk.v1
    again = invoke(other, desk.key, "flight-1")
    assert replayed(again) and again.body["workflow_version"] == desk.v1


@pytest.mark.parametrize("change", ["promote", "rollback"])
def test_a_reserved_invocation_is_never_moved_to_another_version(desk, monkeypatch, change):
    if change == "rollback":  # v2 is production when the request arrives; v1 after
        desk.deploy(desk.v2)
    with monkeypatch.context() as m:

        async def dies(self, genome, task, **kw):
            raise Crash

        m.setattr("runtime.runner.WorkflowRunner.run", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "moved-1")
    bound = desk.deployments().store.invocation("deployments.invoke", _key_hash("moved-1"))
    desk.w.restart()
    if change == "promote":
        desk.deploy(desk.v2)
    else:
        rev = desk.w.ok(desk.key, "GET", f"deployments/{LINEAGE}")["revision"]
        desk.w.ok(desk.key, "POST", f"deployments/{LINEAGE}/rollback", {"expected_revision": rev})
    desk.later()
    for _ in range(2):
        res = invoke(desk.w.r, desk.key, "moved-1")
        e = err(res, 409, "inference_deployment_changed")
        assert replayed(res) and e["details"]["workflow_version"] == bound.version_id
        assert e["details"]["state"] == "ABANDONED"
    assert desk.sent() == 0 and desk.counts()["inference_records"] == 0


def test_a_reserved_invocation_resumes_after_an_unrelated_deployment_write(desk, monkeypatch):
    """Staging another version moves the revision, not production: the bound version still
    serves, so the resumed invocation runs it (and records the revision it was bound to)."""
    with monkeypatch.context() as m:

        async def dies(self, genome, task, **kw):
            raise Crash

        m.setattr("runtime.runner.WorkflowRunner.run", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "stage-1")
    revision = desk.w.ok(desk.key, "GET", f"deployments/{LINEAGE}")["revision"]
    desk.w.ok(desk.key, "POST", f"workflows/{desk.v2}/stage", {"expected_revision": revision})
    desk.later()
    res = invoke(desk.w.r, desk.key, "stage-1")
    assert res.status == 200 and replayed(res) and res.body["workflow_version"] == desk.v1
    assert res.body["deployment_revision"] == revision
    assert desk.sent() == res.body["usage"]["model_calls"]


# == 7. usage accounting and monitoring =========================================================
def test_retries_add_no_usage_cost_or_monitoring_record(desk):
    first = invoke(desk.w.r, desk.key, "usage-1")
    before = desk.w.ok(desk.key, "GET", f"workflows/{desk.v1}/monitoring")
    for _ in range(3):
        assert replayed(invoke(desk.w.r, desk.key, "usage-1"))
    after = desk.w.ok(desk.key, "GET", f"workflows/{desk.v1}/monitoring")
    assert {k: v for k, v in after.items() if k != "window"} == {
        k: v for k, v in before.items() if k != "window"
    }
    usage = first.body["usage"]
    assert usage["cost_authoritative"] and usage["cost"] > 0
    assert desk.sent() == usage["model_calls"] and desk.counts()["inference_records"] == 1
    assert after["inferences"]["count"] == 1 and after["evidence"]["inference_ids"] == [
        first.body["inference_id"]
    ]
    assert after["model_calls"]["total"] == usage["model_calls"]
    assert after["tokens"]["total_tokens"]["total"] == usage["total_tokens"]
    assert after["cost"]["total"] == pytest.approx(usage["cost"])


def _key_hash(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode("ascii")).hexdigest()


# == the websearch request is gated too ==========================================================
def _search_world(tmp_path: Path, shared_provider: FakeProvider) -> tuple[Any, str, str, Any]:
    """A workspace whose production champion performs one live websearch per invocation."""
    from tests.dogfood_support import FakeBrave
    from tests.test_dogfood_lifecycle import websearch_world

    shared_provider.requests.clear()
    shared_provider.status = 200
    brave = FakeBrave(extra={NEW_ORDER["question"]: NEW_ORDER["order_record"]})
    w = websearch_world(tmp_path, shared_provider, brave)
    ws, key = w.workspace("Search", "search@example.test")
    data = w.dataset(key)
    contract = {"workflow": {"allowed_sources": ["websearch"]}}
    body = w.experiment_body(data, ("fixed", "aco"), **contract)
    job = w.ok(key, "POST", "experiments", body, 201)
    w.run(ws, job["job_id"])
    promo = w.ok(key, "POST", f"experiments/{job['job_id']}/promote", status=201)
    champion = promo["record"]["champion"]["champion_id"]
    wv = w.ok(key, "POST", "workflows", {"champion_id": champion}, 201)
    assert wv["document"]["versions"].get("evidence_source") == w.source.identity  # it searches
    w.deploy(key, wv["version_id"])
    return w, ws, key, brave


def test_a_crash_during_a_live_search_is_ambiguous_and_the_search_is_never_resent(
    tmp_path, shared_provider
):
    from tests.dogfood_support import FakeBrave

    w, ws, key, brave = _search_world(tmp_path, shared_provider)
    searches, model_calls = len(brave.requests), len(shared_provider.requests)

    real = FakeBrave.__call__

    def dies(self, *a, **k):
        real(self, *a, **k)  # the search provider received (and may bill) the request
        raise Crash

    FakeBrave.__call__ = dies  # type: ignore[method-assign]
    try:
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(w.r, key, "search-1")
    finally:
        FakeBrave.__call__ = real  # type: ignore[method-assign]
    assert len(brave.requests) == searches + 1
    assert len(shared_provider.requests) == model_calls  # STARTED by the search, not a model call
    deployments = w.r.tenant(ws).api.deployments
    row = deployments.store.invocation("deployments.invoke", _key_hash("search-1"))
    assert row is not None and row.state == "STARTED"

    deployments.lease_clock = lambda: time.time() + 3600
    for _ in range(2):
        err(invoke(w.r, key, "search-1"), 409, "inference_ambiguous_attempt")
    assert len(brave.requests) == searches + 1 and len(shared_provider.requests) == model_calls
    fresh = invoke(w.r, key, "search-2")
    assert fresh.status == 200 and not replayed(fresh)
    assert len(brave.requests) == searches + 2  # the new invocation's one live search


def test_experiment_keys_bound_before_this_change_keep_their_request_hash():
    """The shared ``_claim`` helper hashes an experiment create exactly as #60 stored it."""
    from api.product import CREATE_EXPERIMENT, _claim
    from core.canonical import canonical_hash

    body = {"dataset_id": "d", "plan": {"seeds": [0]}}
    claim = _claim("k-1", CREATE_EXPERIMENT, {"request": body})
    assert claim is not None and claim.key_hash == _key_hash("k-1")
    assert claim.request_hash == canonical_hash({"operation": CREATE_EXPERIMENT, "request": body})
    assert _claim(None, CREATE_EXPERIMENT, {"request": body}) is None


# == review: helpers ===========================================================================
def crash_before_run(monkeypatch, r: Any, key: str, idem: str, path: str = PRODUCTION) -> None:
    """The serving process dies after the key is bound RESERVED, before any provider request."""
    with monkeypatch.context() as m:

        async def dies(self, genome, task, **kw):
            raise Crash

        m.setattr("runtime.runner.WorkflowRunner.run", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(r, key, idem, path=path)


def crash_mid_call(monkeypatch, r: Any, key: str, idem: str) -> None:
    """The serving process dies after the provider received its first request."""
    from runtime.backends.openai_compatible import OpenAICompatibleClient

    real = OpenAICompatibleClient._post_once

    def dies_mid_call(self, payload):
        real(self, payload)
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(OpenAICompatibleClient, "_post_once", dies_mid_call)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(r, key, idem)


def race(calls: list[Any]) -> list[ApiResponse]:
    """Run every call at the same instant, each in its own thread."""
    barrier = threading.Barrier(len(calls))
    out: list[Any] = [None] * len(calls)

    def go(i: int) -> None:
        barrier.wait()
        out[i] = calls[i]()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    assert all(o is not None for o in out)
    return out


def bound(desk: Desk, idem: str, operation: str = "deployments.invoke") -> Any:
    row = desk.deployments().store.invocation(operation, _key_hash(idem))
    assert row is not None
    return row


def outputs_table(desk: Desk) -> list[tuple[str, bytes]]:
    conn = sqlite3.connect(desk.deployments().store.path)
    try:
        rows = conn.execute("SELECT output_sha256, envelope FROM inference_outputs").fetchall()
    finally:
        conn.close()
    return [(sha, bytes(env)) for sha, env in rows]


# == review 1: a successful replay returns the original output ==================================
def test_a_successful_replay_returns_the_original_output_sealed_at_rest(desk):
    first = invoke(desk.w.r, desk.key, "out-1")
    assert first.status == 200 and first.body["output"] == {"answer": "Lincoln"}
    calls = desk.sent()
    desk.w.restart()
    for r in (desk.w.r, desk.router()):  # after a restart, from any server process
        again = invoke(r, desk.key, "out-1")
        assert again.status == 200 and replayed(again)
        assert again.body == first.body  # the very response, output included
    assert desk.sent() == calls  # and still no second provider request
    ((sha, envelope),) = outputs_table(desk)
    assert sha == first.body["output_sha256"] and envelope.startswith(b"WYNKBLOB")
    plain = canonical_json(first.body["output"]).encode()
    path = desk.deployments().store.path
    for f in path.parent.glob(path.name + "*"):  # the database, its WAL and shared memory
        assert plain not in f.read_bytes()
    # the inference record (and so monitoring) keeps the hash only, as before
    record = desk.w.ok(desk.key, "GET", f"inferences/{first.body['inference_id']}")
    assert record["output"] is None and record["output_sha256"] == sha
    assert invoke(desk.w.r, desk.key).status == 200  # a keyless invoke seals nothing
    assert len(outputs_table(desk)) == 1


def test_a_sealed_output_fails_closed_and_is_never_re_run(desk):
    first = invoke(desk.w.r, desk.key, "seal-1")
    calls = desk.sent()
    ((sha, envelope),) = outputs_table(desk)
    with pytest.raises(DecryptionError):  # sealed for this workspace only
        desk.w.r.keyring.cipher("ws-other").open(envelope, sha)
    conn = sqlite3.connect(desk.deployments().store.path, isolation_level=None)
    try:
        tampered = envelope[:-1] + bytes([envelope[-1] ^ 1])
        conn.execute("UPDATE inference_outputs SET envelope=?", (tampered,))
    finally:
        conn.close()
    res = invoke(desk.w.r, desk.key, "seal-1")
    e = err(res, 500, "storage_error")
    assert replayed(res) and e["details"]["inference_id"] == first.body["inference_id"]
    assert "Lincoln" not in json.dumps(res.body)
    assert desk.sent() == calls and desk.state() == ["SUCCEEDED"]


def test_key_rotation_re_seals_stored_outputs(desk, monkeypatch):
    from api.tenancy import secure_data
    from store.crypto import generate_key_entry

    first = invoke(desk.w.r, desk.key, "rotate-1")
    calls = desk.sent()
    new = generate_key_entry("k-rotated")
    monkeypatch.setenv("WYNK_DATA_KEYS", new)  # the old key retired too early
    desk.w.restart()
    err(invoke(desk.w.r, desk.key, "rotate-1"), 500, "storage_error")
    monkeypatch.setenv("WYNK_DATA_KEYS", f"{new},{TEST_DATA_KEYS}")  # rotation as documented
    desk.w.restart()
    assert secure_data(desk.w.r)[desk.ws]["inference_outputs_rekeyed"] == 1
    assert secure_data(desk.w.r)[desk.ws]["inference_outputs_rekeyed"] == 0  # idempotent
    monkeypatch.setenv("WYNK_DATA_KEYS", new)  # now the old key can go
    desk.w.restart()
    again = invoke(desk.w.r, desk.key, "rotate-1")
    assert again.status == 200 and again.body == first.body
    assert desk.sent() == calls


def test_without_an_output_keyring_a_keyed_invoke_binds_nothing(desk):
    desk.deployments().outputs = None
    err(invoke(desk.w.r, desk.key, "nokeys-1"), 503, "inference_backend_unavailable")
    assert desk.sent() == 0
    assert desk.counts() == {"inference_records": 0, "inference_invocations": 0}
    assert invoke(desk.w.r, desk.key).status == 200  # keyless invokes are unaffected


def test_the_database_refuses_a_success_without_its_sealed_output(desk, tmp_path):
    invoke(desk.w.r, desk.key, "db-out-1")
    conn = sqlite3.connect(desk.deployments().store.path, isolation_level=None)
    try:
        for statement in (
            "DELETE FROM inference_outputs",
            "UPDATE inference_outputs SET envelope=CAST('{\"answer\":1}' AS BLOB)",  # plaintext
            "UPDATE inference_outputs SET output_sha256='x'",
            "INSERT INTO inference_outputs SELECT inference_id || 'x', output_sha256, envelope, "
            "created_at FROM inference_outputs",  # no invocation finishes with it
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    finally:
        conn.close()
    store = SQLiteDeploymentStore(tmp_path / "d.sqlite3", workspace_id="ws-race")
    raw = sqlite3.connect(store.path, isolation_level=None)
    raw.execute("INSERT INTO workflow_versions VALUES ('wv-1', 'l', 'c', '{}', 'h', '2026-01-01')")
    raw.close()
    claim = IdempotencyClaim("deployments.invoke", "k" * 64, "r" * 64)
    lease = time.time() + 30
    kw = {"addressed_by": "production", "version_id": "wv-1", "lineage_id": "l", "revision": 1}
    assert (
        store.bind_invocation(claim, inference_id="inf-1", owner="o", lease_expires_at=lease, **kw)
        is None
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.finish_invocation("inf-1", "o", "SUCCEEDED", "{}")  # no sealed output
    row = store.invocation(claim.operation, claim.key_hash)
    assert row is not None and row.state is InvocationState.RESERVED
    assert store.inference("inf-1") is None  # the whole finish rolled back


# == review 2: STARTED is durable before any request leaves the process ========================
def test_started_is_committed_before_the_provider_receives_any_request(desk):
    seen: list[list[str]] = []
    desk.provider.on_request = lambda body: seen.append(desk.state())  # another connection
    first = invoke(desk.w.r, desk.key, "boundary-1")
    assert first.status == 200
    assert len(seen) == desk.sent() == first.body["usage"]["model_calls"] > 0
    assert all(s == ["STARTED"] for s in seen), seen  # committed, as seen at the far end
    assert desk.state() == ["SUCCEEDED"]


def test_started_is_committed_before_the_search_provider_receives_a_request(
    tmp_path, shared_provider
):
    from tests.dogfood_support import FakeBrave

    w, ws, key, brave = _search_world(tmp_path, shared_provider)
    store = w.r.tenant(ws).api.deployments.store
    seen: list[Any] = []
    real = FakeBrave.__call__

    def observed(self, *a, **k):
        row = store.invocation("deployments.invoke", _key_hash("search-b"))  # a new connection
        seen.append(None if row is None else row.state.value)
        return real(self, *a, **k)

    FakeBrave.__call__ = observed  # type: ignore[method-assign]
    try:
        res = invoke(w.r, key, "search-b")
    finally:
        FakeBrave.__call__ = real  # type: ignore[method-assign]
    assert res.status == 200 and seen == ["STARTED"]


def test_a_crash_inside_the_start_commit_sends_nothing_and_is_resumed_once(desk, monkeypatch):
    def dies(self, *a, **k):  # the process dies before RESERVED -> STARTED commits
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(SQLiteDeploymentStore, "start_invocation", dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "start-crash")
    assert desk.sent() == 0 and desk.state() == ["RESERVED"]
    desk.w.restart()
    desk.later()
    res = invoke(desk.w.r, desk.key, "start-crash")
    assert res.status == 200 and replayed(res)
    assert res.body["inference_id"] == bound(desk, "start-crash").inference_id
    assert desk.sent() == res.body["usage"]["model_calls"]


def test_a_crash_between_the_start_commit_and_the_request_is_ambiguous(desk, monkeypatch):
    """STARTED committed, the process died before the request left: nothing was sent, but no
    process can prove that - it is ambiguous (the safe side), never re-sent."""
    real = SQLiteDeploymentStore.start_invocation

    def commits_then_dies(self, *a, **k):
        assert real(self, *a, **k)
        raise Crash

    with monkeypatch.context() as m:
        m.setattr(SQLiteDeploymentStore, "start_invocation", commits_then_dies)
        with pytest.raises((Crash, BaseExceptionGroup)):
            invoke(desk.w.r, desk.key, "start-then-crash")
    assert desk.sent() == 0 and desk.state() == ["STARTED"]
    desk.w.restart()
    desk.later()
    for _ in range(2):
        err(invoke(desk.w.r, desk.key, "start-then-crash"), 409, "inference_ambiguous_attempt")
    assert desk.sent() == 0 and desk.state() == ["AMBIGUOUS"]


def test_a_failed_start_commit_sends_nothing(desk, monkeypatch):
    def refused(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as m:
        m.setattr(SQLiteDeploymentStore, "start_invocation", refused)
        first = invoke(desk.w.r, desk.key, "start-error")
    e = err(first, 502, "inference_failed")
    assert desk.sent() == 0 and desk.state() == ["FAILED"]  # finished without any request
    again = invoke(desk.w.r, desk.key, "start-error")
    assert err(again, 502, "inference_failed")["details"] == e["details"] and replayed(again)
    assert desk.sent() == 0


def test_concurrent_retries_recover_a_dead_reservation_exactly_once(desk, monkeypatch):
    crash_before_run(monkeypatch, desk.w.r, desk.key, "recover-1")
    inference_id = bound(desk, "recover-1").inference_id
    routers = [desk.w.r, desk.router()]
    for r in routers:
        desk.later(r)  # both server processes see the dead owner's lease expired
    out = race([lambda r=r: invoke(r, desk.key, "recover-1") for r in routers * 4])
    done = [o for o in out if o.status == 200]
    assert done and all(replayed(o) for o in out)
    assert {o.body["inference_id"] for o in done} == {inference_id}
    for o in out:
        if o.status != 200:
            assert err(o, 409, "inference_in_progress")["details"]["inference_id"] == inference_id
    assert desk.sent() == done[0].body["usage"]["model_calls"]  # one takeover, one run
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


def test_concurrent_retries_of_a_dead_started_invocation_never_resend(desk, monkeypatch):
    crash_mid_call(monkeypatch, desk.w.r, desk.key, "recover-2")
    sent = desk.sent()
    routers = [desk.w.r, desk.router()]
    for r in routers:
        desk.later(r)
    out = race([lambda r=r: invoke(r, desk.key, "recover-2") for r in routers * 4])
    for o in out:
        assert replayed(o)
        assert err(o, 409, "inference_ambiguous_attempt")["details"]["state"] == "AMBIGUOUS"
    assert desk.sent() == sent == 1 and desk.state() == ["AMBIGUOUS"]


def test_a_stalled_owner_whose_reservation_was_taken_over_sends_nothing(desk, monkeypatch):
    from runtime.runner import WorkflowRunner

    real = WorkflowRunner.run
    stalled, go = threading.Event(), threading.Event()
    first = [True]
    lock = threading.Lock()

    async def run(self, *a, **k):
        with lock:
            stall, first[0] = first[0], False
        if stall:  # the first owner stalls after binding RESERVED, before any request
            stalled.set()
            assert go.wait(120)
        return await real(self, *a, **k)

    monkeypatch.setattr(WorkflowRunner, "run", run)
    t, out = in_thread(lambda: invoke(desk.w.r, desk.key, "stall-1"))
    assert stalled.wait(120)
    assert desk.state() == ["RESERVED"] and desk.sent() == 0
    observer = desk.router()
    desk.later(observer)  # to another process the stalled owner looks dead
    resumed = invoke(observer, desk.key, "stall-1")
    assert resumed.status == 200 and replayed(resumed)
    calls = desk.sent()
    assert calls == resumed.body["usage"]["model_calls"]
    go.set()
    t.join(120)
    (late,) = out
    assert late.status == 200 and replayed(late)  # it reports the new owner's outcome
    assert late.body == resumed.body
    assert desk.sent() == calls  # its RESERVED -> STARTED was fenced: it sent nothing
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


# == review 3: deployment changes ==============================================================
def _change(desk: Desk, change: str) -> None:
    rev = desk.w.ok(desk.key, "GET", f"deployments/{LINEAGE}")["revision"]
    if change == "replace":
        desk.deploy(desk.v2)
    elif change == "rollback":
        desk.w.ok(desk.key, "POST", f"deployments/{LINEAGE}/rollback", {"expected_revision": rev})
    else:  # promote the staged v2
        desk.w.ok(desk.key, "POST", f"workflows/{desk.v2}/promote", {"expected_revision": rev})


@pytest.mark.parametrize("change", ["replace", "rollback", "promote"])
def test_finished_invocations_replay_unchanged_after_a_deployment_change(desk, change):
    if change == "rollback":  # v2 serves now; the rollback retires it
        desk.deploy(desk.v2)
    if change == "promote":  # v2 is staged now; the promotion retires v1
        rev = desk.w.ok(desk.key, "GET", f"deployments/{LINEAGE}")["revision"]
        desk.w.ok(desk.key, "POST", f"workflows/{desk.v2}/stage", {"expected_revision": rev})
    served = desk.v2 if change in ("rollback", "promote") else desk.v1
    bad = {"question": "Which city?"}
    first: dict[tuple[str, str], ApiResponse] = {}
    for path in (PRODUCTION, f"workflows/{served}/invoke"):
        first[(path, "ok")] = invoke(desk.w.r, desk.key, "fin-ok", path=path)
        first[(path, "bad")] = invoke(desk.w.r, desk.key, "fin-bad", bad, path=path)
        desk.provider.status = 503
        first[(path, "fail")] = invoke(desk.w.r, desk.key, "fin-fail", path=path)
        desk.provider.status = 200
    assert [r.status for r in first.values()] == [200, 422, 502] * 2
    calls = desk.sent()
    _change(desk, change)
    for (path, kind), was in first.items():
        inputs = bad if kind == "bad" else NEW_ORDER
        again = invoke(desk.w.r, desk.key, f"fin-{kind}", inputs, path=path)
        assert again.status == was.status and replayed(again), (path, kind, again.body)
        assert again.body == was.body  # the same output / the same error, same inference id
    assert desk.sent() == calls


def test_a_reserved_invocation_by_version_is_abandoned_once_its_version_retires(desk, monkeypatch):
    path = f"workflows/{desk.v1}/invoke"
    crash_before_run(monkeypatch, desk.w.r, desk.key, "ver-res", path=path)
    desk.deploy(desk.v2)  # v1 is RETIRED
    desk.later()
    for _ in range(2):
        e = err(
            invoke(desk.w.r, desk.key, "ver-res", path=path), 409, "inference_deployment_changed"
        )
        assert e["details"]["workflow_version"] == desk.v1
    assert desk.sent() == 0 and desk.state() == ["ABANDONED"]


def test_a_reserved_staged_invocation_resumes_on_its_version_after_its_promotion(desk, monkeypatch):
    rev = desk.w.ok(desk.key, "GET", f"deployments/{LINEAGE}")["revision"]
    desk.w.ok(desk.key, "POST", f"workflows/{desk.v2}/stage", {"expected_revision": rev})
    path = f"workflows/{desk.v2}/invoke"
    crash_before_run(monkeypatch, desk.w.r, desk.key, "staged-res", path=path)
    desk.w.ok(desk.key, "POST", f"workflows/{desk.v2}/promote", {"expected_revision": rev + 1})
    desk.later()
    res = invoke(desk.w.r, desk.key, "staged-res", path=path)
    assert res.status == 200 and replayed(res)  # v2 still serves its own address
    assert res.body["workflow_version"] == desk.v2 and res.body["addressed_by"] == "version"
    assert desk.sent() == res.body["usage"]["model_calls"]


def test_a_promotion_racing_a_takeover_never_runs_the_retired_version(desk, monkeypatch):
    crash_before_run(monkeypatch, desk.w.r, desk.key, "race-res")
    real = SQLiteDeploymentStore.take_over_invocation
    once = [True]

    def promote_first(self, *a, **k):
        if once[0]:  # the promotion lands after the retry checked v1, before its takeover
            once[0] = False
            desk.deploy(desk.v2)
        return real(self, *a, **k)

    monkeypatch.setattr(SQLiteDeploymentStore, "take_over_invocation", promote_first)
    desk.later()
    e = err(invoke(desk.w.r, desk.key, "race-res"), 409, "inference_deployment_changed")
    assert e["details"]["workflow_version"] == desk.v1 and not once[0]
    assert desk.sent() == 0 and desk.state() == ["ABANDONED"]
    assert desk.counts()["inference_records"] == 0


# == review 4: concurrent replays and conflicts ================================================
def test_concurrent_retries_of_a_running_invocation_report_it_and_start_nothing(desk, monkeypatch):
    entered, release_ = blocked_provider(monkeypatch)
    t, out = in_thread(lambda: invoke(desk.w.r, desk.key, "busy-1"))
    assert entered.wait(120)
    inference_id = bound(desk, "busy-1").inference_id
    routers = [desk.w.r, desk.router()]
    same = [lambda r=r: invoke(r, desk.key, "busy-1") for r in routers * 3]
    other = [lambda r=r: invoke(r, desk.key, "busy-1", OTHER_ORDER) for r in routers]
    res = race(same + other)
    for o in res[: len(same)]:
        e = err(o, 409, "inference_in_progress")
        assert replayed(o) and e["details"] == {
            "inference_id": inference_id,
            "workflow_version": desk.v1,
            "state": "STARTED",
        }
    for o in res[len(same) :]:
        err(o, 409, "idempotency_conflict")
    assert len(desk.provider.requests) == 0 and desk.state() == ["STARTED"]
    release_.set()
    t.join(120)
    (first,) = out
    assert first.status == 200 and not replayed(first)
    assert desk.sent() == first.body["usage"]["model_calls"]


def test_racing_requests_with_different_payloads_bind_one_and_refuse_the_rest(desk):
    routers = [desk.w.r, desk.router()]
    payloads = [NEW_ORDER, OTHER_ORDER] * 4
    out = race(
        [lambda i=i: invoke(routers[i % 2], desk.key, "mixed-1", payloads[i]) for i in range(8)]
    )
    winners = [i for i, o in enumerate(out) if o.status == 200 and not replayed(o)]
    assert len(winners) == 1, [(o.status, o.body) for o in out]
    (w,) = winners
    for i, o in enumerate(out):
        if i == w:
            continue
        if payloads[i] != payloads[w]:
            err(o, 409, "idempotency_conflict")
        elif o.status == 200:
            assert replayed(o) and o.body == out[w].body
        else:
            assert replayed(o) and err(o, 409, "inference_in_progress")
    assert desk.sent() == out[w].body["usage"]["model_calls"]
    assert desk.counts() == {"inference_records": 1, "inference_invocations": 1}


def test_a_conflicting_payload_is_refused_in_every_state_and_moves_nothing(desk, monkeypatch):
    invoke(desk.w.r, desk.key, "c-ok")
    invoke(desk.w.r, desk.key, "c-bad", {"question": "Which city?"})
    desk.provider.status = 503
    invoke(desk.w.r, desk.key, "c-fail")
    desk.provider.status = 200
    crash_before_run(monkeypatch, desk.w.r, desk.key, "c-res")
    crash_mid_call(monkeypatch, desk.w.r, desk.key, "c-amb")
    desk.w.restart()
    desk.later()  # the dead owners' leases look expired: a conflict must still move nothing

    def refused_everywhere() -> None:
        before = (desk.sent(), desk.state())
        for idem in ("c-ok", "c-bad", "c-fail", "c-res", "c-amb"):
            res = invoke(desk.w.r, desk.key, idem, OTHER_ORDER)
            e = err(res, 409, "idempotency_conflict")
            assert e.get("details", {}).get("inference_id") is None  # nothing about the owner
        assert (desk.sent(), desk.state()) == before

    refused_everywhere()
    assert desk.state() == ["SUCCEEDED", "FAILED", "FAILED", "RESERVED", "STARTED"]
    desk.deploy(desk.v2)  # the reserved one can never run now; the started one is ambiguous
    err(invoke(desk.w.r, desk.key, "c-res"), 409, "inference_deployment_changed")
    err(invoke(desk.w.r, desk.key, "c-amb"), 409, "inference_ambiguous_attempt")
    refused_everywhere()
    assert desk.state() == ["SUCCEEDED", "FAILED", "FAILED", "ABANDONED", "AMBIGUOUS"]
