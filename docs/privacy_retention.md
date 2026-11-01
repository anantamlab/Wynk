# Privacy, retention and outbound data (Phase 5)

This document covers four controls:

1. sealing of legacy feedback (an offline migration)
2. feedback erasure and retention
3. the caller-metadata allowlist
4. the outbound data policy (audit finding F2)

It also states what each control does NOT guarantee. Code:

* `experiments/feedback_migration.py`
* `experiments/monitoring.py` (erasure, metadata)
* `store/monitoring.py` (erasure records and triggers)
* `ingestion/outbound.py`
* `api/tenancy.py` (operator commands)

Tests:

* `tests/test_feedback_migration.py`
* `tests/test_feedback_retention.py`
* `tests/test_feedback_metadata.py`
* `tests/test_outbound_policy.py`
* `tests/release_readiness.py` (the release-readiness suite)

## 1. Feedback record shapes

| Shape | Where the caller data is | Can be erased |
|---|---|---|
| `inline` (before #33) | `inputs`, `output`, `expected` and metadata, in plaintext in `feedback.record_json` | no: migrate first |
| `sealed_v1` (#33) | row data sealed; `untrusted_metadata` (actor, source) in plaintext in the record | no: migrate first |
| `sealed` (Phase 5) | everything the caller supplied is in ONE sealed payload (AES-256-GCM, workspace key, AAD = payload hash); the record names only the hash | yes |
| `erased` | the payload envelope is the `ERASED` tombstone; an immutable `feedback_erasures` row records why | already erased |

A Phase 5 server writes only `sealed` records. It still reads all four shapes.

## 2. Legacy migration (`migrate-feedback`)

```
python -m api.tenancy migrate-feedback --data-dir DIR --dry-run
python -m api.tenancy migrate-feedback --data-dir DIR --backup-dir /safe/place [--workspace-id ws-...] [--remove-backup]
```

* Stop the server first. The migration is offline.
* `--dry-run` opens each store read-only (`mode=ro`). It reports counts per shape and the
  legacy feedback ids. It never reports a value, and it writes nothing (no audit event either).
* `--backup-dir` is required unless it is a dry run. It must be outside the data directory.

The `feedback` table stays immutable in the live database. No trigger is weakened in place.
The migration is an explicit, offline, verified rebuild:

1. **scan**: read-only counts and ids.
2. **build**: a NEW file `<db>.feedback-migration.new` (SQLite backup API copy). In the new file
   only, two identity triggers are dropped. Every legacy record is rewritten to `sealed`. The
   triggers are then re-created from `store.monitoring.SCHEMA`. The file is vacuumed with
   `secure_delete`.
3. **verify**: every feedback of the new file is opened and compared with the same feedback
   opened from the old file (value for value). All of these must be equal: ids, every other
   column, every other table (row fingerprints) and the trigger set (exactly the store's). Also
   `integrity_check` and `foreign_key_check` must pass, and no legacy shape may remain.
4. **backup**: a single-file copy of the OLD store into `--backup-dir`, verified by fingerprint.
5. **swap**: the old store's fingerprint is checked again. A write since step 2 aborts the run.
   Then `os.replace`.
6. **re-verify**: the swapped store against the backup. Then the journal is removed.

**Recovery.** A journal (`<db>.feedback-migration.json`) records the step. Run the same command
again after a crash. Before the swap, the old store is untouched and the new file is rebuilt.
After the swap, the swapped store is verified against the backup. Tests crash after every step.

**Idempotence.** A store with no legacy record is not changed (`nothing_to_migrate`).

**The backup is kept.** It holds the OLD PLAINTEXT, so protect it like the data. It is deleted
only with `--remove-backup`, and only after the swapped store verified. The run is audited
(`feedback.migrate`: `requested`, then `ok` with counts).

**Boundary.** The rebuild removes plaintext from the live store file. It is not physical
erasure. Old WAL frames that were already copied elsewhere, filesystem snapshots, other backups
and storage-media remanence are outside the application's control. The report says
`"physical_erasure": false`.

**Remaining window.** The writer check (step 5) and `os.replace` are not one atomic step.
Failure mode: a server that still runs commits a write in the few milliseconds between them.
That write is then lost from the live store (it stays in the backup). Likelihood: low, because
the procedure requires a stopped server and the WAL checkpoint refuses a busy store. Cost of a
full fix: an OS-level exclusive lock on every store open. That is not implemented. Stop the
server.

## 3. Erasure

```
POST /api/v1/feedback/{feedback_id}/erase   {"accept_residual_copies": false}   (OWNER / admin only)
python -m api.tenancy erase-feedback --data-dir DIR --workspace-id ws-... --feedback-id fb-... [--accept-residual-copies]
```

**Destroyed:** the sealed payload (`inputs`, `output`, `expected`, `untrusted_metadata`). The
envelope is replaced by the `ERASED` tombstone in one `BEGIN IMMEDIATE` transaction with the
erasure record. The connection runs with `secure_delete=ON`, and the WAL is checkpointed after.

**Kept** (the minimal evidence, `ERASURE_KEPT`):

* the feedback id, the inference id, the workflow version and the lineage
* `labelled`, the timestamps and the evaluation record (no target values)
* the payload hash (`payload_sha256`)
* the authenticated principal (pseudonymous ids)
* the erasure record: reason, actor, residual copies

**Note on the kept hash.** A payload hash of low-entropy values (a yes/no label, a short
question) can be confirmed by guessing. The hash is kept as integrity evidence. It is not
anonymous.

**Guarantees** (each one is tested):

* **Authorization.** Only the `admin` scope (an OWNER). A member key gets
  `403 insufficient_scope`. Another workspace's id answers exactly like an id that never existed.
* **Audit.** `feedback.erase`: `requested` before, then `ok` or `refused` with the error code.
  The `ok` event names the hash and the residual-copy count, never a value.
* **Idempotence.** A second call returns the stored erasure (`created: false`).
* **Fail-closed.** Nothing changes when the request is refused:
  * `409 feedback_not_sealed`: a legacy shape. Migrate it first.
  * `409 feedback_in_use`, reason `reoptimization_pending`: the feedback is frozen into a
    re-optimization that has not finished (no row, `CLAIMED` or `DATASET_CREATED`). This is
    refused even with `accept_residual_copies`.
  * `409 feedback_in_use`, reason `dependency_unverifiable`: a dependent this process cannot
    inspect.
  * `409 feedback_in_use`, reason `residual_copies`: copies that erasure cannot reach. The
    response lists their kinds.
* **Residual copies.** These are listed by id only:
  * `dataset_version`: the challenger dataset version, while a live upload holds its content
  * `challenger_job`: the challenger job; its attempts hold model answers
  * `invocation_output`: a keyed invocation's sealed output (kept for retries)

  With `accept_residual_copies: true` the payload is erased, and the record says
  `fully_erased: false` with the list. `fully_erased: true` is claimed only when no copy is
  known. Delete the challenger dataset (`DELETE` dataset) to remove that copy.
* **Database guards** (additive triggers; no existing trigger changed):
  * an envelope becomes `ERASED` only when an erasure record with the same hash exists
  * an erased envelope never changes again
  * an erasure record is never updated or deleted
* **Crash safety.** The erasure record and the tombstone commit together or not at all. A
  failure between them leaves neither (tested with an injected fault).
* **Effects on other features:**
  * Erased feedback is skipped by the aggregates, drift and new challenger datasets.
  * Key rotation (`secure-data`) skips tombstones.
  * A re-optimization that would need an erased frozen payload stops
    (`frozen_feedback_erased`). It never builds a dataset without that payload.

**Boundary.** Erasure removes the payload from the live store. It does not remove:

* copies in backups or filesystem snapshots
* WAL frames copied before the checkpoint
* the residual copies above, when they were accepted
* data the caller sent elsewhere

A backup taken before the erasure can still be opened with the workspace key while that key
exists.

## 4. Retention (`purge-feedback`)

```
WYNK_FEEDBACK_RETENTION_DAYS=90 python -m api.tenancy purge-feedback --data-dir DIR
```

* **Default: keep.** When the variable is not set, nothing is erased.
* A whole number of days (>= 1). Any other value is refused.
* Run it on a schedule. It erases each feedback received more than N days ago, with reason
  `retention` and actor `system:retention`, audited like an erasure request.
* **Conservative.** Retention NEVER accepts residual copies. A pending, unverifiable,
  not-sealed or copied feedback is SKIPPED and reported as `code:reason`. It is not erased.

## 5. Caller metadata (`actor`, `source`)

These values are caller-asserted. They are never an identity:

* The authenticated principal comes from the API key and is resolved by the server.
* `metadata_trusted` is always `false`.

Phase 5 adds three controls:

* **Sealed.** The metadata is in the sealed payload. It is not plaintext in the store.
* **Allowlist** (`METADATA_VALUE`). ASCII letters, digits, space and `. _ @ + : / # = , -`.
  At most 200 characters. No leading or trailing space. Refused:
  * control characters (newline, CR, tab, NUL) and non-ASCII (bidi overrides)
  * quotes, backslash, `<`, `>`, `$`, `{`, `}`
* **More rules:**
  * a value must not start with `= + - @` (spreadsheet formulas)
  * `source` keys are 1-32 characters: lowercase letters, digits, `_ . -`, starting with a letter
  * at most 20 keys
  * a value that holds a credential (the secret rules of `ingestion.sensitive`) is refused

A refusal (`400 invalid_request`) names the rule or the category. It never names the value or a
caller-chosen key. The key check runs before type validation for this reason.

## 6. Outbound data policy (F2)

Uploads are screened when they are stored. Inference inputs and feedback values never pass
through an upload. Before Phase 5 they reached the model provider (or a challenger dataset)
unscreened. `ingestion.outbound.OutboundPolicy` (`wynk-outbound-policy/1`) closes that gap with
the same fixed-format rules. It uses no model and no external service.

| Mode | Behaviour |
|---|---|
| `off` | nothing is screened (the behaviour before the policy existed) |
| `screen` | every string and integer (nested values and keys too) is matched; a category whose action is `block` refuses the request |

The refusal is `422 outbound_data_blocked`. It happens BEFORE any model binding, provider
request, inference record or idempotency key binding.

* **Default for new workspaces:** `screen`.
  * Blocked: credentials, payment cards (Luhn-valid, with a network prefix), US SSNs and IBANs.
  * Allowed: email and `+` phone numbers. A workspace can block them:
    `screen:email=block,phone=block`.
* Credentials can never be allowed while screening is on.
* **There is no `redact`.** A redacted question is a different request. Its hash would no longer
  match its feedback, replay or telemetry. A category is sent verbatim or refused.
* **Versioned.** Each workspace has an append-only revision history (`identity.sqlite3`,
  `outbound_policies`). A new workspace gets revision 1 (`DEFAULT_POLICY`) in its creation
  transaction.
* **Existing workspaces do not change.** A workspace created before Phase 5 (or by the
  single-tenant migration) has no revision, so it stays `off`. Its deployments behave exactly as
  before. An operator opts it in explicitly (audited as `outbound_policy.set`):

  ```
  python -m api.tenancy outbound-policy --data-dir DIR --workspace-id ws-... [--set screen | --set off | --set screen:email=block]
  ```

* **Fail-closed.** A stored revision that does not parse refuses every invocation (`500`) and
  sends nothing. It is never replaced by a guess.
* **Refusals and audit.** A refusal names the categories and the field names, never a value
  or a part of one. Feedback inputs and `expected` are screened only after they fit the pinned
  schema, so the field names are schema fields, never caller-chosen keys. Each refusal is
  audited as `security.outbound_blocked` (route, categories, fields, policy version and hash).

**Recall is bounded.** These formats are detected:

* keys and tokens with a fixed shape
* card numbers with spaces or dashes
* SSNs, IBANs, emails and `+` phone numbers

These are NOT detected and are sent as given:

* names, addresses, passwords in free text, free-text health or financial details
* card numbers with unusual separators (`4111.1111...`) or spelled out
* local phone numbers without `+`

The screen reduces accidental disclosure. It does not prove that no personal data leaves.

## 7. Residual risks

* **P1.** The backups and WAL copies that existed before a migration or an erasure stay
  decryptable with the workspace key until the key is retired (rotation, then removal of the
  old key) and the backups are destroyed.
* **P2.** The kept payload hashes of low-entropy values can be confirmed by guessing.
* **P3.** Accepted residual copies (challenger job attempts, a challenger dataset version, a
  keyed output) keep the data until they are deleted.
* **P4.** The migration needs a stopped server (section 2, "Remaining window").
* **P5.** `authenticated_principal` stays plaintext (pseudonymous user and key ids).
* **P6.** Outbound screening is fixed-format only (section 6).
