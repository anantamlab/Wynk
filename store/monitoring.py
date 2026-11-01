"""Production monitoring: SQLite persistence for quality feedback, versioned monitoring policies,
trigger decisions and the re-optimizations they start.

Same conventions as ``store.deployments``: stdlib ``sqlite3``, WAL, ``synchronous=FULL``, one
``BEGIN IMMEDIATE`` transaction per write, a fresh connection per operation. The engine is
``experiments.monitoring``. Inference telemetry is NOT stored here: the #30 ``inference_records``
in ``deployments.sqlite3`` are the only telemetry authority, and every row here cites one.

Records

    monitoring_policies  content-addressed (``mp-<hash>``) ``wynk-monitoring-policy/1`` documents:
                         drift statistics, thresholds, trigger rules. Immutable (trigger).
    feedback             one labelled (or unlabelled input-only) observation per inference
                         record, bound to it by ``request_sha256`` / ``output_sha256``. Immutable
                         (trigger); ``UNIQUE(inference_id)``: a second, different feedback for the
                         same inference is a conflict, never an overwrite.
    feedback_payloads    the inputs, output and expected answer of a feedback, sealed with the
                         workspace data key (``store.crypto`` envelope, AAD = their canonical
                         hash, which the feedback record names). Written with its feedback, in
                         the same transaction; only the envelope is ever replaced (key rotation,
                         ``rekey_feedback``). A feedback record from before sealing keeps its
                         payload inline and has no row here.
    trigger_decisions    one immutable decision per deterministic trigger identity
                         (``tr-<hash of the evidence>``): NOT_TRIGGERED / TRIGGERED / SUPPRESSED
                         with machine-readable reasons and evidence. The same evidence can only
                         ever have one decision (PRIMARY KEY, inserted under BEGIN IMMEDIATE),
                         and at most one TRIGGERED per workflow version is open at a time.
    frozen_examples      which feedback a TRIGGERED decision froze into a new dataset version
                         (``feedback_id`` PRIMARY KEY: a label enters at most one new version).
    reoptimizations      per TRIGGERED decision: the fenced claim and the forward-only progress
                         CLAIMED -> DATASET_CREATED -> JOB_CREATED (or FAILED), with the new
                         dataset version, splits and challenger job it produced.
    feedback_erasures    Phase 5: one immutable record per erased feedback payload (the request,
                         reason, actor ids and the copies it could not reach). The payload's
                         envelope is then the fixed ``ERASED`` tombstone, in the SAME
                         transaction; triggers allow that one change only with its record, and
                         an erased payload can never be replaced again. The feedback row itself
                         (ids, hashes, evaluation) stays: it is the minimal evidence.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from store.datasets import Conflict, RepositoryError, _Conn
from store.jobs import utc_now
from store.tenancy import bind_sqlite


class TriggerOutcome(StrEnum):
    NOT_TRIGGERED = "NOT_TRIGGERED"
    TRIGGERED = "TRIGGERED"
    SUPPRESSED = "SUPPRESSED"


class ReoptState(StrEnum):
    CLAIMED = "CLAIMED"
    DATASET_CREATED = "DATASET_CREATED"
    JOB_CREATED = "JOB_CREATED"
    FAILED = "FAILED"


class FeedbackConflict(Conflict):
    """A different feedback already exists for this inference record."""


class ReoptClaimLost(Conflict):
    """Another monitor holds (or advanced) this re-optimization: nothing was written."""


@dataclass(frozen=True)
class PolicyRow:
    policy_id: str
    policy_json: str
    created_at: str


@dataclass(frozen=True)
class FeedbackRow:
    feedback_id: str
    inference_id: str
    workflow_version: str
    lineage_id: str
    labelled: bool
    inference_created_at: str
    record_json: str
    received_at: str


@dataclass(frozen=True)
class ErasureRow:
    feedback_id: str
    payload_sha256: str
    erasure_json: str
    erased_at: str


@dataclass(frozen=True)
class DecisionRow:
    trigger_id: str
    workflow_version: str
    lineage_id: str
    policy_id: str
    outcome: TriggerOutcome
    window_until: str
    decision_json: str
    created_at: str


@dataclass(frozen=True)
class ReoptRow:
    trigger_id: str
    state: ReoptState
    owner: str
    fence: int
    lease_until: float
    dataset_id: str | None
    dataset_version: int | None
    splits_hash: str | None
    job_id: str | None
    detail: str | None
    created_at: str
    updated_at: str


SCHEMA_VERSION = 1
# The envelope of an erased feedback payload: no key opens it (format byte 0 is not an envelope
# format), it keeps the ``WYNKBLOB`` prefix the sealing triggers require, and it holds nothing.
ERASED = b"WYNKBLOB\x00erased/1"
_ERASED_SQL = f"X'{ERASED.hex()}'"
_REOPT_TRANSITIONS = (
    "CLAIMED>CLAIMED",  # a re-claim (fence bump) of an unfinished re-optimization
    "CLAIMED>DATASET_CREATED",
    "DATASET_CREATED>DATASET_CREATED",
    "DATASET_CREATED>JOB_CREATED",
    "CLAIMED>FAILED",
    "DATASET_CREATED>FAILED",
)
SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS monitoring_policies (
        policy_id   TEXT PRIMARY KEY,               -- content-addressed: mp-<document hash>
        policy_json TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        feedback_id          TEXT PRIMARY KEY,
        inference_id         TEXT NOT NULL UNIQUE,  -- one feedback per inference record
        workflow_version     TEXT NOT NULL,
        lineage_id           TEXT NOT NULL,
        labelled             INTEGER NOT NULL CHECK (labelled IN (0, 1)),
        inference_created_at TEXT NOT NULL,         -- copied from the cited inference record
        record_json          TEXT NOT NULL,
        received_at          TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS feedback_by_version ON feedback(workflow_version)",
    """CREATE TABLE IF NOT EXISTS feedback_payloads (
        feedback_id    TEXT PRIMARY KEY REFERENCES feedback(feedback_id),
        payload_sha256 TEXT NOT NULL,           -- canonical hash of {inputs, output, expected}
        envelope       BLOB NOT NULL            -- store.crypto envelope; never plaintext
    )""",
    """CREATE TABLE IF NOT EXISTS trigger_decisions (
        trigger_id       TEXT PRIMARY KEY,          -- tr-<hash of the evidence identity>
        workflow_version TEXT NOT NULL,
        lineage_id       TEXT NOT NULL,
        policy_id        TEXT NOT NULL REFERENCES monitoring_policies(policy_id),
        outcome          TEXT NOT NULL
                         CHECK (outcome IN ('NOT_TRIGGERED', 'TRIGGERED', 'SUPPRESSED')),
        window_until     TEXT NOT NULL,
        decision_json    TEXT NOT NULL,
        created_at       TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS decisions_by_version
        ON trigger_decisions(workflow_version, created_at)""",
    """CREATE TABLE IF NOT EXISTS frozen_examples (
        feedback_id TEXT PRIMARY KEY REFERENCES feedback(feedback_id),
        trigger_id  TEXT NOT NULL REFERENCES trigger_decisions(trigger_id)
    )""",
    """CREATE TABLE IF NOT EXISTS reoptimizations (
        trigger_id      TEXT PRIMARY KEY REFERENCES trigger_decisions(trigger_id),
        state           TEXT NOT NULL
                        CHECK (state IN ('CLAIMED', 'DATASET_CREATED', 'JOB_CREATED', 'FAILED')),
        owner           TEXT NOT NULL,
        fence           INTEGER NOT NULL,
        lease_until     REAL NOT NULL,
        dataset_id      TEXT,
        dataset_version INTEGER,
        splits_hash     TEXT,
        job_id          TEXT UNIQUE,
        detail          TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    # -- immutability, enforced by the database itself -----------------------------------------
    """CREATE TRIGGER IF NOT EXISTS monitoring_policies_are_immutable
    BEFORE UPDATE ON monitoring_policies
    BEGIN SELECT RAISE(ABORT, 'monitoring policies are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS monitoring_policies_are_permanent
    BEFORE DELETE ON monitoring_policies
    BEGIN SELECT RAISE(ABORT, 'monitoring policies are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_is_immutable BEFORE UPDATE ON feedback
    BEGIN SELECT RAISE(ABORT, 'feedback is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_is_permanent BEFORE DELETE ON feedback
    BEGIN SELECT RAISE(ABORT, 'feedback is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_payloads_are_sealed
    BEFORE INSERT ON feedback_payloads
    WHEN substr(NEW.envelope, 1, 8) IS NOT CAST('WYNKBLOB' AS BLOB)
    BEGIN SELECT RAISE(ABORT, 'a feedback payload is stored sealed'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_payloads_keep_their_identity
    BEFORE UPDATE ON feedback_payloads
    WHEN NEW.feedback_id IS NOT OLD.feedback_id OR NEW.payload_sha256 IS NOT OLD.payload_sha256
      OR substr(NEW.envelope, 1, 8) IS NOT CAST('WYNKBLOB' AS BLOB)
    BEGIN SELECT RAISE(ABORT, 'only a feedback payload''s envelope is re-keyed'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_payloads_are_permanent
    BEFORE DELETE ON feedback_payloads
    BEGIN SELECT RAISE(ABORT, 'feedback is immutable'); END""",
    # -- Phase 5 erasure: additive triggers (the ones above are unchanged) ----------------------
    """CREATE TABLE IF NOT EXISTS feedback_erasures (
        feedback_id    TEXT PRIMARY KEY REFERENCES feedback(feedback_id),
        payload_sha256 TEXT NOT NULL,           -- the erased payload's hash: kept as evidence
        erasure_json   TEXT NOT NULL,           -- request, reason, actor ids, residual copies
        erased_at      TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS feedback_erasures_are_immutable
    BEFORE UPDATE ON feedback_erasures
    BEGIN SELECT RAISE(ABORT, 'feedback erasures are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS feedback_erasures_are_permanent
    BEFORE DELETE ON feedback_erasures
    BEGIN SELECT RAISE(ABORT, 'feedback erasures are immutable'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS feedback_payloads_erased_only_on_record
    BEFORE UPDATE ON feedback_payloads
    WHEN NEW.envelope = {_ERASED_SQL} AND NOT EXISTS (
        SELECT 1 FROM feedback_erasures e
        WHERE e.feedback_id = NEW.feedback_id AND e.payload_sha256 = NEW.payload_sha256)
    BEGIN SELECT RAISE(ABORT, 'a feedback payload is erased only with its erasure record'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS feedback_payloads_stay_erased
    BEFORE UPDATE ON feedback_payloads
    WHEN OLD.envelope = {_ERASED_SQL}
    BEGIN SELECT RAISE(ABORT, 'an erased feedback payload stays erased'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS feedback_payloads_inserted_erased_only_on_record
    BEFORE INSERT ON feedback_payloads
    WHEN NEW.envelope = {_ERASED_SQL} AND NOT EXISTS (
        SELECT 1 FROM feedback_erasures e
        WHERE e.feedback_id = NEW.feedback_id AND e.payload_sha256 = NEW.payload_sha256)
    BEGIN SELECT RAISE(ABORT, 'a feedback payload is erased only with its erasure record'); END""",
    """CREATE TRIGGER IF NOT EXISTS trigger_decisions_are_immutable
    BEFORE UPDATE ON trigger_decisions
    BEGIN SELECT RAISE(ABORT, 'trigger decisions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trigger_decisions_are_permanent
    BEFORE DELETE ON trigger_decisions
    BEGIN SELECT RAISE(ABORT, 'trigger decisions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS frozen_examples_are_immutable
    BEFORE UPDATE ON frozen_examples
    BEGIN SELECT RAISE(ABORT, 'frozen examples are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS frozen_examples_are_permanent
    BEFORE DELETE ON frozen_examples
    BEGIN SELECT RAISE(ABORT, 'frozen examples are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS reoptimizations_move_forward
    BEFORE UPDATE ON reoptimizations
    WHEN NEW.trigger_id IS NOT OLD.trigger_id OR NEW.fence < OLD.fence
      OR (OLD.state || '>' || NEW.state) NOT IN ("""
    + ", ".join(f"'{t}'" for t in _REOPT_TRANSITIONS)
    + """)
      OR (OLD.job_id IS NOT NULL AND NEW.job_id IS NOT OLD.job_id)
      OR (OLD.dataset_version IS NOT NULL AND NEW.dataset_version IS NOT OLD.dataset_version)
    BEGIN SELECT RAISE(ABORT, 're-optimization progress only moves forward'); END""",
    """CREATE TRIGGER IF NOT EXISTS reoptimizations_are_permanent
    BEFORE DELETE ON reoptimizations
    BEGIN SELECT RAISE(ABORT, 're-optimizations are permanent'); END""",
)

_FEEDBACK_COLS = (
    "feedback_id, inference_id, workflow_version, lineage_id, labelled, inference_created_at, "
    "record_json, received_at"
)
_DECISION_COLS = (
    "trigger_id, workflow_version, lineage_id, policy_id, outcome, window_until, decision_json, "
    "created_at"
)
_REOPT_COLS = (
    "trigger_id, state, owner, fence, lease_until, dataset_id, dataset_version, splits_hash, "
    "job_id, detail, created_at, updated_at"
)
_ERASURE_COLS = "feedback_id, payload_sha256, erasure_json, erased_at"


class SQLiteMonitoringStore:
    def __init__(
        self,
        path: Path | str,
        clock: Callable[[], str] = utc_now,
        *,
        workspace_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bind_sqlite(self.path, workspace_id)  # #32: before any read or write
        self.workspace_id = workspace_id
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            def create(c: _Conn) -> None:
                for statement in SCHEMA:
                    c.execute(statement)
                row = c.execute(
                    "SELECT value FROM meta WHERE key='monitoring_schema_version'"
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO meta(key, value) VALUES ('monitoring_schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif row[0] != str(SCHEMA_VERSION):
                    raise RepositoryError(f"unsupported monitoring store schema version {row[0]}")

            self._write(conn, create)

    @contextmanager
    def _connect(self) -> Iterator[_Conn]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            yield _Conn(conn)
        finally:
            conn.close()

    @staticmethod
    def _write(conn: _Conn, fn: Callable[[_Conn], Any]) -> Any:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return out

    def _tx(self, fn: Callable[[_Conn], Any]) -> Any:
        with self._connect() as conn:
            return self._write(conn, fn)

    # -- policies ---------------------------------------------------------------------------
    def put_policy(self, policy_id: str, policy_json: str) -> tuple[PolicyRow, bool]:
        now = self.clock()

        def put(c: _Conn) -> tuple[PolicyRow, bool]:
            row = c.execute(
                "SELECT policy_id, policy_json, created_at FROM monitoring_policies "
                "WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
            if row is not None:
                return PolicyRow(*row), False
            c.execute(
                "INSERT INTO monitoring_policies(policy_id, policy_json, created_at) "
                "VALUES (?,?,?)",
                (policy_id, policy_json, now),
            )
            return PolicyRow(policy_id, policy_json, now), True

        return self._tx(put)

    def policy(self, policy_id: str) -> PolicyRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT policy_id, policy_json, created_at FROM monitoring_policies "
                "WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
        return PolicyRow(*row) if row is not None else None

    def policies(self) -> list[PolicyRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT policy_id, policy_json, created_at FROM monitoring_policies "
                "ORDER BY created_at, policy_id"
            ).fetchall()
        return [PolicyRow(*r) for r in rows]

    # -- feedback ---------------------------------------------------------------------------
    def put_feedback(
        self, row: FeedbackRow, payload: tuple[str, bytes] | None = None
    ) -> tuple[FeedbackRow, bool]:
        """Insert ``row`` (and its sealed ``payload``: ``(payload_sha256, envelope)``, in the
        same transaction), or return the identical feedback already stored for its inference
        (``bool``: inserted). A different feedback for the same inference is refused."""

        def put(c: _Conn) -> tuple[FeedbackRow, bool]:
            existing = c.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback WHERE inference_id=?", (row.inference_id,)
            ).fetchone()
            if existing is not None:
                stored = _feedback(existing)
                if stored.feedback_id != row.feedback_id:
                    raise FeedbackConflict(
                        f"inference {row.inference_id} already has feedback {stored.feedback_id}"
                    )
                return stored, False
            c.execute(
                f"INSERT INTO feedback({_FEEDBACK_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                (
                    row.feedback_id,
                    row.inference_id,
                    row.workflow_version,
                    row.lineage_id,
                    int(row.labelled),
                    row.inference_created_at,
                    row.record_json,
                    row.received_at,
                ),
            )
            if payload is not None:
                c.execute(
                    "INSERT INTO feedback_payloads(feedback_id, payload_sha256, envelope) "
                    "VALUES (?,?,?)",
                    (row.feedback_id, payload[0], payload[1]),
                )
            return row, True

        return self._tx(put)

    def feedback(self, feedback_id: str) -> FeedbackRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
        return _feedback(row) if row is not None else None

    def feedback_payload(self, feedback_id: str) -> tuple[str, bytes] | None:
        """``(payload_sha256, envelope)`` of a sealed feedback; ``None``: none stored."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_sha256, envelope FROM feedback_payloads WHERE feedback_id=?",
                (feedback_id,),
            ).fetchone()
        return (row[0], bytes(row[1])) if row is not None else None

    def rekey_feedback(self, reseal: Callable[[str, bytes], bytes | None]) -> int:
        """Key rotation: ``reseal(payload_sha256, envelope)`` returns the envelope under the
        active key (``None``: already under it). Each replacement is a CAS on the old envelope,
        so a concurrent rotation never loses one. Returns how many were re-keyed."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT feedback_id, payload_sha256, envelope FROM feedback_payloads "
                "WHERE envelope != ?",  # an erased payload has nothing left to re-key
                (ERASED,),
            )
            stored = [(r[0], r[1], bytes(r[2])) for r in rows]
        moved = 0
        for feedback_id, sha, envelope in stored:
            fresh = reseal(sha, envelope)
            if fresh is not None and self._tx(
                lambda c, f=fresh, i=feedback_id, e=envelope: (
                    c.execute(
                        "UPDATE feedback_payloads SET envelope=? "
                        "WHERE feedback_id=? AND envelope=?",
                        (f, i, e),
                    ).rowcount
                )
            ):
                moved += 1
        return moved

    def all_feedback(self) -> list[FeedbackRow]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback ORDER BY received_at, feedback_id"
            ).fetchall()
        return [_feedback(r) for r in rows]

    # -- erasure (Phase 5) -----------------------------------------------------------------
    def erase_payload(
        self, feedback_id: str, payload_sha256: str, erasure_json: str
    ) -> tuple[ErasureRow, bool]:
        """Record the erasure and replace the sealed envelope with ``ERASED`` - one
        ``BEGIN IMMEDIATE`` transaction, so a crash leaves either both or neither. Idempotent:
        an already erased payload returns its stored record (``bool`` False). The connection
        runs with ``secure_delete`` so the replaced envelope's bytes are zeroed in the page."""
        now = self.clock()

        def erase(c: _Conn) -> tuple[ErasureRow, bool]:
            existing = c.execute(
                f"SELECT {_ERASURE_COLS} FROM feedback_erasures WHERE feedback_id=?",
                (feedback_id,),
            ).fetchone()
            if existing is not None:
                return ErasureRow(*existing), False
            stored = c.execute(
                "SELECT payload_sha256 FROM feedback_payloads WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
            if stored is None or stored[0] != payload_sha256:
                raise RepositoryError(f"feedback {feedback_id} has no sealed payload to erase")
            c.execute(
                f"INSERT INTO feedback_erasures({_ERASURE_COLS}) VALUES (?,?,?,?)",
                (feedback_id, payload_sha256, erasure_json, now),
            )
            changed = c.execute(
                "UPDATE feedback_payloads SET envelope=? WHERE feedback_id=? AND payload_sha256=?",
                (ERASED, feedback_id, payload_sha256),
            ).rowcount
            if changed != 1:
                raise RepositoryError(f"feedback {feedback_id}'s payload changed while erasing")
            return ErasureRow(feedback_id, payload_sha256, erasure_json, now), True

        with self._connect() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            out = self._write(conn, erase)
        if out[1]:
            self.checkpoint()
        return out

    def checkpoint(self) -> bool:
        """Copy the WAL into the database file and truncate it, so a replaced envelope's old
        page does not stay in ``-wal``. ``False``: a reader held it (retried on the next one)."""
        with self._connect() as conn:
            busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return busy == 0

    def erasure(self, feedback_id: str) -> ErasureRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_ERASURE_COLS} FROM feedback_erasures WHERE feedback_id=?",
                (feedback_id,),
            ).fetchone()
        return ErasureRow(*row) if row is not None else None

    def erasures(self) -> list[ErasureRow]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ERASURE_COLS} FROM feedback_erasures ORDER BY erased_at, feedback_id"
            ).fetchall()
        return [ErasureRow(*r) for r in rows]

    def frozen_trigger(self, feedback_id: str) -> str | None:
        """The TRIGGERED decision that froze this feedback into a dataset version, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trigger_id FROM frozen_examples WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
        return row[0] if row is not None else None

    def feedback_for_version(self, workflow_version: str) -> list[FeedbackRow]:
        """Every feedback citing an inference of exactly ``workflow_version``."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback WHERE workflow_version=? "
                "ORDER BY inference_created_at, feedback_id",
                (workflow_version,),
            ).fetchall()
        return [_feedback(r) for r in rows]

    def frozen_feedback_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT feedback_id FROM frozen_examples").fetchall()
        return {r[0] for r in rows}

    def frozen_by(self, trigger_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT feedback_id FROM frozen_examples WHERE trigger_id=? ORDER BY feedback_id",
                (trigger_id,),
            ).fetchall()
        return [r[0] for r in rows]

    # -- decisions --------------------------------------------------------------------------
    def decide(
        self,
        trigger_id: str,
        workflow_version: str,
        lineage_id: str,
        policy_id: str,
        window_until: str,
        decide: Callable[[list[DecisionRow], set[str]], tuple[TriggerOutcome, str, list[str]]],
    ) -> tuple[DecisionRow, bool]:
        """The decision for ``trigger_id``: the stored one if this evidence was already decided
        (``bool`` False), else ``decide(prior decisions of this version, frozen feedback ids)``
        evaluated and inserted - with the feedback a TRIGGERED decision freezes - inside ONE
        ``BEGIN IMMEDIATE`` transaction. Concurrent monitors are serialised by the write lock,
        so one evidence never gets two decisions and two TRIGGERED decisions are never both
        written on the strength of the same history."""
        now = self.clock()

        def write(c: _Conn) -> tuple[DecisionRow, bool]:
            existing = c.execute(
                f"SELECT {_DECISION_COLS} FROM trigger_decisions WHERE trigger_id=?",
                (trigger_id,),
            ).fetchone()
            if existing is not None:
                return _decision(existing), False
            prior = [
                _decision(r)
                for r in c.execute(
                    f"SELECT {_DECISION_COLS} FROM trigger_decisions WHERE workflow_version=? "
                    "ORDER BY created_at, trigger_id",
                    (workflow_version,),
                ).fetchall()
            ]
            frozen = {r[0] for r in c.execute("SELECT feedback_id FROM frozen_examples")}
            outcome, decision_json, freeze = decide(prior, frozen)
            c.execute(
                f"INSERT INTO trigger_decisions({_DECISION_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                (
                    trigger_id,
                    workflow_version,
                    lineage_id,
                    policy_id,
                    outcome.value,
                    window_until,
                    decision_json,
                    now,
                ),
            )
            if outcome is TriggerOutcome.TRIGGERED:
                for feedback_id in sorted(freeze):
                    c.execute(
                        "INSERT INTO frozen_examples(feedback_id, trigger_id) VALUES (?,?)",
                        (feedback_id, trigger_id),
                    )
            row = DecisionRow(
                trigger_id,
                workflow_version,
                lineage_id,
                policy_id,
                outcome,
                window_until,
                decision_json,
                now,
            )
            return row, True

        return self._tx(write)

    def decision(self, trigger_id: str) -> DecisionRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_DECISION_COLS} FROM trigger_decisions WHERE trigger_id=?",
                (trigger_id,),
            ).fetchone()
        return _decision(row) if row is not None else None

    def decisions(self, workflow_version: str) -> list[DecisionRow]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_DECISION_COLS} FROM trigger_decisions WHERE workflow_version=? "
                "ORDER BY created_at, trigger_id",
                (workflow_version,),
            ).fetchall()
        return [_decision(r) for r in rows]

    # -- re-optimizations -------------------------------------------------------------------
    def reoptimization(self, trigger_id: str) -> ReoptRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_REOPT_COLS} FROM reoptimizations WHERE trigger_id=?", (trigger_id,)
            ).fetchone()
        return _reopt(row) if row is not None else None

    def claim_reoptimization(
        self, trigger_id: str, owner: str, now: float, lease_s: float
    ) -> ReoptRow | None:
        """Claim the (unfinished) re-optimization of a TRIGGERED decision: a fresh claim, or a
        re-claim of one whose lease expired or that ``owner`` already holds. Every claim bumps
        the fence. ``None``: finished (JOB_CREATED / FAILED) or held by a live other monitor."""
        stamp = self.clock()

        def claim(c: _Conn) -> ReoptRow | None:
            decision = c.execute(
                "SELECT outcome FROM trigger_decisions WHERE trigger_id=?", (trigger_id,)
            ).fetchone()
            if decision is None or decision[0] != TriggerOutcome.TRIGGERED.value:
                raise RepositoryError(f"{trigger_id} is not a TRIGGERED decision")
            row = c.execute(
                f"SELECT {_REOPT_COLS} FROM reoptimizations WHERE trigger_id=?", (trigger_id,)
            ).fetchone()
            if row is None:
                c.execute(
                    f"INSERT INTO reoptimizations({_REOPT_COLS}) "
                    "VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?)",
                    (trigger_id, ReoptState.CLAIMED.value, owner, 1, now + lease_s, stamp, stamp),
                )
            else:
                current = _reopt(row)
                if current.state in (ReoptState.JOB_CREATED, ReoptState.FAILED):
                    return None
                if current.owner != owner and current.lease_until > now:
                    return None
                c.execute(
                    "UPDATE reoptimizations SET owner=?, fence=fence+1, lease_until=?, "
                    "updated_at=? WHERE trigger_id=?",
                    (owner, now + lease_s, stamp, trigger_id),
                )
            return _reopt(
                c.execute(
                    f"SELECT {_REOPT_COLS} FROM reoptimizations WHERE trigger_id=?", (trigger_id,)
                ).fetchone()
            )

        return self._tx(claim)

    def release_reoptimization(self, trigger_id: str, owner: str, fence: int) -> bool:
        """End the lease ``(owner, fence)`` holds without moving progress (e.g. job admission
        was refused), so any monitor can resume the same re-optimization at once. Fenced:
        ``False`` and no change at all when that claim is stale (a newer claim holds it)."""
        stamp = self.clock()

        def release(c: _Conn) -> bool:
            cur = c.execute(
                "UPDATE reoptimizations SET lease_until=0, updated_at=? "
                "WHERE trigger_id=? AND owner=? AND fence=? "
                "AND state NOT IN ('JOB_CREATED', 'FAILED')",
                (stamp, trigger_id, owner, fence),
            )
            return cur.rowcount == 1

        return bool(self._tx(release))

    def advance_reoptimization(
        self, trigger_id: str, owner: str, fence: int, state: ReoptState, **fields: Any
    ) -> ReoptRow:
        """Move a claimed re-optimization forward, only while (owner, fence) still holds it."""
        allowed = {"dataset_id", "dataset_version", "splits_hash", "job_id", "detail"}
        if set(fields) - allowed:
            raise ValueError(f"unknown fields {sorted(set(fields) - allowed)}")
        stamp = self.clock()

        def advance(c: _Conn) -> ReoptRow:
            sets = ", ".join(f"{k}=?" for k in sorted(fields))
            cur = c.execute(
                f"UPDATE reoptimizations SET state=?, updated_at=?{', ' + sets if sets else ''} "
                "WHERE trigger_id=? AND owner=? AND fence=?",
                (
                    state.value,
                    stamp,
                    *(fields[k] for k in sorted(fields)),
                    trigger_id,
                    owner,
                    fence,
                ),
            )
            if cur.rowcount != 1:
                raise ReoptClaimLost(f"re-optimization {trigger_id} is no longer held by {owner}")
            return _reopt(
                c.execute(
                    f"SELECT {_REOPT_COLS} FROM reoptimizations WHERE trigger_id=?", (trigger_id,)
                ).fetchone()
            )

        return self._tx(advance)


def _feedback(r: tuple[Any, ...]) -> FeedbackRow:
    return FeedbackRow(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7])


def _decision(r: tuple[Any, ...]) -> DecisionRow:
    return DecisionRow(r[0], r[1], r[2], r[3], TriggerOutcome(r[4]), r[5], r[6], r[7])


def _reopt(r: tuple[Any, ...]) -> ReoptRow:
    return ReoptRow(r[0], ReoptState(r[1]), *r[2:])


__all__ = [
    "ERASED",
    "DecisionRow",
    "ErasureRow",
    "FeedbackConflict",
    "FeedbackRow",
    "PolicyRow",
    "ReoptClaimLost",
    "ReoptRow",
    "ReoptState",
    "SQLiteMonitoringStore",
    "TriggerOutcome",
]
