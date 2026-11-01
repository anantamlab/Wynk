"""A feedback's row data is sealed at rest (#33 envelope), never stored as plaintext.

``POST /inferences/{id}/feedback`` carries the production inputs, the model's output and the
expected answer. The feedback record (``monitoring.sqlite3``) keeps only their canonical hash;
the values are sealed with the workspace data key in ``feedback_payloads`` (AES-GCM, AAD = that
hash) and opened only to compute drift / quality and to build a challenger dataset. Every test
drives the real multi-tenant wiring (``build_api``), so the challenger in ``deploy`` is built
from opened payloads.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest

from api.tenancy import secure_data
from experiments.monitoring import MonitoringIntegrityError, ProductionMonitor
from store.crypto import Keyring
from store.monitoring import FeedbackRow, SQLiteMonitoringStore
from tests.conftest import TEST_DATA_KEYS
from tests.test_production_monitoring import COUNTRIES, SINCE, UNTIL, World, inputs_for
from tests.test_tenancy import call, deploy, maf, router, workspace

LABEL = "Nowhere"  # every feedback in ``deploy`` expects this answer
QUESTIONS = [inputs_for(c, city)["question"] for c, city in COUNTRIES[:4]]
ROTATED = "rot-2026:" + "Q" * 43


def monitoring_db(root: Path, ws: str) -> Path:
    return root / "workspaces" / ws / "monitoring.sqlite3"


def raw_bytes(db: Path) -> bytes:
    return b"".join(p.read_bytes() for p in db.parent.glob(db.name + "*"))  # + -wal / -shm


def rows(db: Path, sql: str, *args) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "data"
    r = router(root)
    ws, key = workspace(r, "Alpha", "a@alpha.test")
    return r, root, ws, key, deploy(r, ws, key)


def summary(r, key, version) -> tuple[int, dict]:
    window = urlencode({"since": SINCE, "until": UNTIL})
    return call(r, key, "GET", f"workflows/{version}/monitoring?{window}")


@maf
def test_feedback_row_data_is_sealed_and_the_challenger_is_still_built_from_it(world):
    r, root, ws, key, a = world  # ``deploy`` already built the challenger: JOB_CREATED
    db = monitoring_db(root, ws)
    blob = raw_bytes(db)
    assert LABEL.encode() not in blob
    for q in QUESTIONS:
        assert q.encode() not in blob  # no production input in clear, anywhere in the file
    for (record,) in rows(db, "SELECT record_json FROM feedback"):
        body = json.loads(record)
        assert not {"inputs", "output", "expected"} & set(body)
        assert len(body["payload_sha256"]) == 64
    sealed = rows(db, "SELECT feedback_id, payload_sha256, envelope FROM feedback_payloads")
    assert sorted(f for f, *_ in sealed) == sorted(a["feedback"])
    assert all(bytes(env).startswith(b"WYNKBLOB") for *_, env in sealed)
    # the labels reached the challenger's dataset version (opened, re-hashed, frozen)
    assert sorted(f for (f,) in rows(db, "SELECT feedback_id FROM frozen_examples")) == sorted(
        a["feedback"]
    )
    ((state, version),) = rows(db, "SELECT state, dataset_version FROM reoptimizations")
    assert (state, version) == ("JOB_CREATED", 2)
    # the aggregates open the payloads too
    s, view = summary(r, key, a["version"])
    assert s == 200 and view["feedback"]["labelled"] == len(a["feedback"]), view
    # the API never returns the row data of a feedback
    s, fb = call(r, key, "GET", f"feedback/{a['feedback'][0]}")
    assert s == 200 and LABEL not in json.dumps(fb) and QUESTIONS[0] not in json.dumps(fb)


@maf
def test_a_tampered_swapped_or_missing_payload_is_refused_never_repaired(world):
    r, root, ws, key, a = world
    db = monitoring_db(root, ws)
    first, second = sorted(a["feedback"])[:2]
    (env1,), (env2,) = (
        rows(db, "SELECT envelope FROM feedback_payloads WHERE feedback_id=?", f)[0]
        for f in (first, second)
    )
    assert summary(r, key, a["version"])[0] == 200

    def put(feedback_id: str, envelope: bytes) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE feedback_payloads SET envelope=? WHERE feedback_id=?",
                (envelope, feedback_id),
            )
            conn.commit()
        finally:
            conn.close()

    flipped = bytearray(env1)
    flipped[-1] ^= 1
    for bad in (bytes(flipped), bytes(env2)):  # a flipped bit; another feedback's envelope
        put(first, bad)
        s, body = summary(r, key, a["version"])
        assert (s, body["error"]["code"]) == (500, "monitoring_integrity_error"), body
        assert LABEL not in json.dumps(body)
    put(first, bytes(env1))
    assert summary(r, key, a["version"])[0] == 200


def test_the_database_refuses_plaintext_a_new_identity_and_deletion(tmp_path):
    store = SQLiteMonitoringStore(tmp_path / "m.sqlite3")
    fb = FeedbackRow("fb-" + "0" * 24, "inf-" + "0" * 24, "wv-1", "l", True, "t", "{}", "t")
    with pytest.raises(Exception, match="stored sealed"):
        store.put_feedback(fb, ("a" * 64, b'{"inputs": "plaintext"}'))
    assert store.feedback(fb.feedback_id) is None  # the same transaction: nothing was kept
    store.put_feedback(fb, ("a" * 64, b"WYNKBLOB-sealed"))
    db = tmp_path / "m.sqlite3"
    for sql, args, message in (
        ("UPDATE feedback_payloads SET payload_sha256=?", ("b" * 64,), "re-keyed"),
        ("UPDATE feedback_payloads SET envelope=?", (b"plain",), "re-keyed"),
        ("DELETE FROM feedback_payloads", (), "immutable"),
    ):
        conn = sqlite3.connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError, match=message):
                conn.execute(sql, args)
        finally:
            conn.close()
    assert store.feedback_payload(fb.feedback_id) == ("a" * 64, b"WYNKBLOB-sealed")


@maf
def test_key_rotation_reseals_every_payload_and_the_old_key_can_then_go(world, monkeypatch):
    r, root, ws, key, a = world
    db = monitoring_db(root, ws)
    assert secure_data(r)[ws]["feedback_payloads_rekeyed"] == 0  # already under the active key
    monkeypatch.setenv("WYNK_DATA_KEYS", f"{ROTATED},{TEST_DATA_KEYS}")
    rotated = router(root)
    assert secure_data(rotated)[ws]["feedback_payloads_rekeyed"] == len(a["feedback"])
    assert secure_data(rotated)[ws]["feedback_payloads_rekeyed"] == 0  # idempotent
    kid = ROTATED.split(":")[0].encode()
    assert all(kid in bytes(e) for (e,) in rows(db, "SELECT envelope FROM feedback_payloads"))
    monkeypatch.setenv("WYNK_DATA_KEYS", ROTATED)  # the old key is dropped
    s, view = summary(router(root), key, a["version"])
    assert s == 200 and view["feedback"]["labelled"] == len(a["feedback"]), view


@maf
def test_another_workspaces_key_does_not_open_a_copied_payload(world, tmp_path):
    r, root, ws, key, a = world
    other, other_key = workspace(r, "Beta", "b@beta.test")
    b = deploy(r, other, other_key)
    db_a, db_b = monitoring_db(root, ws), monitoring_db(root, other)
    (env_a,) = rows(db_a, "SELECT envelope FROM feedback_payloads LIMIT 1")[0]
    conn = sqlite3.connect(db_b)
    try:  # an operator copies A's sealed payload over one of B's
        conn.execute("UPDATE feedback_payloads SET envelope=?", (env_a,))
        conn.commit()
    finally:
        conn.close()
    s, body = summary(r, other_key, b["version"])
    assert (s, body["error"]["code"]) == (500, "monitoring_integrity_error"), body


def test_feedback_stored_before_sealing_still_reads_and_mixes_with_sealed(tmp_path):
    """A monitor without a keyring (and every record from before sealing) keeps the payload
    inline; the sealing monitor reads those records unchanged."""
    w = World(tmp_path)
    version = w.serve()
    w.label(version, 3, wrong=True)  # inline, as before
    sealing = ProductionMonitor(
        w.deployments,
        SQLiteMonitoringStore(w.root / "monitoring.sqlite3"),
        w.jobs,
        w.datasets,
        owner="m-2",
        payloads=Keyring.parse(TEST_DATA_KEYS).cipher(None),
    )
    w.monitor = sealing
    w.label_rest(version, 3, actor="later")  # sealed
    db = w.root / "monitoring.sqlite3"
    assert len(rows(db, "SELECT 1 FROM feedback_payloads")) == 3
    inline = [json.loads(j) for (j,) in rows(db, "SELECT record_json FROM feedback")]
    assert sum("inputs" in b for b in inline) == 3
    assert sum("payload_sha256" in b for b in inline) == 3
    view = sealing.summary(version, SINCE, UNTIL)
    assert view.feedback["labelled"] == 6
    assert sealing.rekey_feedback() == 0


def test_a_payload_that_does_not_hash_to_its_record_is_refused(tmp_path):
    """Sealed with the right key and the record's hash as AAD, yet other values (a writer
    bug): the re-hash after opening refuses it, beyond the AES-GCM check."""
    w = World(tmp_path)
    version = w.serve()
    cipher = Keyring.parse(TEST_DATA_KEYS).cipher(None)
    w.monitor = ProductionMonitor(
        w.deployments,
        SQLiteMonitoringStore(w.root / "monitoring.sqlite3"),
        w.jobs,
        w.datasets,
        payloads=cipher,
    )
    w.label(version, 2, wrong=True)
    assert w.monitor.summary(version, SINCE, UNTIL).feedback["labelled"] == 2
    db = w.root / "monitoring.sqlite3"
    ((fid, sha),) = rows(db, "SELECT feedback_id, payload_sha256 FROM feedback_payloads LIMIT 1")
    other = {"inputs": {"question": "q", "passage": "p"}, "output": None, "expected": None}
    forged = cipher.seal(json.dumps(other).encode("ascii"), sha)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE feedback_payloads SET envelope=? WHERE feedback_id=?", (forged, fid))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MonitoringIntegrityError, match="invalid"):
        w.monitor.summary(version, SINCE, UNTIL)


def test_without_its_keyring_a_sealed_payload_is_refused_not_guessed(tmp_path):
    w = World(tmp_path)
    version = w.serve()
    w.monitor = ProductionMonitor(
        w.deployments,
        SQLiteMonitoringStore(w.root / "monitoring.sqlite3"),
        w.jobs,
        w.datasets,
        payloads=Keyring.parse(TEST_DATA_KEYS).cipher(None),
    )
    w.label(version, 2, wrong=True)
    blind = w.make_monitor("m-3")  # no keyring
    with pytest.raises(MonitoringIntegrityError, match="missing"):
        blind.summary(version, SINCE, UNTIL)
