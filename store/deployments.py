"""Champion deployment: SQLite persistence for immutable workflow versions, the per-lineage
deployment (staging / production slots behind a revision fence), its append-only history and the
inference records that cite them.

Same conventions as ``store.champions`` / ``store.jobs``: stdlib ``sqlite3``, WAL,
``synchronous=FULL``, one ``BEGIN IMMEDIATE`` transaction per write, a fresh connection per
operation. The engine is ``experiments.deployment``; this module stores documents and enforces
immutability, the lifecycle and the fence.

Records

    workflow_versions   one immutable document per published champion (``champion_id`` UNIQUE):
                        the frozen genome and every identity it was pinned to. Never updated or
                        deleted (trigger); ``document_sha256`` is re-checked on every read.
    version_states      the lifecycle state of each version. Only the transitions below are
                        accepted (trigger), and at most one version per lineage is STAGING and at
                        most one is PRODUCTION (partial unique indexes).
    deployments         per lineage: the staging and production slots and ``revision``, the
                        optimistic fence every stage / promote / rollback must name.
    deployment_events   append-only history (trigger): who did what to which version, which
                        version it replaced, and the revision before and after.
    inference_records   append-only (trigger): one per executed invocation, citing the version.
    inference_invocations
                        an ``Idempotency-Key`` invocation: the key (as its sha256 only), the
                        canonical hash of the request it was first sent with, the inference id
                        and the version / revision it is bound to, and its state. Written
                        BEFORE any provider call; never deleted (trigger); identity columns
                        never change and the state only moves along the machine below
                        (trigger). A SUCCEEDED / FAILED invocation always has its inference
                        record, committed in the same transaction (trigger).

Invocation state (``InvocationState``)

    RESERVED --(first provider request about to leave the process)--> STARTED
    RESERVED --> SUCCEEDED | FAILED        finished without any provider request
    RESERVED --(owner dead: lease expired, deployment no longer serves it)--> ABANDONED
    STARTED  --> SUCCEEDED | FAILED        the measured outcome, with its inference record
    STARTED  --(owner dead: lease expired)--> AMBIGUOUS   a request may have been sent; never
                                                          re-sent automatically
    AMBIGUOUS --> SUCCEEDED | FAILED       only the original owner, with the measured outcome

    Every move is a compare-and-set on (state, owner[, lease]) in one ``BEGIN IMMEDIATE``
    transaction, so of two processes exactly one wins. A RESERVED invocation whose lease
    expired provably sent nothing (an owner must win RESERVED -> STARTED before its first
    request), so another request with the same key may take it over and run it.

Lifecycle

    CREATED --stage--> STAGING --promote--> PRODUCTION --(replaced)--> RETIRED
                          '--(displaced by another stage)--> RETIRED      |
                                    PRODUCTION <--rollback-- RETIRED <----'  (only a version
                                                                              that was in
                                                                              production)

Fencing. Every deployment write runs in ONE transaction that re-reads the lineage's revision and
refuses with ``StaleDeployment`` (writing nothing) unless it equals the revision the caller acted
on; it then moves the slots, the states, bumps the revision and appends the event. Two
promotions racing on one lineage can never both win, and a write based on an older view of the
deployment can never overwrite a newer one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from store.datasets import Conflict, IntegrityViolation, RepositoryError, _Conn
from store.jobs import IdempotencyClaim, utc_now
from store.tenancy import StoreQuota, admit, bind_sqlite


class VersionState(StrEnum):
    CREATED = "CREATED"  # published; not deployed anywhere
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    RETIRED = "RETIRED"


class DeploymentAction(StrEnum):
    PUBLISH = "publish"
    STAGE = "stage"
    PROMOTE = "promote"
    ROLLBACK = "rollback"


class StaleDeployment(Conflict):
    """The deployment moved since the caller read it: nothing was written."""


class InvalidTransition(Conflict):
    """The requested lifecycle move is not allowed from the version's current state."""


@dataclass(frozen=True)
class VersionRow:
    version_id: str
    lineage_id: str
    champion_id: str
    document_json: str
    document_sha256: str
    created_at: str
    state: VersionState
    state_updated_at: str


