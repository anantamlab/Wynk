"""Phase 5 legacy feedback migration (``experiments.feedback_migration``), on SYNTHETIC stores
in temp dirs only.

The fixture holds all three shapes: ``inline`` (from before #33 sealing), ``sealed_v1`` (#33:
row data sealed, caller metadata in the record) and ``sealed`` (Phase 5). The migration is an
offline rebuild: dry run, build, verify, backup, swap, re-verify - every step crash-tested, and
the store's immutability triggers are exactly the store's afterwards.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.canonical import canonical_hash, canonical_json
from experiments import feedback_migration as fm
from experiments.feedback_migration import MigrationError, migrate, scan, verify
from experiments.monitoring import FeedbackNotSealed, ProductionMonitor
from store.crypto import Keyring
from store.monitoring import SQLiteMonitoringStore
from tests.conftest import TEST_DATA_KEYS
from tests.test_feedback_sealing import raw_bytes, rows
from tests.test_production_monitoring import COUNTRIES, SINCE, UNTIL, World, inputs_for

CIPHER = Keyring.parse(TEST_DATA_KEYS).cipher(None)
PLAIN = [inputs_for(c, city)["question"].encode() for c, city in COUNTRIES]


def to_v1(db: Path, feedback_id: str) -> None:
    """Rewrite one sealed (v2) record to the #33 shape (a Phase 4 store), in the fixture only."""
    conn = sqlite3.connect(db)
    try:
        record, sha, env = conn.execute(
            "SELECT f.record_json, p.payload_sha256, p.envelope FROM feedback f "
            "JOIN feedback_payloads p USING (feedback_id) WHERE feedback_id=?",
            (feedback_id,),
        ).fetchone()
        body = json.loads(record)
        payload = json.loads(CIPHER.open(bytes(env), sha))
        body["untrusted_metadata"] = payload.pop("untrusted_metadata")
        new_sha = canonical_hash(payload)
        body["payload_sha256"] = new_sha
        sealed = CIPHER.seal(canonical_json(payload).encode("ascii"), new_sha)
        conn.execute("DROP TRIGGER feedback_is_immutable")
        conn.execute("DROP TRIGGER feedback_payloads_keep_their_identity")
        conn.execute(
            "UPDATE feedback SET record_json=? WHERE feedback_id=?",
            (canonical_json(body), feedback_id),
        )
        conn.execute(
            "UPDATE feedback_payloads SET payload_sha256=?, envelope=? WHERE feedback_id=?",
            (new_sha, sealed, feedback_id),
        )
        conn.commit()
    finally:
        conn.close()
    SQLiteMonitoringStore(db).checkpoint()  # re-creates the triggers (the store's schema)


class Legacy:
    def __init__(self, tmp_path: Path) -> None:
        self.w = World(tmp_path / "w")
        self.version = self.w.serve()
        self.inline = self.w.label(self.version, 3, wrong=True, actor="legacy-actor")
        self.w.monitor = self.sealing()
        sealed = self.w.label_rest(self.version, 3, actor="v2-actor")
        self.v1, self.sealed = sealed[:1], sealed[1:]
        self.db = self.w.root / "monitoring.sqlite3"
        to_v1(self.db, self.v1[0])
        self.backups = tmp_path / "backups"

    def sealing(self) -> ProductionMonitor:
        return ProductionMonitor(
            self.w.deployments,
            SQLiteMonitoringStore(self.w.root / "monitoring.sqlite3"),
            self.w.jobs,
            self.w.datasets,
            owner="m-2",
            payloads=CIPHER,
        )

    def summary(self) -> dict:
        return self.sealing().summary(self.version, SINCE, UNTIL).model_dump(mode="json")

    def views(self) -> dict[str, dict]:
        m = self.sealing()
        return {f: m.feedback(f).model_dump(mode="json") for f in self.legacy + self.sealed}

    @property
    def legacy(self) -> list[str]:
        return sorted(self.inline + self.v1)

    def migrate(self, **kw):
        return migrate(self.db, CIPHER, backup_dir=self.backups, **kw)


@pytest.fixture
def legacy(tmp_path):
    return Legacy(tmp_path)


