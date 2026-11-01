"""Server-minted identifiers are not user data (the monitoring PII false positive).

A feedback id is ``"fb-" + canonical_hash(identity)[:24]`` and a trigger id ``"tr-" + ...[:24]``.
The inference id inside a feedback identity is random, so about one feedback id in 10,500 holds a
13-19 digit run that passes the Luhn check. The challenger dataset uses feedback ids as its new
row ids, and its upload filename holds the trigger id, so the payment-card rule refused the
upload: re-optimization failed intermittently.

The fix exempts EXACTLY the identifiers the server minted, by provenance (``ServerMinted``), and
only where it put them: never a pattern a user value can match, never another column.
"""

from __future__ import annotations

import json
import secrets

import pytest

import experiments.monitoring as monitoring
from core.canonical import canonical_hash
from experiments.monitoring import DECISION_SCHEMA, FEEDBACK_SCHEMA, ReoptState, TriggerOutcome
from ingestion.sensitive import (
    Category,
    SensitiveDataPolicy,
    ServerMinted,
    _luhn,
    screen,
)
from ingestion.service import DatasetService, IngestError, NewProject, RegisterDataset
from store.blobs import LocalBlobStore
from store.datasets import SQLiteDatasetRepository
from tests.test_production_monitoring import COUNTRIES, World, _label_one


def luhn_complete(prefix: str) -> str:
    """``prefix`` + the one check digit that makes it pass the Luhn check."""
    for d in "0123456789":
        if _luhn(prefix + d):
            return prefix + d
    raise AssertionError("unreachable")


# Luhn-valid runs of every length the card rule covers, with real issuer prefixes
CARD_RUNS = [
    luhn_complete(p)
    for p in ("422222222222", "37828224631000", "411111111111111", "400000000000000000")
]
assert [len(c) for c in CARD_RUNS] == [13, 15, 16, 19]
assert all(_luhn(c) for c in CARD_RUNS)


def adversarial(run: str, real: str) -> str:
    """24 hex characters that START with the card run; a letter ends it, the real hash keeps
    the id unique."""
    return (run + "a" + real)[:24]


@pytest.fixture
def card_ids(monkeypatch):
    """Every feedback and trigger id holds a Luhn-valid card run, as a real hash sometimes does."""
    real = monitoring.canonical_hash
    counter = {"n": 0}

    def rigged(value):
        digest = real(value)
        if isinstance(value, dict) and value.get("record_schema") in (
            FEEDBACK_SCHEMA,
            DECISION_SCHEMA,
        ):
            run = CARD_RUNS[counter["n"] % len(CARD_RUNS)]
            counter["n"] += 1
            return adversarial(run, digest) + digest[24:]
        return digest

    monkeypatch.setattr(monitoring, "canonical_hash", rigged)
    return CARD_RUNS


def test_card_like_feedback_and_trigger_ids_still_reoptimize(tmp_path, card_ids):
    world = World(tmp_path)
    version = world.serve()
    pid = world.policy()
    world.label(version, 3, wrong=True)
    _label_one(world, version, COUNTRIES[3], wrong=True)
    d = world.evaluate(version, pid)
    assert d.outcome is TriggerOutcome.TRIGGERED
    assert any(run in d.trigger_id for run in card_ids)  # the rigged case really happened
    assert all(any(run in fid for run in card_ids) for fid in d.frozen_feedback_ids)

    view = world.monitor.challenger(d.trigger_id)
    assert view.state is ReoptState.JOB_CREATED, view
    assert view.dataset["new"]["dataset_version"] == 2
    new_ids = set(world.datasets.repo.get_row_ids("capitals", 2))
    assert new_ids - set(world.datasets.repo.get_row_ids("capitals", 1)) == set(
        d.frozen_feedback_ids
    )
    # the stored upload report has no finding: the minted ids are not user data
    upload = world.datasets.repo.get_upload(view.dataset["new"]["upload_id"])
    assert upload.sensitive_data.findings == ()
    assert d.trigger_id in upload.filename  # the filename keeps its provenance


