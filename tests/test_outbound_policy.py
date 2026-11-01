"""F2 outbound data policy: what a workspace lets reach a model provider.

Every value here is SYNTHETIC (test card numbers, made-up tokens). The integration tests drive
the real multi-tenant wiring with a counting test double as the model, so "nothing was sent" is
an observed fact (the double saw no request), not an assumption.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from api import tenancy
from api.product import build_api
from api.tenancy import TenantRouter, outbound_policy_command
from core.models import ModelRegistry
from experiments.deployment import InferenceRuntime
from ingestion.outbound import (
    DEFAULT_POLICY,
    UNSCREENED,
    OutboundAction,
    OutboundMode,
    OutboundPolicy,
    blocked,
    refusal_details,
    screen_values,
)
from ingestion.sensitive import Category
from runtime.model_client import RegisteredModelClient
from tests.test_champion_inference import ENTRY, PassageDouble, maf
from tests.test_champion_promotion import EVERYTHING
from tests.test_production_monitoring import AnyStamped, DataRuntime, inputs_for
from tests.test_tenancy import LINEAGE, call, deploy, workspace

# synthetic values, one per category (none is a real credential or person)
CARD = "4111 1111 1111 1111"  # the well-known Visa test number
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB = "ghp_" + "a" * 36
SSN = "123-45-6789"
IBAN = "GB82 WEST 1234 5698 7654 32"
EMAIL = "someone@example.test"
PHONE = "+44 20 7946 0958"
SECRETS = (CARD, AWS, GITHUB, SSN, IBAN)


def cats(values: dict[str, Any], policy: OutboundPolicy = DEFAULT_POLICY) -> set[str]:
    return {f.category.value for f in blocked(screen_values(values, policy))}


# -- the policy and the screen (no I/O) -----------------------------------------------------------
def test_the_default_blocks_credentials_cards_ssn_and_iban_and_allows_contact_data():
    for value, category in (
        (CARD, "payment_card"),
        (AWS, "aws_access_key"),
        (GITHUB, "github_token"),
        (SSN, "us_ssn"),
        (IBAN, "iban"),
    ):
        assert cats({"q": f"please check {value} now"}) == {category}, category
    found = screen_values({"q": f"mail {EMAIL} or call {PHONE}"}, DEFAULT_POLICY)
    assert {(f.category.value, f.action) for f in found} == {
        ("email", OutboundAction.ALLOW),
        ("phone", OutboundAction.ALLOW),
    }
    assert not blocked(found)


def test_nested_values_integers_and_keys_are_screened_and_named_by_their_top_field():
    found = blocked(
        screen_values(
            {"a": {"deep": [1, {"x": CARD}]}, "b": 4111111111111111, "c": {AWS: "v"}},
            DEFAULT_POLICY,
            prefix="inputs.",
        )
    )
    assert {(f.field, f.category.value) for f in found} == {
        ("inputs.a", "payment_card"),
        ("inputs.b", "payment_card"),
        ("inputs.c", "aws_access_key"),
    }


@pytest.mark.parametrize(
    "text",
    [
        "order 1234567812345670 shipped",  # Luhn-valid, but no card network prefix
        "invoice 4111111111111112",  # a card prefix, Luhn-invalid
        "call 555-0100 today",  # a local number without +
        "ssn-like 000-12-3456",  # never-issued SSN area
        "tracking 1Z999AA10123456784",
        "the capital of France is Paris",
    ],
)
def test_false_positives_are_not_blocked(text):
    assert cats({"q": text}) == set()


@pytest.mark.parametrize(
    "text",
    [
        "4111.1111.1111.1111",  # an unusual separator
        "four one one one, one one one one, one one one one, one one one one",
        "my password is hunter2",  # free text has no fixed format
        "Jane Doe, 1 Main Street, Springfield",  # names and addresses
    ],
)
def test_format_limits_values_without_a_fixed_format_are_not_detected(text):
    """Documented recall limits (docs/privacy_retention.md): sent as given, never claimed."""
    assert cats({"q": text}) == set()


def test_off_screens_nothing_and_the_policy_is_total_and_never_allows_credentials():
    assert screen_values({"q": CARD + " " + AWS}, UNSCREENED) == []
    assert UNSCREENED.mode is OutboundMode.OFF and DEFAULT_POLICY.mode is OutboundMode.SCREEN
    actions = dict(DEFAULT_POLICY.actions)
    actions.pop(Category.PAYMENT_CARD)
    with pytest.raises(ValueError, match="no action for"):
        OutboundPolicy(actions=actions)
    with pytest.raises(ValueError, match="credentials cannot be allowed"):
        OutboundPolicy.parse("screen:aws_access_key=allow")
    # off may carry any actions: it screens nothing either way
    assert OutboundPolicy.parse("off").mode is OutboundMode.OFF


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("screen:email=redact", "never redacts"),
        ("screen:nosuch=block", "unknown outbound category"),
        ("screen:email=maybe", "unknown outbound category or action"),
        ("screen:email", "not category=action"),
        ("strict", "'off' or 'screen'"),
    ],
)
def test_a_bad_policy_spec_is_refused(spec, message):
    with pytest.raises(ValueError, match=message):
        OutboundPolicy.parse(spec)


def test_parse_overrides_and_the_hash_versions_the_policy():
    strict = OutboundPolicy.parse("screen:email=block, phone=block")
    assert strict.actions[Category.EMAIL] is OutboundAction.BLOCK
    assert strict.actions[Category.PAYMENT_CARD] is OutboundAction.BLOCK
    assert OutboundPolicy.parse("screen") == DEFAULT_POLICY
    assert strict.policy_hash != DEFAULT_POLICY.policy_hash
    assert OutboundPolicy.model_validate_json(strict.model_dump_json()) == strict


def test_a_refusal_never_holds_the_value_or_a_part_of_it():
    for value in SECRETS:
        found = blocked(screen_values({"question": f"x {value} y"}, DEFAULT_POLICY))
        details = refusal_details(found, DEFAULT_POLICY)
        text = json.dumps(details) + json.dumps([f.model_dump(mode="json") for f in found])
        compact = value.replace(" ", "").replace("-", "")
        assert value not in text and compact not in text
        assert details["fields"] == "question"


# -- through the API ---------------------------------------------------------------------------
class Counting:
    """One shared test double model, so every provider request is counted."""

    def __init__(self) -> None:
        self.double = PassageDouble()

    @property
    def requests(self) -> int:
        return len(self.double.requests)


def counting_router(root: Path, model: Counting) -> TenantRouter:
    inference = InferenceRuntime(
        registry=lambda: ModelRegistry(entries=(ENTRY,)),
        client=lambda entry: RegisteredModelClient(entry, model.double),
    )
    return build_api(
        root,
        runtime=lambda service: DataRuntime(AnyStamped(EVERYTHING), service.blobs),
        inference=inference,
    )


def serve_world(tmp_path: Path) -> tuple[TenantRouter, str, str, dict[str, Any], Counting]:
    """One workspace through the full deploy flow, its model a counting test double."""
    model = Counting()
    r = counting_router(tmp_path / "data", model)
    ws, key = workspace(r, "Alpha", "a@alpha.test")
    return r, ws, key, deploy(r, ws, key), model


@pytest.fixture
def served(tmp_path):
    return serve_world(tmp_path)


def invoke(r, key, inputs, **headers):
    return call(r, key, "POST", f"deployments/{LINEAGE}/invoke", {"inputs": inputs}, **headers)


def inference_rows(r, ws) -> int:
    db = r.root / "workspaces" / ws / "deployments.sqlite3"
    with sqlite3.connect(db) as conn:
        return sum(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("inference_records", "inference_invocations", "inference_outputs")
        )


def audit_events(r, key, action) -> list[dict[str, Any]]:
    s, body = call(r, key, "GET", "audit")
    assert s == 200, body
    return [e for e in body["events"] if e["action"] == action]


@maf
def test_a_new_workspace_blocks_a_card_before_any_provider_request_or_record(served):
    r, ws, key, a, model = served
    assert r.outbound_policy(ws) == (DEFAULT_POLICY, 1)
    before, rows = model.requests, inference_rows(r, ws)
    inputs = inputs_for("France", "Paris")
    inputs["question"] += f" My card is {CARD}."
    for headers in ({}, {"Idempotency-Key": "k-card-1"}):
        s, body = invoke(r, key, inputs, **headers)
        assert (s, body["error"]["code"]) == (422, "outbound_data_blocked"), body
        details = body["error"]["details"]
        assert (details["categories"], details["fields"]) == ("payment_card", "question")
        assert details["policy_hash"] == DEFAULT_POLICY.policy_hash
        assert CARD not in json.dumps(body) and CARD.replace(" ", "") not in json.dumps(body)
    assert model.requests == before  # nothing reached the model
    assert inference_rows(r, ws) == rows  # no inference record, no idempotency binding
    # the key is not bound to the refusal: the same key with clean inputs serves normally
    s, ok = invoke(r, key, inputs_for("France", "Paris"), **{"Idempotency-Key": "k-card-1"})
    assert s == 200 and model.requests > before, ok
    events = audit_events(r, key, "security.outbound_blocked")
    assert len(events) == 2 and all(e["result"] == "refused" for e in events)
    assert events[0]["details"]["categories"] == "payment_card"
    assert CARD not in json.dumps(events)


@maf
def test_secrets_are_blocked_contact_data_is_sent_and_an_opt_in_blocks_it(served):
    r, ws, key, a, model = served
    for value in (AWS, GITHUB, SSN, IBAN):
        inputs = inputs_for("France", "Paris")
        inputs["passage"] += f" ref {value}"
        s, body = invoke(r, key, inputs)
        assert (s, body["error"]["code"]) == (422, "outbound_data_blocked"), body
        assert body["error"]["details"]["fields"] == "passage"
        assert value not in json.dumps(body)
    contact = inputs_for("France", "Paris")
    contact["question"] += f" Reply to {EMAIL}."
    assert invoke(r, key, contact)[0] == 200  # allowed by default
    out = outbound_policy_command(r, ws, "screen:email=block")
    assert out["revision"] == 2 and out["policy"]["actions"]["email"] == "block"
    s, body = invoke(r, key, contact)
    assert (s, body["error"]["code"]) == (422, "outbound_data_blocked"), body
    assert EMAIL not in json.dumps(body)
    # the same policy again appends nothing
    assert outbound_policy_command(r, ws, "screen:email=block")["revision"] == 2
    sets = [e for e in audit_events(r, key, "outbound_policy.set") if e["result"] == "ok"]
    assert [e["details"]["created"] for e in sets] == [True, False]


@maf
def test_feedback_values_are_screened_and_refused_without_being_stored(served):
    r, ws, key, a, model = served
    inputs = inputs_for("France", "Paris")
    s, inf = invoke(r, key, inputs)
    assert s == 200, inf
    s, body = call(
        r,
        key,
        "POST",
        f"inferences/{inf['inference_id']}/feedback",
        {"inputs": inputs, "output": inf["output"], "expected": {"answer": f"Paris {CARD}"}},
    )
    assert (s, body["error"]["code"]) == (422, "outbound_data_blocked"), body
    assert body["error"]["details"]["fields"] == "expected.answer"
    assert CARD not in json.dumps(body)
    db = r.root / "workspaces" / ws / "monitoring.sqlite3"
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT count(*) FROM feedback").fetchone()[0]
    assert n == len(a["feedback"])  # nothing was stored
    s, ok = call(
        r,
        key,
        "POST",
        f"inferences/{inf['inference_id']}/feedback",
        {"inputs": inputs, "output": inf["output"], "expected": {"answer": "Paris"}},
    )
    assert s == 201, ok


@maf
def test_a_workspace_from_before_the_policy_stays_unscreened_until_an_operator_opts_in(served):
    r, ws, key, a, model = served
    identity = r.root / "identity.sqlite3"
    conn = sqlite3.connect(identity)
    try:  # simulate an identity store written before outbound policies existed
        conn.execute("DROP TRIGGER outbound_policies_no_delete")
        conn.execute("DELETE FROM outbound_policies WHERE workspace_id=?", (ws,))
        conn.commit()
    finally:
        conn.close()
    assert r.outbound_policy(ws) == (UNSCREENED, None)
    shown = outbound_policy_command(r, ws, None)
    assert shown["revision"] is None and shown["source"].startswith("legacy")
    inputs = inputs_for("France", "Paris")
    inputs["question"] += f" card {CARD}"
    assert invoke(r, key, inputs)[0] == 200  # exactly as before: no silent behaviour change
    assert outbound_policy_command(r, ws, "screen")["revision"] == 1
    s, body = invoke(r, key, inputs)
    assert (s, body["error"]["code"]) == (422, "outbound_data_blocked"), body


@maf
def test_a_stored_policy_that_does_not_parse_fails_closed(served):
    r, ws, key, a, model = served
    conn = sqlite3.connect(r.root / "identity.sqlite3")
    try:
        conn.execute("DROP TRIGGER outbound_policies_immutable")
        conn.execute('UPDATE outbound_policies SET policy_json=\'{"mode": "open"}\'')
        conn.commit()
    finally:
        conn.close()
    before = model.requests
    s, body = invoke(r, key, inputs_for("France", "Paris"))
    assert (s, body["error"]["code"]) == (500, "internal_error"), body  # closed, never off
    assert model.requests == before


def test_policy_revisions_are_append_only(tmp_path):
    r = counting_router(tmp_path / "data", Counting())
    ws, _ = workspace(r, "Alpha", "a@alpha.test")
    conn = sqlite3.connect(r.root / "identity.sqlite3")
    try:
        for sql in ("UPDATE outbound_policies SET set_by='x'", "DELETE FROM outbound_policies"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()
    assert r.outbound_policy(ws)[1] == 1
    with pytest.raises(tenancy.TenantIntegrityError):
        r.set_outbound_policy("ws-" + "0" * 20, DEFAULT_POLICY, "x")
