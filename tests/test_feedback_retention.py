"""Phase 5 feedback retention and erasure, through the real multi-tenant wiring.

Erasure destroys a feedback's sealed payload (inputs, output, expected, caller metadata) and
keeps its minimal evidence (ids, payload hash, evaluation, an immutable erasure record). It is
an OWNER's (``admin``) action, audited, idempotent, workspace-isolated and fail-closed: a frozen
example whose re-optimization is pending, a dependent that cannot be checked, or copies the
erasure cannot reach refuse it unless residual copies are accepted explicitly. Retention is OFF
by default (keep) and never accepts residual copies.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from api.tenancy import (
    FEEDBACK_RETENTION_ENV,
    erase_feedback,
    feedback_retention_days_from_env,
    purge_feedback,
    secure_data,
)
from experiments.monitoring import FeedbackInUse
from store.crypto import Keyring
from store.monitoring import ERASED, ReoptState, SQLiteMonitoringStore
from tests.conftest import TEST_DATA_KEYS
from tests.test_champion_inference import maf
from tests.test_feedback_sealing import ROTATED, monitoring_db, rows, summary
from tests.test_outbound_policy import Counting, counting_router, serve_world
from tests.test_production_monitoring import inputs_for
from tests.test_tenancy import LINEAGE, call, code, router, workspace

MARK = "retention-marker-Q4"


@pytest.fixture
def served(tmp_path):
    return serve_world(tmp_path)


def fresh_feedback(r, key, *, idempotency: str | None = None, actor: str = MARK) -> str:
    """One feedback that no re-optimization froze (labelled after ``deploy`` triggered)."""
    inputs = inputs_for("France", "Paris")
    headers = {"Idempotency-Key": idempotency} if idempotency else {}
    s, inf = call(r, key, "POST", f"deployments/{LINEAGE}/invoke", {"inputs": inputs}, **headers)
    assert s == 200, inf
    s, fb = call(
        r,
        key,
        "POST",
        f"inferences/{inf['inference_id']}/feedback",
        {
            "inputs": inputs,
            "output": inf["output"],
            "expected": {"answer": "Paris"},
            "actor": actor,
        },
    )
    assert s == 201, fb
    return str(fb["feedback_id"])


def erase(r, key, fid, **body):
    return call(r, key, "POST", f"feedback/{fid}/erase", body)


def events(r, key, action):
    s, body = call(r, key, "GET", "audit")
    assert s == 200, body
    return [e for e in body["events"] if e["action"] == action]


@maf
def test_an_owner_erases_a_payload_once_and_the_minimal_evidence_stays(served):
    r, ws, key, a, model = served
    fid = fresh_feedback(r, key)
    before = summary(r, key, a["version"])[1]["feedback"]["labelled"]
    s, view = erase(r, key, fid)
    assert s == 201, view
    assert view["created"] and view["fully_erased"] and view["residual_copies"] == []
    assert view["erased"] == ["inputs", "output", "expected", "untrusted_metadata"]
    assert set(view["kept"]) >= {"feedback_id", "inference_id", "payload_sha256", "evaluation"}
    assert view["actor"]["user_id"] == call(r, key, "GET", "workspace")[1]["principal"]["user_id"]
    db = monitoring_db(r.root, ws)
    ((envelope, sha),) = rows(
        db, "SELECT envelope, payload_sha256 FROM feedback_payloads WHERE feedback_id=?", fid
    )
    assert bytes(envelope) == ERASED and sha == view["payload_sha256"]
    # the record reads as erased: ids, hashes, evaluation - no caller value, no metadata
    s, got = call(r, key, "GET", f"feedback/{fid}")
    assert s == 200 and got["payload_erased"] is True and got["untrusted_metadata"] == {}, got
    assert got["evaluation"] is not None and MARK not in json.dumps(got)
    # aggregates skip it (its values are gone), everything else still opens
    assert summary(r, key, a["version"])[1]["feedback"]["labelled"] == before - 1
    # idempotent: the stored erasure, unchanged
    s, again = erase(r, key, fid)
    assert s == 200 and again == view | {"created": False}, again
    audited = events(r, key, "feedback.erase")
    assert [e["result"] for e in audited] == ["requested", "ok", "requested", "ok"]
    assert audited[1]["details"]["fully_erased"] is True
    assert audited[1]["details"]["payload_sha256"] == view["payload_sha256"]


@maf
def test_only_an_owner_can_erase_and_another_workspace_cannot_even_see_it(served):
    r, ws, key, a, model = served
    fid = fresh_feedback(r, key)
    s, member = call(r, key, "POST", "workspace/members", {"email": "m@alpha.test"})
    assert s == 201, member
    body = {"name": "m", "scopes": ["read", "write"], "user_id": member["user_id"]}
    s, mk = call(r, key, "POST", "workspace/keys", body)
    assert s == 201, mk
    assert code(erase(r, mk["secret"], fid)) == (403, "insufficient_scope")
    other, other_key = workspace(r, "Beta", "b@beta.test")
    foreign = erase(r, other_key, fid)
    missing = erase(r, other_key, "fb-" + "0" * 24)
    assert foreign[0] == missing[0] == 404
    assert json.dumps(foreign[1]).replace(fid, "ID") == json.dumps(missing[1]).replace(
        "fb-" + "0" * 24, "ID"
    )
    db = monitoring_db(r.root, ws)
    assert (
        bytes(rows(db, "SELECT envelope FROM feedback_payloads WHERE feedback_id=?", fid)[0][0])
        != ERASED
    )


@maf
def test_frozen_feedback_with_copies_is_refused_unless_residual_copies_are_accepted(served):
    r, ws, key, a, model = served
    fid = a["feedback"][0]  # frozen into the challenger dataset; its job exists
    s, body = erase(r, key, fid)
    assert (s, body["error"]["code"]) == (409, "feedback_in_use"), body
    assert body["error"]["details"]["reason"] == "residual_copies"
    assert set(body["error"]["details"]["copies"].split(",")) == {
        "challenger_job",
        "dataset_version",
    }
    db = monitoring_db(r.root, ws)
    assert rows(db, "SELECT count(*) FROM feedback_erasures") == [(0,)]
    refused = events(r, key, "feedback.erase")
    assert refused[-1]["result"] == "refused" and refused[-1]["code"] == "feedback_in_use"
    s, view = erase(r, key, fid, accept_residual_copies=True)
    assert s == 201 and view["fully_erased"] is False and view["residual_copies_accepted"], view
    kinds = {c["kind"]: c for c in view["residual_copies"]}
    assert kinds["challenger_job"]["job_id"] == a["challenger_job"]
    assert kinds["dataset_version"]["dataset_version"] == 2
    # ids only: never a copied value
    assert "Nowhere" not in json.dumps(view)
    # the challenger's evidence still verifies
    s, ch = call(r, key, "GET", f"triggers/{a['trigger']}/challenger")
    assert s == 200 and ch["state"] == "JOB_CREATED", ch


@maf
def test_a_keyed_invocation_keeps_a_sealed_output_copy(served):
    r, ws, key, a, model = served
    fid = fresh_feedback(r, key, idempotency="k-erase-1")
    s, body = erase(r, key, fid)
    assert (s, body["error"]["details"]["copies"]) == (409, "invocation_output"), body
    s, view = erase(r, key, fid, accept_residual_copies=True)
    assert s == 201 and [c["kind"] for c in view["residual_copies"]] == ["invocation_output"]


@maf
def test_a_pending_or_unverifiable_dependent_refuses_erasure_even_when_accepted(
    served, monkeypatch
):
    r, ws, key, a, model = served
    monitor = r.tenant(ws).api.monitor
    fid = a["feedback"][1]
    real = monitor.store.reoptimization
    for state in (ReoptState.CLAIMED, ReoptState.DATASET_CREATED):
        monkeypatch.setattr(
            monitor.store,
            "reoptimization",
            lambda t, state=state: dataclasses.replace(real(t), state=state),
        )
        s, body = erase(r, key, fid, accept_residual_copies=True)
        assert (s, body["error"]["details"]["reason"]) == (409, "reoptimization_pending"), body
    monkeypatch.setattr(monitor.store, "reoptimization", lambda t: None)
    s, body = erase(r, key, fid, accept_residual_copies=True)
    assert (s, body["error"]["details"]["reason"]) == (409, "reoptimization_pending"), body
    monkeypatch.setattr(monitor.store, "reoptimization", real)
    monkeypatch.setattr(monitor, "datasets", None)
    s, body = erase(r, key, fid, accept_residual_copies=True)
    assert (s, body["error"]["details"]["reason"]) == (409, "dependency_unverifiable"), body
    assert rows(monitoring_db(r.root, ws), "SELECT count(*) FROM feedback_erasures") == [(0,)]


@maf
def test_retention_is_off_by_default_and_never_accepts_residual_copies(served, monkeypatch):
    r, ws, key, a, model = served
    fid = fresh_feedback(r, key)
    monkeypatch.delenv(FEEDBACK_RETENTION_ENV, raising=False)
    out = purge_feedback(r)
    assert out["retention_days"] is None and out["workspaces"] == {}
    assert rows(monitoring_db(r.root, ws), "SELECT count(*) FROM feedback_erasures") == [(0,)]
    monkeypatch.setenv(FEEDBACK_RETENTION_ENV, "30")
    assert purge_feedback(r)["workspaces"][ws] == {"erased": [], "skipped": {}}  # all recent
    later = datetime.now(UTC) + timedelta(days=31)
    out = purge_feedback(r, now=later)
    assert out["workspaces"][ws]["erased"] == [fid]
    skipped = out["workspaces"][ws]["skipped"]
    assert set(skipped) == set(a["feedback"])  # frozen + copied: kept, reported
    assert set(skipped.values()) == {"feedback_in_use:residual_copies"}
    view = r.tenant(ws).api.monitor.erasure(fid)
    assert view.reason == "retention" and view.actor["user_id"] == "system:retention"
    assert purge_feedback(r, now=later)["workspaces"][ws]["erased"] == []  # idempotent
    ok = [e for e in events(r, key, "feedback.erase") if e["result"] == "ok"]
    assert len(ok) == 1 and ok[0]["actor_user_id"] == "system:retention"


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "1.5"])
def test_a_bad_retention_setting_is_refused(raw):
    with pytest.raises(ValueError, match="whole number of days"):
        feedback_retention_days_from_env({FEEDBACK_RETENTION_ENV: raw})
    assert feedback_retention_days_from_env({FEEDBACK_RETENTION_ENV: " "}) is None
    assert feedback_retention_days_from_env({FEEDBACK_RETENTION_ENV: "7"}) == 7


@maf
def test_the_operator_command_is_audited_and_rotation_skips_erased_payloads(served, monkeypatch):
    r, ws, key, a, model = served
    fid = fresh_feedback(r, key)
    out = erase_feedback(r, ws, fid)
    assert out["created"] and out["actor"]["user_id"] == "system:operator"
    assert [e["result"] for e in events(r, key, "feedback.erase")] == ["requested", "ok"]
    monkeypatch.setenv("WYNK_DATA_KEYS", f"{ROTATED},{TEST_DATA_KEYS}")
    rotated = counting_router(r.root, Counting())
    total = len(a["feedback"]) + 1
    assert secure_data(rotated)[ws]["feedback_payloads_rekeyed"] == total - 1  # not the erased one
    db = monitoring_db(r.root, ws)
    assert (
        bytes(rows(db, "SELECT envelope FROM feedback_payloads WHERE feedback_id=?", fid)[0][0])
        == ERASED
    )
    monkeypatch.setenv("WYNK_DATA_KEYS", ROTATED)  # the old key is gone: everything still reads
    s, got = call(router(r.root), key, "GET", f"feedback/{fid}")
    assert s == 200 and got["payload_erased"] is True, got


# -- the store: triggers and crash safety (no API) ---------------------------------------------
@pytest.fixture
def erased_store(tmp_path):
    from store.monitoring import FeedbackRow

    store = SQLiteMonitoringStore(tmp_path / "m.sqlite3")
    rows_ = []
    for i in range(2):
        fb = FeedbackRow(f"fb-{i:024d}", f"inf-{i:024d}", "wv-1", "l", True, "t", "{}", "t")
        store.put_feedback(fb, (f"{i}" * 64, b"WYNKBLOB-sealed-" + bytes([48 + i])))
        rows_.append(fb)
    store.erase_payload(rows_[0].feedback_id, "0" * 64, '{"x": 1}', frozen_trigger=None)
    return store, tmp_path / "m.sqlite3", rows_


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            f"UPDATE feedback_payloads SET envelope=X'{ERASED.hex()}' WHERE feedback_id LIKE '%1'",
            "erasure record",
        ),  # erased without a record
        (  # a sealed-looking envelope: only the stay-erased guard refuses it
            f"UPDATE feedback_payloads SET envelope=X'{b'WYNKBLOB-back'.hex()}' "
            "WHERE feedback_id LIKE '%0'",
            "stays erased",
        ),
        ("DELETE FROM feedback_erasures", None),
        ("UPDATE feedback_erasures SET erasure_json='{}'", None),
        ("UPDATE feedback_erasures SET payload_sha256='x'", None),
    ],
)
def test_the_database_refuses_an_unrecorded_reversed_or_edited_erasure(erased_store, sql, message):
    store, db, fbs = erased_store
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            conn.execute(sql)
    finally:
        conn.close()
    assert store.feedback_payload(fbs[0].feedback_id)[1] == ERASED
    assert store.feedback_payload(fbs[1].feedback_id)[1] != ERASED


def test_an_erasure_record_that_names_another_hash_does_not_erase(erased_store):
    store, db, fbs = erased_store
    with pytest.raises(Exception, match="no sealed payload"):
        store.erase_payload(fbs[1].feedback_id, "f" * 64, "{}", frozen_trigger=None)
    assert store.erasure(fbs[1].feedback_id) is None


def test_a_crash_inside_the_erasure_leaves_neither_the_record_nor_the_tombstone(erased_store):
    store, db, fbs = erased_store
    conn = sqlite3.connect(db)
    try:  # a failure AFTER the erasure record is inserted, inside the same transaction
        conn.execute(
            "CREATE TRIGGER crash BEFORE UPDATE OF envelope ON feedback_payloads "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match="simulated crash"):
        store.erase_payload(fbs[1].feedback_id, "1" * 64, "{}", frozen_trigger=None)
    assert store.erasure(fbs[1].feedback_id) is None
    assert store.feedback_payload(fbs[1].feedback_id)[1] != ERASED
    # the first erasure is untouched and a retry after the fault succeeds
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER crash")
        conn.commit()
    finally:
        conn.close()
    row, created = store.erase_payload(fbs[1].feedback_id, "1" * 64, "{}", frozen_trigger=None)
    assert created and store.feedback_payload(fbs[1].feedback_id)[1] == ERASED


def test_a_rekey_never_touches_an_erased_payload(erased_store):
    store, db, fbs = erased_store
    seen = []

    def reseal(sha: str, envelope: bytes) -> bytes | None:
        seen.append(sha)
        return None

    assert store.rekey_feedback(reseal) == 0
    assert seen == ["1" * 64]  # only the live payload was offered for re-keying


def test_a_frozen_example_erased_offline_blocks_its_resumed_reoptimization(tmp_path, monkeypatch):
    """The API refuses to erase a frozen example while its re-optimization is pending. An
    offline edit (here: the store called directly) is not refused - so the re-optimization
    itself refuses to build a dataset without the erased payload."""
    from experiments.monitoring import ProductionMonitor
    from tests.test_production_monitoring import World

    cipher = Keyring.parse(TEST_DATA_KEYS).cipher(None)
    world = World(tmp_path)

    def sealing(owner: str) -> ProductionMonitor:
        store = SQLiteMonitoringStore(world.root / "monitoring.sqlite3")
        return ProductionMonitor(
            world.deployments, store, world.jobs, world.datasets, owner=owner, payloads=cipher
        )

    world.monitor = sealing("m-1")
    version = world.serve()
    pid = world.policy()
    world.label(version, 4, wrong=True)

    class Crash(BaseException):
        pass

    def boom(self, trigger_id, owner, fence, state, **fields):
        raise Crash()  # dies right after claiming (examples frozen), before any dataset

    real = SQLiteMonitoringStore.advance_reoptimization
    monkeypatch.setattr(SQLiteMonitoringStore, "advance_reoptimization", boom)
    with pytest.raises(Crash):
        world.evaluate(version, pid)
    monkeypatch.setattr(SQLiteMonitoringStore, "advance_reoptimization", real)
    (decision,) = world.monitor.history(version).decisions
    store = world.monitor.store
    frozen = store.frozen_trigger
    fid = next(r.feedback_id for r in store.all_feedback() if frozen(r.feedback_id))
    with pytest.raises(FeedbackInUse):  # the API path refuses
        world.monitor.erase_feedback(fid, accept_residual_copies=True)
    sha = json.loads(store.feedback(fid).record_json)["payload_sha256"]
    with monkeypatch.context() as m:  # a freeze the erasure did not see: it refuses, unchanged
        m.setattr(store, "frozen_trigger", lambda feedback_id: None)
        with pytest.raises(FeedbackInUse) as raced:
            world.monitor.erase_feedback(fid)
    assert raced.value.details["reason"] == "concurrent_change"
    assert store.erasure(fid) is None and store.feedback_payload(fid)[1] != ERASED
    store.erase_payload(fid, sha, "{}", frozen_trigger=frozen(fid))  # the offline edit
    versions_before = [v.dataset_version for v in world.datasets.get_dataset("capitals")]
    other = sealing("m-other")
    other.clock = lambda: 10**12  # the crashed monitor's lease expired
    other.reoptimize(decision.trigger_id)
    row = store.reoptimization(decision.trigger_id)
    assert row.state is ReoptState.FAILED
    assert json.loads(row.detail)["reason"] == "frozen_feedback_erased"
    assert [v.dataset_version for v in world.datasets.get_dataset("capitals")] == versions_before


def test_a_payload_cannot_be_inserted_already_erased(tmp_path):
    from store.monitoring import FeedbackRow

    store = SQLiteMonitoringStore(tmp_path / "m.sqlite3")
    fb = FeedbackRow("fb-" + "2" * 24, "inf-" + "2" * 24, "wv-1", "l", True, "t", "{}", "t")
    with pytest.raises(Exception, match="erasure record"):
        store.put_feedback(fb, ("2" * 64, ERASED))
    assert store.feedback(fb.feedback_id) is None