def test_repeated_reoptimizations_are_deterministic_with_card_like_ids(tmp_path, card_ids):
    """The same evidence gives the same dataset bytes, row ids and splits in a fresh store."""
    results = []
    for run in ("a", "b"):
        world = World(tmp_path / run)
        version = world.serve()
        pid = world.policy()
        world.label(version, 3, wrong=True)
        _label_one(world, version, COUNTRIES[3], wrong=True)
        d = world.evaluate(version, pid)
        view = world.monitor.challenger(d.trigger_id)
        assert view.state is ReoptState.JOB_CREATED
        results.append(view)
    # inference ids are random per world, so ids differ; the structure must not
    a, b = results
    assert a.dataset["new"]["dataset_version"] == b.dataset["new"]["dataset_version"] == 2
    assert sorted(a.dataset["new_rows"]) == sorted(b.dataset["new_rows"])


# -- the screen: only exact, minted identifiers, only where the server put them ---------------
POLICY = SensitiveDataPolicy()
ID = adversarial(CARD_RUNS[2], "0" * 24)
MINTED_ID = "fb-" + ID


def rows(*values: str, column: str = "qid") -> list[dict[str, str]]:
    return [{column: v, "question": "q"} for v in values]


def test_without_the_exemption_a_card_like_minted_id_is_a_payment_card():
    report = screen(["qid", "question"], rows(MINTED_ID), POLICY).report
    assert [f.category for f in report.blocked] == [Category.PAYMENT_CARD]


def test_the_exemption_covers_exactly_the_minted_cells_of_its_column():
    minted = ServerMinted(cells={"qid": frozenset({MINTED_ID})})
    assert screen(["qid", "question"], rows(MINTED_ID), POLICY, minted=minted).report.findings == ()
    # the same value in another column is still screened
    other = [{"qid": "q1", "question": MINTED_ID}]
    assert [
        f.column for f in screen(["qid", "question"], other, POLICY, minted=minted).report.blocked
    ] == ["question"]
    # a value that only CONTAINS the minted id is still screened
    longer = rows(MINTED_ID + " 4111 1111 1111 1111")
    assert screen(["qid", "question"], longer, POLICY, minted=minted).report.blocked
    # another card-like id in the same column, not minted, is still screened
    assert screen(
        ["qid", "question"],
        rows(MINTED_ID, "fb-" + adversarial(CARD_RUNS[0], "1" * 24)),
        POLICY,
        minted=minted,
    ).report.blocked


def test_the_exemption_never_covers_a_genuine_card_number_or_free_text():
    for value in (
        "4111111111111111",
        "4111 1111 1111 1111",
        "my card is 4111-1111-1111-1111",
        "fb-4111111111111111",
    ):
        with pytest.raises(ValueError, match="server-minted"):
            ServerMinted(cells={"qid": frozenset({value})})
        with pytest.raises(ValueError, match="server-minted"):
            ServerMinted(filename_tokens=frozenset({value}))


def test_a_filename_token_exempts_only_spans_inside_it():
    token = "tr-" + ID
    minted = ServerMinted(filename_tokens=frozenset({token}))
    clean = screen(["qid"], [{"qid": "q1"}], POLICY, f"capitals-{token}.jsonl", minted=minted)
    assert clean.report.findings == () and clean.filename == f"capitals-{token}.jsonl"
    leaked = screen(
        ["qid"], [{"qid": "q1"}], POLICY, f"4111111111111111-{token}.jsonl", minted=minted
    )
    assert [f.category for f in leaked.report.blocked] == [Category.PAYMENT_CARD]
    # a card that starts inside the token and runs past it is not inside it: still blocked
    tail = "tr-" + "a" * 16 + "41111111"
    straddle = f"capitals-{tail}11111111.jsonl"  # the digit run is 4111 1111 1111 1111
    minted_tail = ServerMinted(filename_tokens=frozenset({tail}))
    out = screen(["qid"], [{"qid": "q1"}], POLICY, straddle, minted=minted_tail)
    assert [f.category for f in out.report.blocked] == [Category.PAYMENT_CARD]