def digest(path: Path) -> str:
    """The store's content (every row of every table, and the triggers); a WAL checkpoint
    moves pages between files without changing it."""
    with fm._read_only(path) as conn:
        content = {"rows": fm._fingerprint(conn), "triggers": fm._triggers(conn)}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def leftovers(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.glob(db.name + ".feedback-migration*"))


def test_the_fixture_holds_every_legacy_shape(legacy):
    assert scan(legacy.db) == {
        "exists": True,
        "counts": {"inline": 3, "sealed": 2, "sealed_v1": 1},
        "legacy_ids": legacy.legacy,
    }
    assert scan(legacy.db.with_name("missing.sqlite3"))["exists"] is False


def test_a_dry_run_reports_counts_and_ids_and_writes_nothing(legacy):
    before = digest(legacy.db)
    report = legacy.migrate(dry_run=True)
    assert report["status"] == "dry_run" and report["dry_run"] is True
    assert report["before"] == {"inline": 3, "sealed": 2, "sealed_v1": 1}
    assert report["legacy_ids"] == legacy.legacy
    text = json.dumps(report)
    assert not any(q.decode() in text for q in PLAIN) and "legacy-actor" not in text
    assert digest(legacy.db) == before and leftovers(legacy.db) == []
    assert not legacy.backups.exists()


