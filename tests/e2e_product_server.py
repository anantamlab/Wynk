"""TEST ONLY: the product API the browser E2E suite (``ui/playwright.config.ts``) runs against.

    python -m tests.e2e_product_server --data-dir DIR --port 8790 --max-upload-mb 1

This is ``python -m api.product`` with exactly one difference: the model registry points at
local fake model servers (``tests.dogfood_support.FakeProvider``, an OpenAI-compatible
chat-completions server that answers the order-desk prompts deterministically) started in this
process. Everything else - the multi-tenant router, the durable job worker, held-out promotion,
deployment and invocation - is the real product wiring (``api.product.main``). No request can
reach a hosted provider: the registry lists only the local fakes, the provider key is a fake
one, and an operator's websearch / registry settings are removed from the environment.

Two registry entries:
  ``e2e-desk``       answers at once
  ``e2e-desk-slow``  waits ``SLOW_S`` seconds per request, so a browser test can watch a job
                     run and cancel it before it finishes

``WYNK_E2E_PROVIDER_LOG=FILE`` appends every request a fake model server receives to FILE (one
JSON line each), so a browser test can count provider executions (for example: a same-key
replay makes none).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tests.dogfood_support import PROVIDER_KEY, FakeProvider, entry_for, write_registry

SLOW_S = 1.5


def main(argv: list[str] | None = None) -> None:
    for name in list(os.environ):  # never a real search provider or registry
        if name.startswith("WYNK_WEBSEARCH_") or name == "WYNK_MODEL_REGISTRY":
            del os.environ[name]
    os.environ["WYNK_MODEL_API_KEY"] = PROVIDER_KEY  # the fakes' (public, fake) key

    log = os.environ.get("WYNK_E2E_PROVIDER_LOG")
    lock = threading.Lock()

    def record(body: dict[str, Any]) -> None:
        if log:
            with lock, open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(body, sort_keys=True) + "\n")

    def slow(body: dict[str, Any]) -> None:
        record(body)
        time.sleep(SLOW_S)

    fast = FakeProvider(on_request=record).start()
    slow_provider = FakeProvider(on_request=slow).start()
    registry = Path(tempfile.mkdtemp(prefix="wynk-e2e-registry-")) / "registry.json"
    write_registry(
        registry,
        entry_for(fast.endpoint, name="e2e-desk", model_id="e2e-desk-model"),
        entry_for(slow_provider.endpoint, name="e2e-desk-slow", model_id="e2e-desk-slow-model"),
    )
    from api.product import main as product_main

    product_main([*(sys.argv[1:] if argv is None else argv), "--model-registry", str(registry)])


if __name__ == "__main__":
    main()
