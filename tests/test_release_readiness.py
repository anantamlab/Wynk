"""The release-readiness suite's own logic: statuses, harness field selection, secret scan."""

from __future__ import annotations

from pathlib import Path

from tests import release_readiness as rr


def test_overall_is_fail_then_blocked_then_pass():
    assert rr.overall({"a": {"status": "PASS"}, "b": {"status": "FAIL"}}) == "FAIL"
    assert rr.overall({"a": {"status": "PASS"}, "b": {"status": "BLOCKED"}}) == "BLOCKED"
    assert rr.overall({"a": {"status": "PASS"}, "b": {"status": "NOT_RUN"}}) == "BLOCKED"
    assert rr.overall({"a": {"status": "PASS"}}) == "PASS"


def test_the_live_and_production_gates_are_always_blocked_and_every_gate_has_tests():
    assert set(rr.BLOCKED) == {"live_provider_smoke", "production_deployment"}
    root = Path(rr.ROOT)
    for gate in rr.GATES:
        assert gate.parts, gate.name
        for part in gate.parts:
            for path in part.paths:
                assert (root / path).is_file(), (gate.name, path)


def test_stable_fields_keep_verdicts_digests_and_counts_only():
    report = {
        "verdict": "PASS",
        "stages": [{"name": "x", "assertions": 3, "seconds": 1.2, "digest": "d"}],
        "requests": {"count": 9},
        "tmp_dir": "C:/tmp/x",
    }
    assert rr.stable_fields(report) == {
        "verdict": "PASS",
        "stages[0].assertions": 3,
        "stages[0].digest": "d",
        "requests.count": 9,
    }


def test_the_secret_scan_finds_a_planted_value_and_names_no_value(tmp_path, monkeypatch):
    monkeypatch.setenv("WYNK_DATA_KEYS", "kid:" + "Z" * 43)
    (tmp_path / "clean.log").write_text("all good", "utf-8")
    assert rr.scan_for_secrets(tmp_path)["status"] == "PASS"
    (tmp_path / "leak.log").write_text("key " + "Z" * 43, "utf-8")
    (tmp_path / "card.json").write_text('{"v": "4111 1111 1111 1111"}', "utf-8")
    found = rr.scan_for_secrets(tmp_path)
    assert found["status"] == "FAIL" and len(found["hits"]) == 2
    assert all("Z" * 43 not in h and "4111" not in h for h in found["hits"])


def test_a_gate_with_no_test_selected_fails(tmp_path):
    empty = rr.Gate("empty", "nothing", (rr.Part(("tests/test_release_readiness.py",), "nomatch"),))
    result = rr.run_gate(empty, tmp_path, __import__("sys").executable)
    assert result["status"] == "FAIL" and result["passed"] == 0