def real_feedback_id(i: int) -> str:
    """A feedback id exactly as the server mints it, for the inference id number ``i``."""
    identity = {
        "record_schema": FEEDBACK_SCHEMA,
        "inference_id": f"inf-{i:024x}",
        "expected": {"answer": "x"},
    }
    return "fb-" + canonical_hash(identity)[:24]


def test_real_minted_ids_hit_the_card_rule_without_the_exemption_and_never_with_it():
    """The real false-positive rate, from real hashes (deterministic inference ids): about one
    feedback id in 10,500. Ids 16348, 46803, 47515 and 51991 below 60,000 hit it."""
    ids = [real_feedback_id(i) for i in range(60_000)]
    data = rows(*ids)
    without = screen(["qid", "question"], data, POLICY).report
    (card,) = [f for f in without.findings if f.category is Category.PAYMENT_CARD]
    assert card.column == "qid" and card.values == 4 and card.first_row == 16348 + 1
    with_minted = screen(
        ["qid", "question"], data, POLICY, minted=ServerMinted(cells={"qid": frozenset(ids)})
    ).report
    assert with_minted.findings == ()


def test_random_minted_ids_never_hit_with_the_exemption():
    """Repeated runs with random inference ids, as in production."""
    for _ in range(5):
        ids = [
            "fb-"
            + canonical_hash(
                {
                    "record_schema": FEEDBACK_SCHEMA,
                    "inference_id": "inf-" + secrets.token_hex(12),
                    "expected": {"answer": "x"},
                }
            )[:24]
            for _ in range(5_000)
        ]
        minted = ServerMinted(cells={"qid": frozenset(ids)})
        assert (
            screen(["qid"], [{"qid": v} for v in ids], POLICY, minted=minted).report.findings == ()
        )


# -- the dataset service: user data keeps the full screen --------------------------------------
@pytest.fixture
def service(tmp_path):
    svc = DatasetService(
        SQLiteDatasetRepository(tmp_path / "m.sqlite3"), LocalBlobStore(tmp_path / "b")
    )
    return svc, svc.create_project(NewProject(name="p")).project_id


def jsonl(*records: dict) -> bytes:
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode()


REGISTER = RegisterDataset(
    dataset_id="ds",
    name="d",
    input_columns=("question",),
    target_columns=("answer",),
    row_ids="column",
    id_column="qid",
)


def test_a_user_upload_with_a_card_like_id_is_still_refused(service):
    svc, project = service
    data = jsonl({"qid": MINTED_ID, "question": "q", "answer": "a"})
    with pytest.raises(IngestError) as exc:
        svc.upload(project, data, "jsonl")  # no exemption: the API path
    assert exc.value.code == "sensitive_data_blocked"


def test_a_user_card_number_next_to_minted_ids_is_still_refused(service):
    svc, project = service
    minted = ServerMinted(cells={"qid": frozenset({MINTED_ID})})
    data = jsonl({"qid": MINTED_ID, "question": "card 4111 1111 1111 1111", "answer": "a"})
    with pytest.raises(IngestError) as exc:
        svc.upload(project, data, "jsonl", minted=minted)
    assert exc.value.code == "sensitive_data_blocked" and exc.value.details["column"] == "question"


def test_register_checks_row_ids_with_the_same_exemption_only(service):
    svc, project = service
    minted = ServerMinted(cells={"qid": frozenset({MINTED_ID})})
    data = jsonl({"qid": MINTED_ID, "question": "q", "answer": "a"})
    upload, _ = svc.upload(project, data, "jsonl", minted=minted)
    with pytest.raises(IngestError) as exc:
        svc.register(upload.upload_id, REGISTER)  # without it: the id is card-like
    assert exc.value.code == "sensitive_row_ids"
    record, _ = svc.register(upload.upload_id, REGISTER, minted=minted)
    assert svc.repo.get_row_ids("ds", record.dataset_version) == (MINTED_ID,)
