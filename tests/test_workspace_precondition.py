"""The ``Wynk-Expected-Workspace`` precondition (``api.tenancy``).

The browser reads ``GET /workspace`` and then sends a mutation. The UI server's credential can
change between the two requests (a key rotation, another tenant's key). The client therefore
names the workspace it looked up; the server refuses the request with ``workspace_mismatch``
(412) when the authenticated key belongs to another workspace, before the scope check, the
body, any idempotency claim, any write or any provider request.

Every test drives the real multi-tenant router with TWO workspaces that both serve the same
lineage (so a request moved to the wrong workspace WOULD run there and spend). "Nothing ran" is
the fake provider's count of real HTTP requests plus the workspaces' own rows.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import threading
from typing import Any

import pytest

from api.product import _ROUTES
from api.tenancy import _ADMIN_ROUTES, EXPECTED_WORKSPACE
from tests.conftest import TEST_DATA_KEYS
from tests.dogfood_support import LINEAGE, FakeProvider
from tests.test_dogfood_lifecycle import NEW_ORDER, World, release

PRODUCTION = f"deployments/{LINEAGE}/invoke"
HEADER = "Wynk-Expected-Workspace"


@pytest.fixture(scope="module")
def shared_provider():
    p = FakeProvider().start()
    yield p
    p.stop()


@pytest.fixture(scope="module")
def template(tmp_path_factory, shared_provider) -> dict[str, Any]:
    """Workspaces A and B, each with the lineage deployed to production."""
    root = tmp_path_factory.mktemp("template")
    with pytest.MonkeyPatch.context() as m:
        m.delenv("WYNK_DATA_KEYS_FILE", raising=False)
        m.setenv("WYNK_DATA_KEYS", TEST_DATA_KEYS)
        w = World(root, shared_provider)
        out: dict[str, Any] = {"root": root}
        for name in ("A", "B"):
            ws, key = w.workspace(f"Desk {name}", f"{name.lower()}@example.test")
            data = w.dataset(key)
            w.deploy(key, release(w, ws, key, data)["version"])
            out[name] = {"ws": ws, "key": key, "data": data}
    assert out["A"]["ws"] != out["B"]["ws"]
    return out


class Two:
    def __init__(self, w: World, t: dict[str, Any]) -> None:
        self.w, self.A, self.B = w, t["A"], t["B"]
        self.experiment = w.experiment_body(t["A"]["data"])

    def sent(self) -> int:
        return len(self.w.provider.requests)

    def rows(self, side: dict[str, Any], db: str, table: str) -> int:
        conn = sqlite3.connect(self.w.data / "workspaces" / side["ws"] / db)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def counts(self, side: dict[str, Any]) -> dict[str, int]:
        return {
            "inference_records": self.rows(side, "deployments.sqlite3", "inference_records"),
            "inference_invocations": self.rows(
                side, "deployments.sqlite3", "inference_invocations"
            ),
            "jobs": self.rows(side, "jobs.sqlite3", "experiment_jobs"),
        }

    def post(
        self, key: str, path: str, body: Any, expected: str | None = None, idem: str | None = None
    ):
        raw = json.dumps(body).encode()
        h = {
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "Authorization": f"Bearer {key}",
        }
        if expected is not None:
            h[HEADER] = expected
        if idem is not None:
            h["Idempotency-Key"] = idem
        return self.w.r.handle("POST", "/api/v1/" + path, h, io.BytesIO(raw))

    def audit(self, side: dict[str, Any]) -> list[dict[str, Any]]:
        return self.w.ok(side["key"], "GET", "audit?limit=500")["events"]


@pytest.fixture
def two(tmp_path, template, shared_provider) -> Two:
    shutil.copytree(template["root"], tmp_path / "w")
    shared_provider.requests.clear()
    shared_provider.status = 200
    shared_provider.on_request = None
    return Two(World(tmp_path / "w", shared_provider), template)


def err(res, status: int, code: str) -> dict[str, Any]:
    assert (res.status, res.body.get("error", {}).get("code")) == (status, code), res.body
    return res.body["error"]


# -- the race: the key changed between the lookup and the mutation ----------------------------
def test_an_inference_sent_with_the_other_workspaces_key_runs_nowhere(two):
    before = {"A": two.counts(two.A), "B": two.counts(two.B)}
    sent = two.sent()
    # the browser looked up A; the UI server's key is now B's
    res = two.post(
        two.B["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=two.A["ws"], idem="race-1"
    )
    e = err(res, 412, "workspace_mismatch")
    assert e["details"] == {"field": HEADER}
    assert two.A["ws"] not in json.dumps(res.body) and two.B["ws"] not in json.dumps(res.body)
    assert two.sent() == sent  # zero provider requests: zero spend
    assert {"A": two.counts(two.A), "B": two.counts(two.B)} == before  # zero writes anywhere
    # the key was never bound in B: B's own request with that key runs fresh, once
    ok = two.post(
        two.B["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=two.B["ws"], idem="race-1"
    )
    assert ok.status == 200 and dict(ok.headers).get("Idempotent-Replayed") is None, ok.body
    assert two.counts(two.B)["inference_invocations"] == before["B"]["inference_invocations"] + 1


def test_an_experiment_sent_with_the_other_workspaces_key_is_created_nowhere(two):
    before = {"A": two.counts(two.A), "B": two.counts(two.B)}
    sent = two.sent()
    res = two.post(
        two.B["key"], "experiments", two.experiment, expected=two.A["ws"], idem="exp-race"
    )
    err(res, 412, "workspace_mismatch")
    assert two.sent() == sent
    assert {"A": two.counts(two.A), "B": two.counts(two.B)} == before
    # the same request is created where the precondition holds: it was the only refusal
    created = two.post(
        two.A["key"], "experiments", two.experiment, expected=two.A["ws"], idem="exp-race"
    )
    assert created.status == 201, created.body
    assert two.counts(two.A)["jobs"] == before["A"]["jobs"] + 1 and two.counts(two.B) == before["B"]


def test_a_matching_or_absent_precondition_changes_nothing(two):
    for expected in (two.A["ws"], None):
        sent = two.sent()
        res = two.post(two.A["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=expected)
        assert res.status == 200, res.body
        assert two.sent() > sent


def test_the_precondition_is_never_authorization(two):
    """Naming a workspace never selects or grants it: B's key with B's name sees only B."""
    a = two.post(two.A["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=two.A["ws"])
    inference = a.body["inference_id"]
    res = two.w.r.handle(
        "GET",
        f"/api/v1/inferences/{inference}",
        {"Authorization": f"Bearer {two.B['key']}", HEADER: two.B["ws"]},
        io.BytesIO(b""),
    )
    err(res, 404, "inference_not_found")
    # and A's name with B's key is refused, not served from A
    res = two.w.r.handle(
        "GET",
        f"/api/v1/inferences/{inference}",
        {"Authorization": f"Bearer {two.B['key']}", HEADER: two.A["ws"]},
        io.BytesIO(b""),
    )
    err(res, 412, "workspace_mismatch")
    # authentication still comes first: no key, any name -> 401
    res = two.w.r.handle("POST", "/api/v1/" + PRODUCTION, {HEADER: two.A["ws"]}, io.BytesIO(b"{}"))
    err(res, 401, "unauthenticated")


@pytest.mark.parametrize("value", ["", "ws-", "WS-ALPHA", "ws-a b", "ws-alpha,ws-beta", "x" * 70])
def test_a_malformed_precondition_is_refused_before_anything_runs(two, value):
    before, sent = two.counts(two.A), two.sent()
    res = two.post(two.A["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=value)
    err(res, 400, "invalid_request")
    assert two.sent() == sent and two.counts(two.A) == before


def every_mutation() -> list[tuple[str, str]]:
    """A concrete path for every state-changing product and admin route (each capture group
    becomes a placeholder id: the request must be refused before any route looks at it)."""
    out = []
    for method, pattern, _ in [*_ROUTES, *_ADMIN_ROUTES]:
        if method != "GET":
            out.append((method, re.sub(r"\([^()]*\)", "x1", pattern.pattern.strip("^$"))))
    return out


@pytest.mark.parametrize(("method", "path"), every_mutation())
def test_every_mutation_route_refuses_a_mismatch_first(two, method, path):
    before = {"A": two.counts(two.A), "B": two.counts(two.B)}
    sent = two.sent()
    res = two.w.r.handle(
        method,
        "/api/v1/" + path,
        {"Authorization": f"Bearer {two.B['key']}", HEADER: two.A["ws"], "Content-Length": "2"},
        io.BytesIO(b"{}"),
    )
    err(res, 412, "workspace_mismatch")
    assert two.sent() == sent and {"A": two.counts(two.A), "B": two.counts(two.B)} == before


def test_a_mismatch_is_recorded_in_the_keys_own_trail_without_secrets(two):
    two.post(two.B["key"], PRODUCTION, {"inputs": NEW_ORDER}, expected=two.A["ws"], idem="audit-1")
    events = [e for e in two.audit(two.B) if e["action"] == "security.workspace_mismatch"]
    assert len(events) == 1 and events[0]["result"] == "refused"
    assert (
        events[0]["code"] == "workspace_mismatch"
        and events[0]["resource_id"] == "invoke_production"
    )
    blob = json.dumps(events)
    assert two.B["key"] not in blob and "audit-1" not in blob and two.A["ws"] not in blob
    assert not [e for e in two.audit(two.A) if e["action"] == "security.workspace_mismatch"]


def test_concurrent_key_switches_never_run_in_the_other_workspace(two):
    """Requests that all looked up A, half of them sent with B's key: only A runs them."""
    before = {"A": two.counts(two.A), "B": two.counts(two.B)}
    results: list[Any] = [None] * 8
    start = threading.Barrier(8)

    def go(i: int) -> None:
        key = two.A["key"] if i % 2 == 0 else two.B["key"]
        start.wait()
        results[i] = two.post(
            key,
            PRODUCTION,
            {"inputs": {**NEW_ORDER, "question": f"{NEW_ORDER['question']} #{i}"}},
            expected=two.A["ws"],
            idem=f"c-{i}",
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.status for r in results] == [200, 412] * 4, [r.body for r in results]
    assert two.counts(two.B) == before["B"]
    assert two.counts(two.A)["inference_invocations"] == before["A"]["inference_invocations"] + 4


def test_the_header_name_is_the_documented_one():
    assert EXPECTED_WORKSPACE == HEADER.lower()
