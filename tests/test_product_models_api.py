"""``GET /api/v1/models`` and the dataset identity on job views: what the live UI needs to
configure an experiment and find a project's experiments (Phase 2 live UI integration).

The registry entry carries an endpoint and the server holds a provider credential; neither may
reach a response. Jobs are workspace-scoped, so a job view names the dataset (and task) it runs
over, and a client finds a project's jobs through the project's datasets.
"""

from __future__ import annotations

from pathlib import Path

from api.product import build_api
from runtime.prompts import PROMPT_TEMPLATE_VERSION
from tests.dogfood_support import (
    DATASET_ID,
    FIXTURE,
    PROVIDER_KEY,
    REGISTER,
    SPLIT_PLAN,
    call,
    code,
    entry_for,
    product,
    write_registry,
)

ENDPOINT = "http://10.11.12.13:9999/internal-only/v1"


def _world(tmp_path: Path):
    registry = tmp_path / "registry.json"
    entry = entry_for(ENDPOINT)
    write_registry(registry, entry)
    r = product(tmp_path / "data", registry)
    ws, _, _, key = r.create_workspace("Models", "models@example.test")
    return r, key, entry, registry


def test_models_lists_the_registry_without_endpoint_or_credential(tmp_path):
    r, key, entry, _ = _world(tmp_path)
    status, body = call(r, key, "GET", "models")
    assert status == 200, body
    (model,) = body["models"]
    assert model == {
        "name": entry.name,
        "provider": entry.provider,
        "adapter": entry.adapter,
        "model_id": entry.model_id,
        "revision": entry.revision,
        "model_hash": entry.model_hash,
        "context_window": None,
        "capabilities": [c.value for c in entry.capabilities],
        "pricing": entry.pricing.model_dump(mode="json"),
        "enabled": True,
    }
    assert ENDPOINT not in repr(body) and PROVIDER_KEY not in repr(body)


def test_models_is_re_read_needs_auth_and_fails_closed_without_a_registry(tmp_path):
    r, key, entry, registry = _world(tmp_path)
    assert code(call(r, None, "GET", "models")) == (401, "unauthenticated")
    # re-read on every request: a disabled entry is reported as such
    write_registry(registry, entry.model_copy(update={"enabled": False}))
    assert call(r, key, "GET", "models")[1]["models"][0]["enabled"] is False
    # a read-only key may list models (a GET is a read)
    status, made = call(r, key, "POST", "workspace/keys", {"name": "ro", "scopes": ["read"]})
    assert status == 201, made
    assert call(r, made["secret"], "GET", "models")[0] == 200
    bare = build_api(tmp_path / "bare")
    _, _, _, bare_key = bare.create_workspace("Bare", "bare@example.test")
    assert code(call(bare, bare_key, "GET", "models")) == (503, "experiment_backend_unavailable")


def test_job_views_name_their_dataset_version_and_task(tmp_path):
    r, key, entry, _ = _world(tmp_path)
    project = call(r, key, "POST", "projects", {"name": "Order desk"})[1]
    up = call(
        r, key, "POST", f"projects/{project['project_id']}/uploads?format=csv", FIXTURE.read_bytes()
    )[1]
    version = call(r, key, "POST", f"uploads/{up['upload_id']}/register", REGISTER)[1]
    splits = call(r, key, "POST", f"datasets/{DATASET_ID}/versions/1/splits", SPLIT_PLAN)[1]
    spec = version["spec"]
    body = {
        "dataset_id": DATASET_ID,
        "dataset_version": 1,
        "splits_hash": splits["splits_hash"],
        "contract": {
            "task_id": "desk-task",
            "contract_version": 1,
            "task_type": "question_answering",
            "instructions": "Name the destination city.",
            "input_schema": {
                "fields": [
                    {"name": "question", "type": "string"},
                    {"name": "order_record", "type": "string"},
                ]
            },
            "output_schema": {"fields": [{"name": "answer", "type": "string"}]},
            "dataset": spec,
            "evaluation": {"evaluator": "exact_match"},
            # the runtime enforces per-run caps, so a contract must state them
            "constraints": {
                "maximum_tokens_per_example": 20_000,
                "maximum_wall_time_s": 120.0,
                "maximum_tool_calls": 20,
                "maximum_retries": 2,
                "maximum_model_calls": 4,
            },
        },
        "plan": {
            "model": {"provider": entry.provider, "model": entry.model_id},
            "expected_model_hash": entry.model_hash,
            "expected_prompt_version": PROMPT_TEMPLATE_VERSION,
            "budget": {"max_candidate_evaluations": 2},
            "seeds": [0],
            "strategies": ["fixed"],
        },
    }
    status, job = call(r, key, "POST", "experiments", body)
    assert status == 201, job
    assert (job["dataset_id"], job["dataset_version"], job["task_id"]) == (
        DATASET_ID,
        1,
        "desk-task",
    )
    (summary,) = call(r, key, "GET", "experiments")[1]["jobs"]
    assert (summary["dataset_id"], summary["dataset_version"], summary["task_id"]) == (
        DATASET_ID,
        1,
        "desk-task",
    )
    assert call(r, key, "GET", f"experiments/{job['job_id']}")[1] == job
