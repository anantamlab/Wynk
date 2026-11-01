"""Real experiment / promotion / deployment / inference responses, shared with the UI's tests.

``ui/src/api/contract/fixtures/product-api.lifecycle.v1.json`` holds one response of every
lifecycle route the live UI reads, captured from the REAL product wiring (the dogfood world:
``build_api`` + registry runtime + a local deterministic fake model server, no hosted provider).
The UI's decoders and screens are tested against it (``ui/src/api/lifecycle.test.ts``).

Ids, timestamps and latencies differ between runs, so this test pins the STRUCTURE: every
object's key set, recursively (the first item stands for a list). A field the server adds,
renames or drops fails here until the fixture is regenerated - and then the UI tests run
against the new shape:

    WYNK_UPDATE_UI_FIXTURES=1 python -m pytest tests/test_product_api_ui_lifecycle_fixture.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from tests.dogfood_support import LINEAGE, PROVIDER_KEY, FakeProvider

pytest.importorskip("agent_framework")  # the real runtime executes workflows with MAF

from tests.test_dogfood_lifecycle import NEW_ORDER, World  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "ui" / "src" / "api" / "contract" / "fixtures" / "product-api.lifecycle.v1.json"


def shape(v: Any) -> Any:
    """Key sets, recursively; the first item of a list stands for the list."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
    return None


def trim(v: Any, depth: int = 0) -> Any:
    """Keep the fixture reviewable: at most 3 items per list below the top level."""
    if isinstance(v, dict):
        return {k: trim(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [trim(x, depth + 1) for x in (v if depth < 2 else v[:3])]
    return v


def capture(tmp_path: Path) -> dict[str, Any]:
    provider = FakeProvider().start()
    try:
        w = World(tmp_path, provider)
        _, key = w.workspace("Fixture", "fixture@example.test")
        out: dict[str, Any] = {}

        def ok(name: str, method: str, path: str, body: Any = None, status: int = 200, **h: str):
            s, b = w.call(key, method, path, body, **h)
            assert s == status, (path, s, b)
            out[name] = {"status": s, "body": b}
            return b

        def err(name: str, method: str, path: str, body: Any = None, **h: str):
            s, b = w.call(key, method, path, body, **h)
            assert s >= 400, (path, s, b)
            out[name] = {"status": s, "body": b}

        ok("list_models", "GET", "models")
        data = w.dataset(key)
        ok("create_experiment", "POST", "experiments", w.experiment_body(data), 201)
        job_id = out["create_experiment"]["body"]["job_id"]
        group = w.r.start_workers()
        try:
            deadline = time.monotonic() + 120
            while w.ok(key, "GET", f"experiments/{job_id}")["state"] != "COMPLETED":
                assert time.monotonic() < deadline
                time.sleep(0.1)
        finally:
            group.stop()
        ok("get_experiment", "GET", f"experiments/{job_id}")
        ok("list_experiments", "GET", "experiments")
        ok("get_artifact", "GET", f"experiments/{job_id}/artifact")
        ok("get_provenance", "GET", f"experiments/{job_id}/provenance")
        ok("verify_artifact", "GET", f"experiments/{job_id}/verify")
        err("error_job_not_cancellable", "POST", f"experiments/{job_id}/cancel")
        err("error_champion_not_found", "GET", f"champions/{LINEAGE}")
        promo = ok("promote_experiment", "POST", f"experiments/{job_id}/promote", status=201)
        ok("get_champion", "GET", f"champions/{LINEAGE}")
        champion = promo["record"]["champion"]["champion_id"]
        err("error_deployment_not_found", "GET", f"deployments/{LINEAGE}")
        wv = ok("publish_workflow", "POST", "workflows", {"champion_id": champion}, 201)
        ok("list_workflows", "GET", f"workflows?lineage={LINEAGE}")
        ok("get_deployment", "GET", f"deployments/{LINEAGE}")
        ok(
            "stage_workflow",
            "POST",
            f"workflows/{wv['version_id']}/stage",
            {"expected_revision": 0},
        )
        err(
            "error_stale_deployment",
            "POST",
            f"workflows/{wv['version_id']}/promote",
            {"expected_revision": 0},
        )
        ok(
            "promote_workflow",
            "POST",
            f"workflows/{wv['version_id']}/promote",
            {"expected_revision": 1},
        )
        ok("get_deployment_history", "GET", f"deployments/{LINEAGE}/history")
        invoke = (f"deployments/{LINEAGE}/invoke", {"inputs": NEW_ORDER})
        ok("invoke_production", "POST", *invoke, **{"Idempotency-Key": "fixture-key-1"})
        ok("invoke_production_replay", "POST", *invoke, **{"Idempotency-Key": "fixture-key-1"})
        err(
            "error_idempotency_conflict",
            "POST",
            f"deployments/{LINEAGE}/invoke",
            {"inputs": {**NEW_ORDER, "question": "Another question?"}},
            **{"Idempotency-Key": "fixture-key-1"},
        )
        err("error_invalid_inference_request", "POST", invoke[0], {"inputs": {"question": 1}})
        ok("current_workspace", "GET", "workspace")
        err(  # the key changed between the workspace read and the mutation (api/tenancy.py)
            "error_workspace_mismatch",
            "POST",
            *invoke,
            **{"Idempotency-Key": "fixture-key-2", "Wynk-Expected-Workspace": "ws-another"},
        )
        ok("get_quota", "GET", "workspace/quota")
        out["new_order"] = NEW_ORDER
        assert PROVIDER_KEY not in json.dumps(out)
        return out
    finally:
        provider.stop()


def test_lifecycle_fixture_matches_the_real_product_api(tmp_path):
    captured = trim(capture(tmp_path))
    if os.environ.get("WYNK_UPDATE_UI_FIXTURES"):
        FIXTURE.write_text(json.dumps(captured, indent=1) + "\n", encoding="utf-8", newline="\n")
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert shape(committed) == shape(captured), (
        "the product API's lifecycle responses changed shape; regenerate the UI fixture with "
        "WYNK_UPDATE_UI_FIXTURES=1 and update ui/src/api/contract/lifecycle.ts"
    )