def test_migration_seals_everything_keeps_every_value_and_backs_up_first(legacy):
    summary, views = legacy.summary(), legacy.views()
    assert any(q in raw_bytes(legacy.db) for q in PLAIN)
    with pytest.raises(FeedbackNotSealed):  # a legacy record cannot be erased before
        legacy.sealing().erase_feedback(legacy.inline[0])
    report = legacy.migrate(clock=lambda: "T1")
    assert report["status"] == "migrated" and report["rewritten"] == legacy.legacy
    assert report["after"] == {"sealed": 6}
    assert report["verified"] == {"feedback": 6, "erased": 0}
    assert report["physical_erasure"] is False
    # no plaintext caller value is left in the store (main file, WAL and SHM)
    blob = raw_bytes(legacy.db)
    assert not any(q in blob for q in PLAIN)
    assert b"legacy-actor" not in blob and b"v2-actor" not in blob
    # every feedback reads exactly as before; the aggregates are identical
    assert legacy.views() == views and legacy.summary() == summary
    # the backup is KEPT (no flag) and holds the old plaintext: it is not an erasure
    backup = legacy.backups / "monitoring.sqlite3.pre-feedback-migration.T1"
    assert report["backup"] == {"kept": True, "name": backup.name, "path": str(backup)}
    assert any(q in backup.read_bytes() for q in PLAIN)
    assert leftovers(legacy.db) == []
    # the live store is still immutable: the rebuild never weakened a trigger
    conn = sqlite3.connect(legacy.db)
    try:
        for sql in (
            "UPDATE feedback SET record_json='{}'",
            "DELETE FROM feedback",
            "UPDATE feedback_payloads SET payload_sha256='x'",
            "DELETE FROM feedback_payloads",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        triggers = {n: s for n, s in conn.execute("SELECT name, sql FROM sqlite_master "
                                                  "WHERE type='trigger'")}  # fmt: skip
    finally:
        conn.close()
    assert {n: " ".join(s.split()) for n, s in triggers.items()} == fm._expected_triggers()
    # and a migrated legacy record can now be erased
    view = legacy.sealing().erase_feedback(legacy.inline[0])
    assert view.created and view.fully_erased


def test_migration_is_idempotent(legacy):
    legacy.migrate(clock=lambda: "T1")
    before = digest(legacy.db)
    again = legacy.migrate(clock=lambda: "T2")
    assert again["status"] == "nothing_to_migrate" and again["legacy_ids"] == []
    assert digest(legacy.db) == before
    assert sorted(p.name for p in legacy.backups.iterdir()) == [
        "monitoring.sqlite3.pre-feedback-migration.T1"
    ]


def test_the_backup_is_removed_only_with_the_explicit_flag(legacy):
    report = legacy.migrate(clock=lambda: "T1", remove_backup=True)
    assert report["backup"] == {
        "kept": False,
        "name": "monitoring.sqlite3.pre-feedback-migration.T1",
    }
    assert list(legacy.backups.iterdir()) == []


@pytest.mark.parametrize("step", ["build", "verify", "backup", "pre_swap", "swap"])
def test_a_crash_at_any_step_resumes_to_the_same_verified_result(legacy, step):
    summary, views = legacy.summary(), legacy.views()
    with pytest.raises(MigrationError, match="simulated crash"):
        legacy.migrate(clock=lambda: "T1", crash_at=step)
    if step != "swap":  # before the swap the live store is untouched
        assert scan(legacy.db)["legacy_ids"] == legacy.legacy
        assert legacy.summary() == summary
    report = legacy.migrate(clock=lambda: "T2")
    assert report["status"] == "migrated" and report["after"] == {"sealed": 6}
    resumed = {
        "build": "build",
        "verify": "verify",
        "backup": "verify",
        "pre_swap": "swapped",  # the journal says swapped, but the old file was still in place
        "swap": "swapped",
    }
    assert report["resumed"] == resumed[step]
    assert report["rewritten"] == sorted(legacy.legacy)  # also after a resume past the swap
    # the resumed run reuses the journal's backup name: one plaintext copy, never two
    name = "monitoring.sqlite3.pre-feedback-migration.T1"
    assert report["backup"]["name"] == name
    assert [p.name for p in legacy.backups.iterdir()] == [name]
    assert legacy.views() == views and legacy.summary() == summary
    assert leftovers(legacy.db) == []
    assert legacy.migrate()["status"] == "nothing_to_migrate"


@pytest.mark.parametrize("step", ["backup", "pre_swap", "swap"])
def test_a_resumed_run_with_the_flag_leaves_no_backup_at_all(legacy, step):
    with pytest.raises(MigrationError, match="simulated crash"):
        legacy.migrate(clock=lambda: "T1", crash_at=step)
    assert [p.name for p in legacy.backups.iterdir()] == [
        "monitoring.sqlite3.pre-feedback-migration.T1"
    ]
    report = legacy.migrate(clock=lambda: "T2", remove_backup=True)
    assert report["status"] == "migrated" and report["backup"]["kept"] is False
    assert list(legacy.backups.iterdir()) == []


def test_a_rewrite_that_changes_a_value_fails_verification_and_the_store_is_untouched(
    legacy, monkeypatch
):
    before, real = digest(legacy.db), fm._seal

    def forged(cipher, body, payload):
        if payload.get("expected"):
            payload = payload | {"expected": {"answer": "Somewhere else"}}
        return real(cipher, body, payload)

    monkeypatch.setattr(fm, "_seal", forged)
    with pytest.raises(MigrationError, match="opens to other values") as info:
        legacy.migrate(clock=lambda: "T1")
    assert "Somewhere" not in str(info.value) and "Nowhere" not in str(info.value)
    assert digest(legacy.db) == before and not legacy.backups.exists()
    monkeypatch.setattr(fm, "_seal", real)
    assert legacy.migrate(clock=lambda: "T2")["status"] == "migrated"


def test_verify_detects_a_changed_table_column_or_trigger(legacy, tmp_path):
    legacy.migrate(clock=lambda: "T1")
    backup = legacy.backups / "monitoring.sqlite3.pre-feedback-migration.T1"
    assert verify(backup, legacy.db, CIPHER)["feedback"] == 6
    for sql, message in (
        ("UPDATE frozen_examples SET feedback_id=feedback_id", None),  # a no-op: still equal
        ("DROP TRIGGER feedback_is_immutable", "triggers"),
        ("INSERT INTO meta(key, value) VALUES ('x', 'y')", "another table"),
    ):
        copy = tmp_path / f"copy-{abs(hash(sql))}.sqlite3"
        fm._backup_to(legacy.db, copy)
        conn = sqlite3.connect(copy)
        try:
            conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
        if message is None:
            verify(backup, copy, CIPHER)
        else:
            with pytest.raises(MigrationError, match=message):
                verify(backup, copy, CIPHER)


def test_a_write_during_the_migration_aborts_the_swap(legacy, monkeypatch):
    real = fm._verify_copy

    def and_a_writer(old, copy):
        real(old, copy)
        legacy.sealing().store.checkpoint()
        legacy.w.monitor = legacy.sealing()
        legacy.w.label(legacy.version, 1, wrong=False, actor="late")  # the server still runs

    monkeypatch.setattr(fm, "_verify_copy", and_a_writer)
    with pytest.raises(MigrationError, match="changed during the migration"):
        legacy.migrate(clock=lambda: "T1")
    assert scan(legacy.db)["counts"]["inline"] == 3  # untouched: the swap never happened
    monkeypatch.setattr(fm, "_verify_copy", real)
    assert legacy.migrate(clock=lambda: "T2")["after"] == {"sealed": 7}


def test_without_the_workspace_key_nothing_is_migrated(legacy):
    other = Keyring.parse("other:" + "B" * 43).cipher(None)
    before = digest(legacy.db)
    with pytest.raises(Exception, match="cannot be opened"):
        migrate(legacy.db, other, backup_dir=legacy.backups, clock=lambda: "T1")
    assert digest(legacy.db) == before


def test_an_erased_record_survives_the_migration_erased(legacy):
    m = legacy.sealing()
    erased = m.erase_feedback(legacy.sealed[0])
    assert erased.created
    report = legacy.migrate(clock=lambda: "T1")
    assert report["after"] == {"erased": 1, "sealed": 5}
    assert report["verified"] == {"feedback": 6, "erased": 1}
    assert legacy.sealing().feedback(legacy.sealed[0]).payload_erased is True
    assert rows(legacy.db, "SELECT count(*) FROM feedback_erasures") == [(1,)]


# -- the operator command (api.tenancy) --------------------------------------------------------
def test_the_operator_command_refuses_an_unsafe_backup_dir_and_audits_a_run(tmp_path):
    from api.tenancy import main, migrate_feedback
    from tests.test_tenancy import call, router, workspace

    root = tmp_path / "data"
    r = router(root)
    ws, key = workspace(r, "Alpha", "a@alpha.test")
    with pytest.raises(ValueError, match="--backup-dir is required"):
        migrate_feedback(r, backup_dir=None)
    with pytest.raises(ValueError, match="outside the data directory"):
        migrate_feedback(r, backup_dir=root / "backups")
    dry = migrate_feedback(r, backup_dir=None, dry_run=True)
    assert dry[ws]["status"] == "nothing_to_migrate" and dry[ws]["dry_run"] is True
    out = migrate_feedback(r, backup_dir=tmp_path / "backups")
    assert out[ws]["status"] == "nothing_to_migrate"
    s, body = call(r, key, "GET", "audit")
    runs = [e for e in body["events"] if e["action"] == "feedback.migrate"]
    assert [e["result"] for e in runs] == ["requested", "ok"]  # the dry run is not a write
    assert runs[1]["details"] == {"status": "nothing_to_migrate", "migrated": 0, "legacy": 0}
    assert main(["migrate-feedback", "--data-dir", str(root), "--dry-run"]) == 0

    def fails(*a, **kw):
        raise MigrationError("feedback fb-secret-id opens to other values")

    with pytest.MonkeyPatch.context() as m:  # a failed run is audited, without its message
        m.setattr("experiments.feedback_migration.migrate", fails)
        with pytest.raises(MigrationError):
            migrate_feedback(r, backup_dir=tmp_path / "backups")
    s, body = call(r, key, "GET", "audit")
    runs = [e for e in body["events"] if e["action"] == "feedback.migrate"]
    assert [(e["result"], e.get("code")) for e in runs[2:]] == [
        ("requested", None),
        ("error", "migration_failed"),
    ]
    assert "fb-secret-id" not in json.dumps(body)


def test_a_resume_after_the_swap_re_verifies_the_swapped_store(legacy):
    with pytest.raises(MigrationError, match="simulated crash"):
        legacy.migrate(clock=lambda: "T1", crash_at="swap")
    conn = sqlite3.connect(legacy.db)
    try:  # the swapped store is changed before the resume verifies it
        conn.execute("INSERT INTO meta(key, value) VALUES ('tampered', 'yes')")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MigrationError, match="another table"):
        legacy.migrate(clock=lambda: "T2")
