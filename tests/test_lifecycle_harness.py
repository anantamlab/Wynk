"""CI entry point of the Phase 3 lifecycle validation harness (``tests/lifecycle_harness.py``).

PROVIDER MODE: FAKE. These tests run the real product API over HTTP against the local fake
model provider (``tests.dogfood_support.FakeProvider``); no hosted provider is called. The
live-provider smoke test is BLOCKED (no spending authorization) and is reported, never run.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from tests import lifecycle_harness as harness

pytest.importorskip("agent_framework")  # the real runtime executes workflows with MAF


def test_the_whole_lifecycle_passes_twice_with_identical_verdicts_and_digests(tmp_path):
    out = tmp_path / "reports"
    report = harness.run_harness(runs=2, out=out)

    failures = [
        (r["run"], s["name"], s["status"], s["failure"] or s["reason"])
        for r in report["runs"]
        for s in r["stages"]
        if s["status"] != "PASS" and s["name"] in harness.LIFECYCLE
    ]
    assert failures == []
    assert report["verdict"] == "PASS"
    assert report["determinism"] == {
        "runs": 2,
        "same_verdicts": True,
        "same_digests": True,
        "same_assertion_counts": True,
        "deterministic": True,
    }
    for run in report["runs"]:
        names = [s["name"] for s in run["stages"]]
        assert names == [*harness.LIFECYCLE, *harness.BLOCKED_STAGES]
        live = run["stages"][-1]
        assert (live["name"], live["status"]) == ("live_provider_smoke", "BLOCKED")
        assert "spending authorization" in live["reason"]
        assert run["provider_mode"] == "fake" and run["provider_requests_total"] > 0
        assert run["non_loopback_connections_refused"] == 0
        assert run["teardown"]["clean"] and run["teardown"]["store_removed"]
        assert all(s["assertions"] > 0 for s in run["stages"] if s["status"] == "PASS")
    # identical totals: the same number of fake-provider requests in both runs
    assert len({r["provider_requests_total"] for r in report["runs"]}) == 1

    # the reports: machine-readable JSON and Markdown, labelled as a fake-provider run
    data = json.loads((out / "lifecycle-report.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == harness.SCHEMA_VERSION
    assert data["provider_mode"] == "fake" and data["simulated"] is True
    assert data["commit"] and data["python"]
    md = (out / "lifecycle-report.md").read_text(encoding="utf-8")
    assert "Provider mode: FAKE" in md and "live_provider_smoke | BLOCKED | BLOCKED" in md
    for text in (json.dumps(data), md):
        assert "wynk_sk_" not in text and harness.PROVIDER_KEY not in text


def test_a_failed_stage_blocks_the_rest_and_the_store_is_still_removed(monkeypatch):
    def broken(self, st):
        st.check(False, "deliberately broken split")

    monkeypatch.setattr(harness.Run, "split", broken)
    result = harness.run_once(1)
    status = {s.name: s.status for s in result.stages}
    assert status["upload"] == status["register"] == "PASS"
    assert status["split"] == "FAIL"
    assert "deliberately broken split" in next(s.failed for s in result.stages if s.name == "split")
    later = harness.LIFECYCLE[harness.LIFECYCLE.index("split") + 1 :]
    assert {status[n] for n in later} == {"BLOCKED"}
    assert result.teardown["clean"] and result.teardown["store_removed"]
    assert result.report()["verdict"] == "FAIL"
    assert harness.build_report([result])["verdict"] == "FAIL"


def test_the_guard_refuses_every_connection_beyond_loopback():
    guard, real = harness.LoopbackGuard(), socket.socket.connect
    with guard.installed():
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("203.0.113.7", 443), timeout=1)  # TEST-NET-3, never routed
    assert guard.refused == ["203.0.113.7"]
    assert socket.socket.connect is real  # restored


def test_a_credential_never_reaches_a_report():
    harness.scrub_check("all clear", ["sk-secret"])
    for leaked in ("token sk-secret here", "key wynk_sk_0123"):
        with pytest.raises(AssertionError, match="credential"):
            harness.scrub_check(leaked, ["sk-secret"])


def test_the_harness_environment_is_restored(monkeypatch):
    monkeypatch.setenv("WYNK_MODEL_REGISTRY", "/operator/registry.json")
    monkeypatch.setenv("WYNK_WEBSEARCH_API_KEY", "operator-search-key")
    with harness.harness_environment():
        assert "WYNK_MODEL_REGISTRY" not in os.environ
        assert "WYNK_WEBSEARCH_API_KEY" not in os.environ
        assert os.environ["WYNK_DATA_KEYS"] == harness.HARNESS_DATA_KEYS
    assert os.environ["WYNK_MODEL_REGISTRY"] == "/operator/registry.json"
    assert os.environ["WYNK_WEBSEARCH_API_KEY"] == "operator-search-key"


def test_reports_go_to_a_git_ignored_directory():
    ignored = (Path(harness.REPO) / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".wynk-validation/" in ignored
    assert harness.DEFAULT_OUT.relative_to(harness.REPO).parts[0] == ".wynk-validation"