@dataclass(frozen=True)
class DeploymentRow:
    lineage_id: str
    revision: int
    staging_version_id: str | None
    production_version_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EventRow:
    seq: int
    lineage_id: str
    action: DeploymentAction
    version_id: str
    replaced_version_id: str | None
    revision_before: int
    revision_after: int
    actor: str
    created_at: str


@dataclass(frozen=True)
class InferenceRow:
    inference_id: str
    version_id: str
    lineage_id: str
    status: str
    record_json: str
    created_at: str


class InvocationState(StrEnum):
    RESERVED = "RESERVED"  # bound; no provider request has left the process
    STARTED = "STARTED"  # a provider request may have been sent
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"  # STARTED and its owner died: the outcome is unknown
    ABANDONED = "ABANDONED"  # RESERVED, its owner died and the deployment no longer serves it


FINISHED = frozenset({InvocationState.SUCCEEDED, InvocationState.FAILED})


@dataclass(frozen=True)
class InvocationRow:
    operation: str
    key_hash: str
    request_hash: str
    inference_id: str
    addressed_by: str  # "version" | "production"
    version_id: str  # the version the key is bound to: a retry never runs another one
    lineage_id: str
    deployment_revision: int | None
    state: InvocationState
    owner: str
    lease_expires_at: float  # wall-clock seconds; meaningful while RESERVED / STARTED
    created_at: str
    updated_at: str


