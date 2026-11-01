"""Offline migration of legacy feedback: seal every caller-supplied value of a monitoring store.

Two older record shapes keep caller data in the plaintext ``feedback.record_json``:

    inline      before #33 sealing: ``inputs``, ``output``, ``expected`` and the caller metadata
    sealed_v1   #33 sealing: the row data is sealed, the caller metadata (``actor``, ``source``)
                is still in the record

The current shape (``sealed``) holds none of it: ``inputs``, ``output``, ``expected`` and
``untrusted_metadata`` are one sealed payload, and the record names only its hash. Erasure
(``ProductionMonitor.erase_feedback``) needs that shape.

The ``feedback`` table is immutable (triggers refuse UPDATE and DELETE) and stays so in the live
database: this module never changes a row in place. It is an explicit, offline, verified rebuild:

    1. scan       read-only (``mode=ro``): counts and feedback ids per shape, never a value.
                  ``dry_run`` stops here.
    2. build      a NEW file ``<db>.feedback-migration.new``: a consistent page copy of the store
                  (SQLite backup API), then - in that new file only - the two identity triggers
                  of ``feedback`` / ``feedback_payloads`` are dropped, every legacy record is
                  rewritten to the sealed shape (feedback id, inference id, every other column
                  and every other table unchanged), the triggers are re-created from
                  ``store.monitoring.SCHEMA`` and checked to be exactly the store's triggers,
                  and the file is ``VACUUM``-ed with ``secure_delete`` so the old plaintext
                  pages are not left behind as free pages.
    3. verify     every feedback of the new file is opened (AES-GCM + re-hash) and must equal,
                  value for value, the same feedback opened from the old file; ids, every other
                  column and every other table must be identical; no legacy shape may remain;
                  ``integrity_check`` and ``foreign_key_check`` must pass. Only then:
    4. backup     a consistent copy of the OLD file into ``backup_dir`` (verified the same way).
    5. swap       the old file's content fingerprint is re-checked (a writer since step 2 aborts
                  the run: the server must be stopped), then ``os.replace(new, db)``.
    6. re-verify  the swapped file against the backup. Then the journal is removed.

Recovery: a journal (``<db>.feedback-migration.json``, atomically replaced) records the step.
Re-running after a crash resumes: before the swap the old file is untouched, so the new file is
rebuilt from scratch; after it, the swapped file is verified against the backup. Idempotent: a
store with no legacy record is left untouched (``nothing_to_migrate``).

Nothing is deleted without an explicit flag. The backup holds the old plaintext and is KEPT
unless ``remove_backup=True`` (``--remove-backup``), and only after the swap verified. Removing
it, and the application-level rewrite itself, is not physical erasure: SQLite WAL pages of the
old file, filesystem snapshots, other backups and storage media remanence are outside the
application's control.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.canonical import canonical_hash, canonical_json
from experiments.monitoring import _PAYLOAD_V2, feedback_payload_state, open_feedback
from store.crypto import BlobCipher
from store.monitoring import _ERASURE_COLS, _FEEDBACK_COLS, SCHEMA, ErasureRow, FeedbackRow

MIGRATION_SCHEMA = "wynk-feedback-migration/1"
LEGACY_STATES = ("inline", "sealed_v1")
# The only triggers the rebuild drops (in the NEW file only) to rewrite a legacy record; both are
# re-created from ``store.monitoring.SCHEMA`` before anything is verified.
_REBUILD_TRIGGERS = ("feedback_is_immutable", "feedback_payloads_keep_their_identity")


class MigrationError(RuntimeError):
    """The migration refused or failed. The message never holds a feedback value."""


@dataclass(frozen=True)
class _ReadOnly:
    """``PayloadSource`` over one open connection (read-only for the scan)."""

    conn: sqlite3.Connection

    def feedback_payload(self, feedback_id: str) -> tuple[str, bytes] | None:
        r = self.conn.execute(
            "SELECT payload_sha256, envelope FROM feedback_payloads WHERE feedback_id=?",
            (feedback_id,),
        ).fetchone()
        return (r[0], bytes(r[1])) if r is not None else None

    def erasure(self, feedback_id: str) -> ErasureRow | None:
        if not _has_table(self.conn, "feedback_erasures"):
            return None
        r = self.conn.execute(
            f"SELECT {_ERASURE_COLS} FROM feedback_erasures WHERE feedback_id=?", (feedback_id,)
        ).fetchone()
        return ErasureRow(*r) if r is not None else None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection) -> list[FeedbackRow]:
    rows = conn.execute(f"SELECT {_FEEDBACK_COLS} FROM feedback ORDER BY feedback_id").fetchall()
    return [FeedbackRow(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7]) for r in rows]


def scan(db: Path | str) -> dict[str, Any]:
    """Counts and ids per record shape - read-only, never a value."""
    path = Path(db)
    if not path.exists():
        return {"exists": False, "counts": {}, "legacy_ids": []}
    with _read_only(path) as conn:
        if not _has_table(conn, "feedback"):
            return {"exists": True, "counts": {}, "legacy_ids": []}
        src = _ReadOnly(conn)
        states: dict[str, list[str]] = {}
        for row in _rows(conn):
            states.setdefault(feedback_payload_state(src, row), []).append(row.feedback_id)
    legacy = sorted(fid for s in LEGACY_STATES for fid in states.get(s, []))
    return {
        "exists": True,
        "counts": {s: len(ids) for s, ids in sorted(states.items())},
        "legacy_ids": legacy,
    }


def _fingerprint(conn: sqlite3.Connection, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """sha256 of every row of every table (by table), in primary-key order."""
    out = {}
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    for table in tables:
        if table in skip:
            continue
        h = hashlib.sha256()
        n = len(conn.execute(f'PRAGMA table_info("{table}")').fetchall())
        order = ", ".join(str(i) for i in range(1, n + 1))  # never rowid: VACUUM renumbers it
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):  # noqa: S608
            h.update(
                repr(tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)).encode()
            )
        out[table] = h.hexdigest()
    return out


def _triggers(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        name: " ".join(sql.split())
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
    }


def _expected_triggers() -> dict[str, str]:
    with closing(sqlite3.connect(":memory:")) as mem:
        for statement in SCHEMA:
            mem.execute(statement)
        return _triggers(mem)


def _seal(cipher: BlobCipher, body: dict[str, Any], payload: dict[str, Any]) -> tuple[str, bytes]:
    sha = canonical_hash(payload)
    body["payload_sha256"] = sha
    return sha, cipher.seal(canonical_json(payload).encode("ascii"), sha)


def _rewrite(conn: sqlite3.Connection, cipher: BlobCipher) -> list[str]:
    """In the NEW file: every legacy record to the sealed shape. Returns the rewritten ids."""
    src = _ReadOnly(conn)
    done = []
    for row in _rows(conn):
        state = feedback_payload_state(src, row)
        if state not in LEGACY_STATES:
            continue
        opened = open_feedback(src, cipher, row)  # verified (hash) before it is rewritten
        if opened is None:
            raise MigrationError(f"feedback {row.feedback_id} is erased in a legacy shape")
        body = json.loads(row.record_json)
        body.pop("payload_sha256", None)
        for k in _PAYLOAD_V2:
            body.pop(k, None)
        sha, envelope = _seal(cipher, body, {k: opened[k] for k in _PAYLOAD_V2})
        conn.execute(
            "UPDATE feedback SET record_json=? WHERE feedback_id=?",
            (canonical_json(body), row.feedback_id),
        )
        if state == "inline":
            conn.execute(
                "INSERT INTO feedback_payloads(feedback_id, payload_sha256, envelope) "
                "VALUES (?,?,?)",
                (row.feedback_id, sha, envelope),
            )
        else:
            conn.execute(
                "UPDATE feedback_payloads SET payload_sha256=?, envelope=? WHERE feedback_id=?",
                (sha, envelope, row.feedback_id),
            )
        done.append(row.feedback_id)
    return done


def _opened_all(path: Path, cipher: BlobCipher) -> dict[str, str]:
    """feedback id -> canonical hash of the fully opened record (``erased`` when erased)."""
    with _read_only(path) as conn:
        src = _ReadOnly(conn)
        out = {}
        for row in _rows(conn):
            opened = open_feedback(src, cipher, row)
            out[row.feedback_id] = "erased" if opened is None else canonical_hash(opened)
        return out


def _columns(path: Path) -> dict[str, tuple[Any, ...]]:
    """Every feedback column except the record JSON (which the rewrite changes)."""
    with _read_only(path) as conn:
        return {
            r.feedback_id: (
                r.inference_id,
                r.workflow_version,
                r.lineage_id,
                r.labelled,
                r.inference_created_at,
                r.received_at,
            )
            for r in _rows(conn)
        }


def verify(old: Path, new: Path, cipher: BlobCipher) -> dict[str, Any]:
    """``new`` holds exactly ``old``'s feedback, opened value for value, and nothing else changed;
    no legacy shape is left. Raises ``MigrationError`` on any difference."""
    problems = []
    a, b = _opened_all(old, cipher), _opened_all(new, cipher)
    if set(a) != set(b):
        problems.append("the feedback ids differ")
    problems += [f"feedback {f} opens to other values" for f in sorted(a) if a.get(f) != b.get(f)]
    if _columns(old) != _columns(new):
        problems.append("a feedback column other than its record changed")
    with _read_only(old) as o, _read_only(new) as n:
        skip = ("feedback", "feedback_payloads")
        fo, fn = _fingerprint(o, skip=skip), _fingerprint(n, skip=skip)
        # a store from before erasure existed has no erasure table; the new file has it, empty
        if "feedback_erasures" not in fo:
            fo["feedback_erasures"] = hashlib.sha256().hexdigest()
        if any(fo[t] != fn.get(t) for t in fo):
            problems.append("another table changed")
        if _triggers(n) != _expected_triggers():
            problems.append("the triggers are not exactly the store's")
        if n.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("integrity_check failed")
        if n.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("foreign_key_check failed")
    left = scan(new)["legacy_ids"]
    if left:
        problems.append(f"{len(left)} legacy records remain")
    if problems:
        raise MigrationError("verification failed: " + "; ".join(problems[:10]))
    return {"feedback": len(b), "erased": sum(v == "erased" for v in b.values())}


def _verify_copy(old: Path, copy: Path) -> None:
    with _read_only(old) as o, _read_only(copy) as c:
        if _fingerprint(o) != _fingerprint(c):
            raise MigrationError("the backup is not an exact copy")


def _backup_to(src: Path, dst: Path, *, single_file: bool = False) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for old in (dst, *_sidecars(dst)):
        old.unlink(missing_ok=True)
    with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)
        if single_file:  # a backup is one file: no -wal / -shm left beside it when it is read
            d.execute("PRAGMA journal_mode=DELETE")


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + s) for s in ("-wal", "-shm")]


def _checkpoint(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    if busy:
        raise MigrationError("the store is in use (WAL checkpoint was busy); stop the server")


class _Journal:
    def __init__(self, db: Path) -> None:
        self.path = db.with_name(db.name + ".feedback-migration.json")

    def read(self) -> dict[str, Any] | None:
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else None

    def write(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def migrate(
    db: Path | str,
    cipher: BlobCipher,
    *,
    backup_dir: Path | str,
    dry_run: bool = False,
    remove_backup: bool = False,
    clock: Callable[[], str] = lambda: time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
    crash_at: str | None = None,  # tests only: raise right after this step
) -> dict[str, Any]:
    """Seal every legacy feedback record of the monitoring store ``db`` (module docstring)."""
    path = Path(db)
    journal = _Journal(path)
    new = path.with_name(path.name + ".feedback-migration.new")
    state = journal.read()
    found = scan(path)
    report: dict[str, Any] = {
        "record_schema": MIGRATION_SCHEMA,
        "store": path.name,
        "dry_run": dry_run,
        "before": found["counts"],
        "legacy_ids": found["legacy_ids"],
    }

    def step(name: str) -> None:
        if crash_at == name:
            raise MigrationError(f"simulated crash after {name}")

    if dry_run:
        report["status"] = "dry_run" if found["legacy_ids"] else "nothing_to_migrate"
        report["resumable"] = state is not None and state.get("step")
        return report
    if state is not None and state.get("step") == "swapped" and not new.exists():
        # the old file was replaced: finish by verifying the swapped file against the backup
        backup = Path(state["backup"])
        verify(backup, path, cipher)
        report["resumed"] = "swapped"
        return _finish(report, journal, backup, remove_backup, path)
    if new.exists():  # a crash before the swap: the old file is untouched, rebuild
        new.unlink()
        for side in _sidecars(new):
            side.unlink(missing_ok=True)
        report["resumed"] = (state or {}).get("step", "build")
    if not found["legacy_ids"]:
        journal.clear()
        report["status"] = "nothing_to_migrate"
        return report
    _checkpoint(path)
    with _read_only(path) as conn:
        before = _fingerprint(conn)
    backup = Path(backup_dir) / f"{path.name}.pre-feedback-migration.{clock()}"
    journal.write({"step": "build", "backup": str(backup), "schema": MIGRATION_SCHEMA})
    # -- build (the NEW file only) -----------------------------------------------------------
    _backup_to(path, new)
    with closing(sqlite3.connect(new, timeout=30, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name in _REBUILD_TRIGGERS:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            rewritten = _rewrite(conn, cipher)
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("VACUUM")  # no free page keeps an old plaintext record
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    step("build")
    journal.write({"step": "verify", "backup": str(backup), "schema": MIGRATION_SCHEMA})
    checked = verify(path, new, cipher)
    step("verify")
    # -- backup, then swap ---------------------------------------------------------------------
    _backup_to(path, backup, single_file=True)
    _verify_copy(path, backup)
    step("backup")
    with _read_only(path) as conn:
        if _fingerprint(conn) != before:
            raise MigrationError("the store changed during the migration; stop the server")
    _checkpoint(path)
    for side in _sidecars(path):
        try:
            side.unlink(missing_ok=True)
        except OSError:
            raise MigrationError("the store is open in another process; stop the server") from None
    journal.write({"step": "swapped", "backup": str(backup), "schema": MIGRATION_SCHEMA})
    try:
        os.replace(new, path)
    except OSError:
        journal.write({"step": "verify", "backup": str(backup), "schema": MIGRATION_SCHEMA})
        raise MigrationError("the store could not be replaced; stop the server") from None
    for side in _sidecars(new):
        side.unlink(missing_ok=True)
    step("swap")
    verify(backup, path, cipher)
    report.update(rewritten=sorted(rewritten), verified=checked)
    return _finish(report, journal, backup, remove_backup, path)


def _finish(
    report: dict[str, Any], journal: _Journal, backup: Path, remove_backup: bool, path: Path
) -> dict[str, Any]:
    journal.clear()
    report["after"] = scan(path)["counts"]
    report["status"] = "migrated"
    if remove_backup:  # explicit only, and only after the swapped file verified
        for f in (backup, *_sidecars(backup)):
            f.unlink(missing_ok=True)
        report["backup"] = {"kept": False, "name": backup.name}
    else:
        report["backup"] = {"kept": True, "name": backup.name, "path": str(backup)}
    report["physical_erasure"] = False  # see the module docstring
    return report


__all__ = ["LEGACY_STATES", "MIGRATION_SCHEMA", "MigrationError", "migrate", "scan", "verify"]
