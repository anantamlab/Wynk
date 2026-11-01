"""Phase 5 metadata audit: a feedback's ``actor`` / ``source`` are caller-asserted, so they are
(1) held to a strict allowlist, (2) sealed with the row data, never kept as plaintext, and
(3) still reported as untrusted (``metadata_trusted: false``); the authenticated principal is
the server's, not the caller's. Every refusal names the rule, never the value.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from experiments.monitoring import METADATA_VALUE, FeedbackRequest
from tests.test_champion_inference import maf
from tests.test_feedback_sealing import monitoring_db, raw_bytes, rows
from tests.test_outbound_policy import AWS, CARD, GITHUB, serve_world
from tests.test_production_monitoring import inputs_for
from tests.test_tenancy import LINEAGE, call

INPUTS = {"question": "q", "passage": "p"}


@pytest.fixture
def served(tmp_path):
    return serve_world(tmp_path)


def refused(**kw) -> str:
    with pytest.raises(ValidationError) as info:
        FeedbackRequest(inputs=INPUTS, **kw)
    return json.dumps([{k: e[k] for k in ("loc", "msg")} for e in info.value.errors()])


@pytest.mark.parametrize(
    "actor",
    [
        '=HYPERLINK("http://x")',  # spreadsheet formula injection
        "+cmd",
        "-2+3",
        "@SUM(A1)",
        "<script>alert(1)</script>",
        "a\nb",  # log line injection
        "a\rb",
        "tab\there",
        'quote"d',
        "back\\slash",
        "nul\x00",
        "‮evil",  # bidi override
        "naïve",  # non-ASCII is refused, not normalised
        " padded",
        "padded ",
        "x" * 201,
        "${jndi:ldap://x}",
    ],
)
def test_a_malicious_or_malformed_actor_is_refused(actor):
    message = refused(actor=actor)
    assert actor not in message


@pytest.mark.parametrize("secret", [AWS, GITHUB, "sk-" + "A1b2" * 12])
def test_a_credential_in_metadata_is_refused_without_echo(secret):
    for kw in ({"actor": f"user {secret}"}, {"source": {"app": secret}}):
        message = refused(**kw)
        assert secret not in message and "credential" in message


@pytest.mark.parametrize(
    "source",
    [
        {"App": "x"},  # upper-case key
        {"1app": "x"},
        {"a" * 33: "x"},
        {"a b": "x"},
        {AWS: 5},  # a type error must not name the key
        {AWS.lower(): ["x"]},
        {"app": None},
        {"app": "=1+1"},
        {f"k{i}": "v" for i in range(21)},  # too many keys
    ],
)
def test_malformed_source_is_refused_and_no_key_or_value_is_echoed(source):
    message = refused(source=source)
    assert AWS not in message and AWS.lower() not in message
    assert all(e["loc"] == ["source"] for e in json.loads(message)), message


def test_ordinary_metadata_is_accepted_unchanged():
    ok = FeedbackRequest(
        inputs=INPUTS,
        actor="reviewer:jane.doe@example.test",
        source={"app": "crm-2", "ticket": "#4512", "batch.id": "2026-11-01/7", "k": ""},
    )
    assert ok.actor == "reviewer:jane.doe@example.test"
    assert METADATA_VALUE.fullmatch("a, b = c / d") is not None
    # a card number is not a credential: it passes the allowlist (it is sealed like the rest)
    assert FeedbackRequest(inputs=INPUTS, actor=CARD.replace(" ", "")).actor


@maf
def test_metadata_is_sealed_never_plaintext_and_stays_untrusted(served):
    r, ws, key, a, model = served
    inputs = inputs_for("France", "Paris")
    s, inf = call(r, key, "POST", f"deployments/{LINEAGE}/invoke", {"inputs": inputs})
    assert s == 200, inf
    actor, marker = "spoofed-owner-7Q2", "source-marker-Z9K"
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
            "source": {"app": marker},
        },
    )
    assert s == 201, fb
    db = monitoring_db(r.root, ws)
    blob = raw_bytes(db)
    assert actor.encode() not in blob and marker.encode() not in blob
    for (record,) in rows(db, "SELECT record_json FROM feedback"):
        assert not {"untrusted_metadata", "actor", "source"} & set(json.loads(record))
    # read back: opened from the sealed payload, still flagged untrusted
    s, got = call(r, key, "GET", f"feedback/{fb['feedback_id']}")
    assert s == 200, got
    assert got["untrusted_metadata"] == {"actor": actor, "source": {"app": marker}}
    assert got["metadata_trusted"] is False
    # the trust boundary: who submitted it is the server-resolved principal, never ``actor``
    principal = call(r, key, "GET", "workspace")[1]["principal"]
    assert got["authenticated_principal"]["user_id"] == principal["user_id"] != actor


@maf
def test_a_refused_metadata_value_is_not_echoed_by_the_api(served):
    r, ws, key, a, model = served
    inputs = inputs_for("France", "Paris")
    s, inf = call(r, key, "POST", f"deployments/{LINEAGE}/invoke", {"inputs": inputs})
    assert s == 200, inf
    for extra in ({"actor": f"me {GITHUB}"}, {"source": {GITHUB.lower(): 1}}):
        s, body = call(
            r,
            key,
            "POST",
            f"inferences/{inf['inference_id']}/feedback",
            {"inputs": inputs, "output": inf["output"], **extra},
        )
        assert (s, body["error"]["code"]) == (400, "invalid_request"), body
        assert GITHUB not in json.dumps(body) and GITHUB.lower() not in json.dumps(body)