SCHEMA_VERSION = 1
_TRANSITIONS = (
    "CREATED>STAGING",
    "STAGING>PRODUCTION",
    "STAGING>RETIRED",
    "PRODUCTION>RETIRED",
    "RETIRED>PRODUCTION",  # rollback; the store checks the version was in production before
)
_INVOCATION_TRANSITIONS = (
    "RESERVED>RESERVED",  # lease renewal / takeover of a dead owner's reservation
    "RESERVED>STARTED",
    "RESERVED>SUCCEEDED",
    "RESERVED>FAILED",
    "RESERVED>ABANDONED",
    "STARTED>STARTED",  # lease renewal
    "STARTED>SUCCEEDED",
    "STARTED>FAILED",
    "STARTED>AMBIGUOUS",
    "AMBIGUOUS>SUCCEEDED",  # the original owner measured the outcome after all
    "AMBIGUOUS>FAILED",
)
SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_versions (
        version_id      TEXT PRIMARY KEY,           -- content-addressed: wv-<document hash>
        lineage_id      TEXT NOT NULL,
        champion_id     TEXT NOT NULL UNIQUE,       -- one version per champion
        document_json   TEXT NOT NULL,
        document_sha256 TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS version_states (
        version_id  TEXT PRIMARY KEY REFERENCES workflow_versions(version_id),
        lineage_id  TEXT NOT NULL,
        state       TEXT NOT NULL
                    CHECK (state IN ('CREATED', 'STAGING', 'PRODUCTION', 'RETIRED')),
        updated_at  TEXT NOT NULL
    )""",
    # one active production (and one staging) version per lineage, enforced by the database
    """CREATE UNIQUE INDEX IF NOT EXISTS one_production_per_lineage
        ON version_states(lineage_id) WHERE state = 'PRODUCTION'""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_staging_per_lineage
        ON version_states(lineage_id) WHERE state = 'STAGING'""",
    """CREATE TABLE IF NOT EXISTS deployments (
        lineage_id            TEXT PRIMARY KEY,
        revision              INTEGER NOT NULL CHECK (revision >= 0),  -- the CAS fence
        staging_version_id    TEXT REFERENCES workflow_versions(version_id),
        production_version_id TEXT REFERENCES workflow_versions(version_id),
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS deployment_events (
        seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
        lineage_id          TEXT NOT NULL,
        action              TEXT NOT NULL
                            CHECK (action IN ('publish', 'stage', 'promote', 'rollback')),
        version_id          TEXT NOT NULL REFERENCES workflow_versions(version_id),
        replaced_version_id TEXT REFERENCES workflow_versions(version_id),
        revision_before     INTEGER NOT NULL,
        revision_after      INTEGER NOT NULL,
        actor               TEXT NOT NULL,
        created_at          TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS events_by_lineage ON deployment_events(lineage_id, seq)",
    """CREATE TABLE IF NOT EXISTS inference_records (
        inference_id TEXT PRIMARY KEY,
        version_id   TEXT NOT NULL REFERENCES workflow_versions(version_id),
        lineage_id   TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
        record_json  TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )""",
    # -- immutability, enforced by the database itself -----------------------------------------
    """CREATE TRIGGER IF NOT EXISTS workflow_versions_are_immutable
    BEFORE UPDATE ON workflow_versions
    BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS workflow_versions_are_permanent
    BEFORE DELETE ON workflow_versions
    BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS version_states_follow_the_lifecycle
    BEFORE UPDATE ON version_states
    WHEN NEW.version_id IS NOT OLD.version_id OR NEW.lineage_id IS NOT OLD.lineage_id
      OR (OLD.state || '>' || NEW.state) NOT IN ("""
    + ", ".join(f"'{t}'" for t in _TRANSITIONS)
    + """)
    BEGIN SELECT RAISE(ABORT, 'version state transition not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS version_states_are_permanent BEFORE DELETE ON version_states
    BEGIN SELECT RAISE(ABORT, 'version states are permanent'); END""",
    """CREATE TRIGGER IF NOT EXISTS deployments_are_permanent BEFORE DELETE ON deployments
    BEGIN SELECT RAISE(ABORT, 'deployments are permanent'); END""",
    """CREATE TRIGGER IF NOT EXISTS deployment_revisions_only_advance
    BEFORE UPDATE ON deployments WHEN NEW.revision != OLD.revision + 1
      OR NEW.lineage_id IS NOT OLD.lineage_id
    BEGIN SELECT RAISE(ABORT, 'a deployment write advances its revision by exactly one'); END""",
    """CREATE TRIGGER IF NOT EXISTS deployment_events_are_immutable
    BEFORE UPDATE ON deployment_events
    BEGIN SELECT RAISE(ABORT, 'deployment history is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS deployment_events_are_permanent
    BEFORE DELETE ON deployment_events
    BEGIN SELECT RAISE(ABORT, 'deployment history is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS inference_records_are_immutable
    BEFORE UPDATE ON inference_records
    BEGIN SELECT RAISE(ABORT, 'inference records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS inference_records_are_permanent
    BEFORE DELETE ON inference_records
    BEGIN SELECT RAISE(ABORT, 'inference records are immutable'); END""",
    # -- Idempotency-Key invocations: bound before any provider call, never unbound -------------
    """CREATE TABLE IF NOT EXISTS inference_invocations (
        workspace_id        TEXT NOT NULL,      -- the store's workspace ('' when unbound)
        operation           TEXT NOT NULL,
        key_hash            TEXT NOT NULL,      -- sha256 of the key (the key is never stored)
        request_hash        TEXT NOT NULL,      -- canonical hash of the request first sent with it
        inference_id        TEXT NOT NULL UNIQUE,
        addressed_by        TEXT NOT NULL CHECK (addressed_by IN ('version', 'production')),
        version_id          TEXT NOT NULL REFERENCES workflow_versions(version_id),
        lineage_id          TEXT NOT NULL,
        deployment_revision INTEGER,
        state               TEXT NOT NULL CHECK (state IN ('RESERVED', 'STARTED', 'SUCCEEDED',
                                                           'FAILED', 'AMBIGUOUS', 'ABANDONED')),
        owner               TEXT NOT NULL,      -- the fencing token of the process running it
        lease_expires_at    REAL NOT NULL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        PRIMARY KEY (workspace_id, operation, key_hash)
    )""",
    """CREATE TRIGGER IF NOT EXISTS inference_invocations_are_born_reserved_or_rejected
    BEFORE INSERT ON inference_invocations
    WHEN NEW.state NOT IN ('RESERVED', 'FAILED') OR (NEW.state = 'FAILED' AND NOT EXISTS (
        SELECT 1 FROM inference_records WHERE inference_id = NEW.inference_id))
    BEGIN SELECT RAISE(ABORT, 'an invocation is bound RESERVED or with its record'); END""",
    """CREATE TRIGGER IF NOT EXISTS inference_invocations_keep_their_identity
    BEFORE UPDATE ON inference_invocations
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.operation IS NOT OLD.operation
      OR NEW.key_hash IS NOT OLD.key_hash OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.inference_id IS NOT OLD.inference_id OR NEW.addressed_by IS NOT OLD.addressed_by
      OR NEW.version_id IS NOT OLD.version_id OR NEW.lineage_id IS NOT OLD.lineage_id
      OR NEW.deployment_revision IS NOT OLD.deployment_revision
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'an idempotency key is bound once'); END""",
    # every UPDATE (a lease renewal too) is a transition: a finished row never changes again
    """CREATE TRIGGER IF NOT EXISTS inference_invocations_follow_the_state_machine
    BEFORE UPDATE ON inference_invocations
    WHEN (OLD.state || '>' || NEW.state) NOT IN ("""
    + ", ".join(f"'{t}'" for t in _INVOCATION_TRANSITIONS)
    + """)
    BEGIN SELECT RAISE(ABORT, 'invocation state transition not allowed'); END""",
    """CREATE TRIGGER IF NOT EXISTS inference_invocations_finish_with_their_record
    BEFORE UPDATE ON inference_invocations
    WHEN NEW.state IN ('SUCCEEDED', 'FAILED') AND NOT EXISTS (
        SELECT 1 FROM inference_records WHERE inference_id = NEW.inference_id)
    BEGIN SELECT RAISE(ABORT, 'a finished invocation has its inference record'); END""",
    """CREATE TRIGGER IF NOT EXISTS inference_invocations_are_permanent
    BEFORE DELETE ON inference_invocations
    BEGIN SELECT RAISE(ABORT, 'an idempotency key is bound once'); END""",
)

_VERSION_COLS = (
    "v.version_id, v.lineage_id, v.champion_id, v.document_json, v.document_sha256, "
    "v.created_at, s.state, s.updated_at"
)
_VERSION_FROM = "workflow_versions v JOIN version_states s ON s.version_id = v.version_id"
_DEPLOYMENT_COLS = (
    "lineage_id, revision, staging_version_id, production_version_id, created_at, updated_at"
)
_EVENT_COLS = (
    "seq, lineage_id, action, version_id, replaced_version_id, revision_before, revision_after, "
    "actor, created_at"
)
_INFERENCE_COLS = "inference_id, version_id, lineage_id, status, record_json, created_at"
_INVOCATION_COLS = (
    "operation, key_hash, request_hash, inference_id, addressed_by, version_id, lineage_id, "
    "deployment_revision, state, owner, lease_expires_at, created_at, updated_at"
)


class SQLiteDeploymentStore:
    quota: StoreQuota | None = None  # #32: set by the tenant router; enforced in-transaction

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

            self._write(conn, create)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='deployments_schema_version'"
            ).fetchone()
            if row is None:
                self._write(
                    conn,
                    lambda c: c.execute(
                        "INSERT OR IGNORE INTO meta(key, value) "
                        "VALUES ('deployments_schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    ),
                )
            elif row[0] != str(SCHEMA_VERSION):
                raise RepositoryError(f"unsupported deployment store schema version {row[0]}")

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

    # -- reads ------------------------------------------------------------------------------
    def version(self, version_id: str) -> VersionRow | None:
        with self._connect() as conn:
            return self._version_in(conn, version_id)

    @staticmethod
    def _version_in(c: _Conn, version_id: str) -> VersionRow | None:
        row = c.execute(
            f"SELECT {_VERSION_COLS} FROM {_VERSION_FROM} WHERE v.version_id=?", (version_id,)
        ).fetchone()
        return _version(row) if row is not None else None

    def version_for_champion(self, champion_id: str) -> VersionRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_VERSION_COLS} FROM {_VERSION_FROM} WHERE v.champion_id=?",
                (champion_id,),
            ).fetchone()
        return _version(row) if row is not None else None

    def versions(self, lineage_id: str | None = None) -> list[VersionRow]:
        where, params = ("WHERE v.lineage_id=? ", (lineage_id,)) if lineage_id else ("", ())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_VERSION_COLS} FROM {_VERSION_FROM} {where}"
                "ORDER BY v.created_at, v.version_id",
                params,
            ).fetchall()
        return [_version(r) for r in rows]

    def deployment(self, lineage_id: str) -> DeploymentRow | None:
        with self._connect() as conn:
            return self._deployment_in(conn, lineage_id)

    @staticmethod
    def _deployment_in(c: _Conn, lineage_id: str) -> DeploymentRow | None:
        row = c.execute(
            f"SELECT {_DEPLOYMENT_COLS} FROM deployments WHERE lineage_id=?", (lineage_id,)
        ).fetchone()
        return DeploymentRow(*row) if row is not None else None

    def history(self, lineage_id: str) -> list[EventRow]:
        """Every deployment event of the lineage, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_EVENT_COLS} FROM deployment_events WHERE lineage_id=? ORDER BY seq",
                (lineage_id,),
            ).fetchall()
        return [_event(r) for r in rows]

    def inference(self, inference_id: str) -> InferenceRow | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_INFERENCE_COLS} FROM inference_records WHERE inference_id=?",
                (inference_id,),
            ).fetchone()
        return InferenceRow(*row) if row is not None else None

    def inferences(self, version_id: str) -> list[InferenceRow]:
        """Every inference record of exactly ``version_id``, oldest first (read-only: the
        monitoring authority of #31; records stay append-only)."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_INFERENCE_COLS} FROM inference_records WHERE version_id=? "
                "ORDER BY created_at, inference_id",
                (version_id,),
            ).fetchall()
        return [InferenceRow(*r) for r in rows]

    # -- publish ----------------------------------------------------------------------------
    def publish(
        self,
        version_id: str,
        lineage_id: str,
        champion_id: str,
        document_json: str,
        document_sha256: str,
        actor: str,
    ) -> tuple[VersionRow, bool]:
        """Store a new CREATED version (and the lineage's empty deployment on its first
        version), or return the version already published for ``champion_id``. ``bool``:
        inserted. Publishing never touches the staging or production slot."""

        def publish_(c: _Conn) -> tuple[VersionRow, bool]:
            existing = c.execute(
                "SELECT version_id FROM workflow_versions WHERE champion_id=?", (champion_id,)
            ).fetchone()
            if existing is not None:
                row = self._version_in(c, existing[0])
                assert row is not None
                return row, False
            if self.quota is not None:  # only a NEW version counts
                used = c.execute("SELECT COUNT(*) FROM workflow_versions").fetchone()[0]
                admit("workflow_versions", self.quota.max_workflow_versions, used)
            now = self.clock()
            try:
                c.execute(
                    "INSERT INTO workflow_versions(version_id, lineage_id, champion_id, "
                    "document_json, document_sha256, created_at) VALUES (?,?,?,?,?,?)",
                    (version_id, lineage_id, champion_id, document_json, document_sha256, now),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"workflow version {version_id} already exists") from None
            c.execute(
                "INSERT INTO version_states(version_id, lineage_id, state, updated_at) "
                "VALUES (?,?,?,?)",
                (version_id, lineage_id, VersionState.CREATED.value, now),
            )
            deployment = self._deployment_in(c, lineage_id)
            if deployment is None:
                c.execute(
                    f"INSERT INTO deployments({_DEPLOYMENT_COLS}) VALUES (?,0,NULL,NULL,?,?)",
                    (lineage_id, now, now),
                )
            revision = deployment.revision if deployment is not None else 0
            self._event(
                c,
                lineage_id,
                DeploymentAction.PUBLISH,
                version_id,
                None,
                revision,
                revision,
                actor,
                now,
            )
            row = self._version_in(c, version_id)
            assert row is not None
            return row, True

        return self._tx(publish_)

    # -- deployment moves -------------------------------------------------------------------
    def stage(self, version_id: str, expected_revision: int, actor: str) -> DeploymentRow:
        """CREATED -> STAGING. A version already staging on the lineage is displaced (RETIRED:
        it never reached production)."""

        def stage_(c: _Conn, version: VersionRow, d: DeploymentRow, now: str):
            if version.state is not VersionState.CREATED:
                raise InvalidTransition(
                    f"workflow version {version_id} is {version.state.value}; only a CREATED "
                    "version can be staged"
                )
            displaced = d.staging_version_id
            if displaced is not None:
                self._set_state(c, displaced, VersionState.RETIRED, now)
            self._set_state(c, version_id, VersionState.STAGING, now)
            c.execute(
                "UPDATE deployments SET staging_version_id=?, revision=revision+1, updated_at=? "
                "WHERE lineage_id=? AND revision=?",
                (version_id, now, d.lineage_id, d.revision),
            )
            return DeploymentAction.STAGE, displaced

        return self._move(version_id, expected_revision, actor, stage_)

    def promote(self, version_id: str, expected_revision: int, actor: str) -> DeploymentRow:
        """STAGING -> PRODUCTION, atomically: the previous production version is RETIRED in the
        same transaction, so the lineage never has zero or two active production versions."""

        def promote_(c: _Conn, version: VersionRow, d: DeploymentRow, now: str):
            if version.state is not VersionState.STAGING or d.staging_version_id != version_id:
                raise InvalidTransition(
                    f"workflow version {version_id} is {version.state.value}; only the lineage's "
                    "STAGING version can be promoted to production"
                )
            replaced = d.production_version_id
            if replaced is not None:
                self._set_state(c, replaced, VersionState.RETIRED, now)
            self._set_state(c, version_id, VersionState.PRODUCTION, now)
            c.execute(
                "UPDATE deployments SET staging_version_id=NULL, production_version_id=?, "
                "revision=revision+1, updated_at=? WHERE lineage_id=? AND revision=?",
                (version_id, now, d.lineage_id, d.revision),
            )
            return DeploymentAction.PROMOTE, replaced

        return self._move(version_id, expected_revision, actor, promote_)

    def rollback(self, version_id: str, expected_revision: int, actor: str) -> DeploymentRow:
        """RETIRED -> PRODUCTION for a version that WAS in production on this lineage before;
        the current production version is RETIRED in the same transaction. Nothing is copied:
        the old immutable version itself becomes active again."""

        def rollback_(c: _Conn, version: VersionRow, d: DeploymentRow, now: str):
            current = d.production_version_id
            if current is None:
                raise InvalidTransition(f"lineage {d.lineage_id} has no production version")
            if version_id == current:
                raise InvalidTransition(f"workflow version {version_id} is already in production")
            served = c.execute(
                "SELECT 1 FROM deployment_events WHERE lineage_id=? AND version_id=? "
                "AND action IN ('promote', 'rollback') LIMIT 1",
                (d.lineage_id, version_id),
            ).fetchone()
            if version.state is not VersionState.RETIRED or served is None:
                raise InvalidTransition(
                    f"workflow version {version_id} was never in production on lineage "
                    f"{d.lineage_id}; rollback only restores a previous production version"
                )
            self._set_state(c, current, VersionState.RETIRED, now)
            self._set_state(c, version_id, VersionState.PRODUCTION, now)
            c.execute(
                "UPDATE deployments SET production_version_id=?, revision=revision+1, "
                "updated_at=? WHERE lineage_id=? AND revision=?",
                (version_id, now, d.lineage_id, d.revision),
            )
            return DeploymentAction.ROLLBACK, current

        return self._move(version_id, expected_revision, actor, rollback_)

    def _move(self, version_id: str, expected_revision: int, actor: str, move) -> DeploymentRow:
        def move_(c: _Conn) -> DeploymentRow:
            version = self._version_in(c, version_id)
            if version is None:
                raise InvalidTransition(f"no workflow version {version_id}")
            d = self._deployment_in(c, version.lineage_id)
            if d is None:
                raise IntegrityViolation(f"lineage {version.lineage_id} has no deployment")
            if d.revision != expected_revision:
                raise StaleDeployment(
                    f"lineage {d.lineage_id} is at revision {d.revision}; this change was made "
                    f"against revision {expected_revision}"
                )
            now = self.clock()
            action, replaced = move(c, version, d, now)
            self._event(
                c,
                d.lineage_id,
                action,
                version_id,
                replaced,
                d.revision,
                d.revision + 1,
                actor,
                now,
            )
            after = self._deployment_in(c, d.lineage_id)
            assert after is not None and after.revision == d.revision + 1
            return after

        return self._tx(move_)

    @staticmethod
    def _set_state(c: _Conn, version_id: str, state: VersionState, now: str) -> None:
        c.execute(
            "UPDATE version_states SET state=?, updated_at=? WHERE version_id=?",
            (state.value, now, version_id),
        )

    @staticmethod
    def _event(
        c: _Conn,
        lineage_id: str,
        action: DeploymentAction,
        version_id: str,
        replaced: str | None,
        before: int,
        after: int,
        actor: str,
        now: str,
    ) -> None:
        c.execute(
            "INSERT INTO deployment_events(lineage_id, action, version_id, replaced_version_id, "
            "revision_before, revision_after, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (lineage_id, action.value, version_id, replaced, before, after, actor, now),
        )

    # -- inference records ------------------------------------------------------------------
    def record_inference(
        self, inference_id: str, version_id: str, lineage_id: str, status: str, record_json: str
    ) -> InferenceRow:
        now = self.clock()
        self._tx(
            lambda c: c.execute(
                f"INSERT INTO inference_records({_INFERENCE_COLS}) VALUES (?,?,?,?,?,?)",
                (inference_id, version_id, lineage_id, status, record_json, now),
            )
        )
        return InferenceRow(inference_id, version_id, lineage_id, status, record_json, now)

    # -- Idempotency-Key invocations --------------------------------------------------------
    def invocation(self, operation: str, key_hash: str) -> InvocationRow | None:
        """The invocation an idempotency key of this workspace is bound to, if any."""
        with self._connect() as conn:
            return self._invocation_in(conn, operation, key_hash)

    def _invocation_in(self, c: _Conn, operation: str, key_hash: str) -> InvocationRow | None:
        row = c.execute(
            f"SELECT {_INVOCATION_COLS} FROM inference_invocations "
            "WHERE workspace_id=? AND operation=? AND key_hash=?",
            (self.workspace_id or "", operation, key_hash),
        ).fetchone()
        return _invocation(row) if row is not None else None

    def bind_invocation(
        self,
        claim: IdempotencyClaim,
        *,
        inference_id: str,
        addressed_by: str,
        version_id: str,
        lineage_id: str,
        revision: int | None,
        owner: str,
        lease_expires_at: float,
        rejection_json: str | None = None,
    ) -> InvocationRow | None:
        """Bind ``claim``'s key to a new invocation - RESERVED, or (``rejection_json``) FAILED
        together with that rejection's inference record - in one ``BEGIN IMMEDIATE``
        transaction. If the key is already bound (by any process) nothing is written and that
        invocation is returned; ``None``: bound now."""
        now = self.clock()

        def bind(c: _Conn) -> InvocationRow | None:
            bound = self._invocation_in(c, claim.operation, claim.key_hash)
            if bound is not None:
                return bound
            state = InvocationState.RESERVED
            if rejection_json is not None:
                state = InvocationState.FAILED
                c.execute(
                    f"INSERT INTO inference_records({_INFERENCE_COLS}) VALUES (?,?,?,?,?,?)",
                    (inference_id, version_id, lineage_id, "FAILED", rejection_json, now),
                )
            c.execute(
                "INSERT INTO inference_invocations(workspace_id, " + _INVOCATION_COLS + ") "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.workspace_id or "",
                    claim.operation,
                    claim.key_hash,
                    claim.request_hash,
                    inference_id,
                    addressed_by,
                    version_id,
                    lineage_id,
                    revision,
                    state.value,
                    owner,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            return None

        return self._tx(bind)  # type: ignore[no-any-return]

    def _cas(self, sql: str, params: tuple[Any, ...]) -> bool:
        """One compare-and-set UPDATE of an invocation; ``True`` iff it matched."""
        return bool(self._tx(lambda c: c.execute(sql, params).rowcount))

    def renew_invocation(self, inference_id: str, owner: str, lease_expires_at: float) -> bool:
        """Extend the owner's lease while it runs; ``False``: it no longer owns a live run."""
        return self._cas(
            "UPDATE inference_invocations SET lease_expires_at=?, updated_at=? "
            "WHERE inference_id=? AND owner=? AND state IN ('RESERVED', 'STARTED')",
            (lease_expires_at, self.clock(), inference_id, owner),
        )

    def start_invocation(self, inference_id: str, owner: str, lease_expires_at: float) -> bool:
        """RESERVED -> STARTED, immediately before the first provider request; ``False``: the
        owner lost the reservation and must send nothing."""
        return self._cas(
            "UPDATE inference_invocations SET state='STARTED', lease_expires_at=?, updated_at=? "
            "WHERE inference_id=? AND owner=? AND state='RESERVED'",
            (lease_expires_at, self.clock(), inference_id, owner),
        )

    def take_over_invocation(
        self, inference_id: str, owner: str, new_owner: str, now_s: float, lease_expires_at: float
    ) -> bool:
        """A dead owner's RESERVED invocation (its lease expired at ``now_s``) -> ``new_owner``.
        Safe: the dead owner never won RESERVED -> STARTED, so it sent nothing."""
        return self._cas(
            "UPDATE inference_invocations SET owner=?, lease_expires_at=?, updated_at=? "
            "WHERE inference_id=? AND owner=? AND state='RESERVED' AND lease_expires_at<=?",
            (new_owner, lease_expires_at, self.clock(), inference_id, owner, now_s),
        )

    def abandon_invocation(self, inference_id: str, owner: str, now_s: float) -> bool:
        """A dead owner's RESERVED invocation -> ABANDONED (it can never run any more)."""
        return self._cas(
            "UPDATE inference_invocations SET state='ABANDONED', updated_at=? "
            "WHERE inference_id=? AND owner=? AND state='RESERVED' AND lease_expires_at<=?",
            (self.clock(), inference_id, owner, now_s),
        )

    def mark_ambiguous(self, inference_id: str, now_s: float) -> bool:
        """A STARTED invocation whose owner's lease expired at ``now_s`` -> AMBIGUOUS."""
        return self._cas(
            "UPDATE inference_invocations SET state='AMBIGUOUS', updated_at=? "
            "WHERE inference_id=? AND state='STARTED' AND lease_expires_at<=?",
            (self.clock(), inference_id, now_s),
        )

    def finish_invocation(
        self, inference_id: str, owner: str, status: str, record_json: str
    ) -> InferenceRow | None:
        """The owner's measured outcome: the inference record and the invocation's terminal
        state commit in ONE transaction. ``None`` (nothing written): ``owner`` does not own it
        (another process took the reservation over)."""
        now = self.clock()

        def finish(c: _Conn) -> InferenceRow | None:
            row = c.execute(
                "SELECT version_id, lineage_id FROM inference_invocations WHERE inference_id=? "
                "AND owner=? AND state IN ('RESERVED', 'STARTED', 'AMBIGUOUS')",
                (inference_id, owner),
            ).fetchone()
            if row is None:
                return None
            version_id, lineage_id = row
            c.execute(
                f"INSERT INTO inference_records({_INFERENCE_COLS}) VALUES (?,?,?,?,?,?)",
                (inference_id, version_id, lineage_id, status, record_json, now),
            )
            c.execute(
                "UPDATE inference_invocations SET state=?, updated_at=? WHERE inference_id=?",
                (status, now, inference_id),
            )
            return InferenceRow(inference_id, version_id, lineage_id, status, record_json, now)

        return self._tx(finish)  # type: ignore[no-any-return]


def _invocation(r: tuple[Any, ...]) -> InvocationRow:
    return InvocationRow(
        *r[:8],
        state=InvocationState(r[8]),
        owner=r[9],
        lease_expires_at=r[10],
        created_at=r[11],
        updated_at=r[12],
    )


def _version(r: tuple[Any, ...]) -> VersionRow:
    return VersionRow(*r[:6], state=VersionState(r[6]), state_updated_at=r[7])


def _event(r: tuple[Any, ...]) -> EventRow:
    return EventRow(r[0], r[1], DeploymentAction(r[2]), *r[3:])


__all__ = [
    "DeploymentAction",
    "DeploymentRow",
    "EventRow",
    "FINISHED",
    "InferenceRow",
    "InvalidTransition",
    "InvocationRow",
    "InvocationState",
    "SQLiteDeploymentStore",
    "StaleDeployment",
    "VersionRow",
    "VersionState",
]
