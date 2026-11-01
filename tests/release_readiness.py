"""Phase 5: the v0.1 release-readiness suite - one report, one status per gate.

    python -m tests.release_readiness --out DIR                      # every local gate
    python -m tests.release_readiness --out DIR --with-e2e           # + the Playwright UI lifecycle
    python -m tests.release_readiness --out DIR --reference R.json   # + a harness reference check

A sibling of ``tests.lifecycle_harness`` (which it runs unchanged, so its digests stay the
regression check of benchmark, optimizer, held-out and provenance behaviour). Every other gate is
a named selection of the repository's own tests, run in a fresh ``pytest`` process with the
deterministic FAKE providers only; the gate passes only if every selected test passes and at
least one ran. Counts come from each run's JUnit XML, never from parsing its console output.

Gates that need something this suite must not have report BLOCKED, never PASS:

    live_provider_smoke    needs a paid provider call and a spending authorization
    production_deployment  needs an operator, real infrastructure and a release decision

``secret_leakage`` scans everything this run wrote (reports, logs, the harness output) for the
test data key and the synthetic secrets the privacy tests use; a hit fails the gate.

Overall: FAIL if any gate failed, else BLOCKED if any gate is blocked, else PASS. A skipped gate
(``--with-e2e`` not given) is NOT_RUN, and a harness run without ``--reference`` is NOT_COMPARED;
either makes the overall at best BLOCKED. A gate passes only if EVERY part ran a passing test (a
part whose tests were all skipped or deselected fails the gate).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "wynk-release-readiness/1"


@dataclass(frozen=True)
class Part:
    paths: tuple[str, ...]
    k: str | None = None


@dataclass(frozen=True)
class Gate:
    name: str
    covers: str
    parts: tuple[Part, ...] = field(default_factory=tuple)


GATES: tuple[Gate, ...] = (
    Gate(
        "restart_recovery",
        "a crash or restart mid-job / mid-invocation / mid-trigger resumes, never duplicates",
        (
            Part(
                (
                    "tests/test_durable_jobs.py",
                    "tests/test_champion_inference.py",
                    "tests/test_inference_idempotency.py",
                    "tests/test_production_monitoring.py",
                    "tests/test_experiment_idempotency.py",
                ),
                "restart or recover or crash",
            ),
        ),
    ),
    Gate(
        "rollback_and_fencing",
        "rollback restores the previous version; a stale lease or revision is fenced off",
        (
            Part(
                (
                    "tests/test_champion_inference.py",
                    "tests/test_dogfood_lifecycle.py",
                    "tests/test_durable_jobs.py",
                ),
                "rollback or fenc or lease or stale",
            ),
        ),
    ),
    Gate(
        "idempotency_and_ambiguous_outcomes",
        "a retried request replays; an unknown provider outcome is never retried blindly",
        (
            Part(("tests/test_inference_idempotency.py", "tests/test_experiment_idempotency.py")),
            Part(("tests/test_champion_promotion.py",), "ambiguous"),
        ),
    ),
    Gate(
        "cross_workspace_authorization",
        "no workspace can read, change or detect another's resources; roles and scopes hold",
        (
            Part(
                (
                    "tests/test_tenancy.py",
                    "tests/test_authority_boundaries.py",
                    "tests/test_workspace_precondition.py",
                )
            ),
        ),
    ),
    Gate(
        "encryption_and_rotation",
        "uploads, outputs and feedback are sealed per workspace; rotation re-seals everything",
        (
            Part(("tests/test_feedback_sealing.py",)),
            Part(("tests/test_upload_security.py",), "encrypt or rotat or seal or key"),
        ),
    ),
    Gate(
        "migration",
        "legacy feedback and legacy single-tenant data migrate offline, verified and resumable",
        (
            Part(("tests/test_feedback_migration.py",)),
            Part(("tests/test_tenancy.py",), "legacy or migrat"),
        ),
    ),
    Gate(
        "retention_and_erasure",
        "erasure is audited, admin-only, idempotent and fail-closed; retention defaults to keep",
        (
            Part(("tests/test_feedback_retention.py",)),
            Part(("tests/test_upload_security.py",), "retention or delet or purge"),
        ),
    ),
    Gate(
        "pii_and_outbound_controls",
        "fixed-format screening of uploads, inference inputs, feedback and caller metadata",
        (
            Part(("tests/test_outbound_policy.py", "tests/test_feedback_metadata.py")),
            Part(("tests/test_upload_security.py",), "sensitive or pii or secret or redact"),
        ),
    ),
    Gate(
        "cost_accounting",
        "budgets are enforced and every model call's cost is accounted, never guessed",
        (
            Part(("tests/test_budget_guard.py",)),
            Part(
                (
                    "tests/test_inference_idempotency.py",
                    "tests/test_durable_jobs.py",
                    "tests/test_production_monitoring.py",
                ),
                "cost",
            ),
        ),
    ),
    Gate(
        "provenance",
        "every artifact, id and evaluation is traceable and reproducible from stored evidence",
        (Part(("tests/test_experiment_provenance.py", "tests/test_server_minted_ids.py")),),
    ),
)
BLOCKED = {
    "live_provider_smoke": "needs a paid provider call; there is no spending authorization",
    "production_deployment": "needs an operator, real infrastructure and a release decision",
}
# what must never appear in anything this run writes
_SYNTHETIC_SECRETS = (
    "4111 1111 1111 1111",
    "4111111111111111",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "ghp_" + "a" * 36,
    "123-45-6789",
)


def _counts(xml: Path) -> dict[str, int]:
    if not xml.exists():
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    root = ET.parse(xml).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for k in out:
            out[k] += int(s.get(k, 0))
    return out


def run_gate(gate: Gate, out: Path, python: str) -> dict[str, Any]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    parts, started = [], time.monotonic()
    for i, part in enumerate(gate.parts):
        xml = out / "junit" / f"{gate.name}-{i}.xml"
        xml.parent.mkdir(parents=True, exist_ok=True)
        args = [python, "-m", "pytest", "-p", "no:cacheprovider", f"--junitxml={xml}", *part.paths]
        if part.k:
            args += ["-k", part.k]
        log = out / "logs" / f"{gate.name}-{i}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as fh:
            code = subprocess.call(args, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        c = _counts(xml)
        for k in totals:
            totals[k] += c[k]
        ran = c["tests"] - c["failures"] - c["errors"] - c["skipped"]
        parts.append(
            {"paths": list(part.paths), "k": part.k, "exit_code": code, "passed": ran, **c}
        )
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    ok = all(p["exit_code"] == 0 and p["passed"] > 0 for p in parts)
    return {
        "status": "PASS" if ok else "FAIL",
        "covers": gate.covers,
        "passed": passed,
        "failed": totals["failures"] + totals["errors"],
        "skipped": totals["skipped"],
        "parts": parts,
        "seconds": round(time.monotonic() - started, 1),
    }


def _walk(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def stable_fields(report: dict[str, Any]) -> dict[str, Any]:
    """The harness fields that must not change between runs or releases: verdicts, digests,
    assertion / request counts (never timings or temp paths)."""
    words = ("assertion", "request", "digest", "verdict", "count")
    return {k: v for k, v in _walk(report) if any(w in k.lower() for w in words)}


def run_harness(out: Path, python: str, reference: Path | None) -> dict[str, Any]:
    target = out / "harness"
    log = out / "logs" / "lifecycle_harness.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log.open("w", encoding="utf-8") as fh:
        code = subprocess.call(
            [python, "-m", "tests.lifecycle_harness", "--out", str(target)],
            cwd=ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    report_path = target / "lifecycle-report.json"
    result: dict[str, Any] = {"exit_code": code, "seconds": round(time.monotonic() - started, 1)}
    if code != 0 or not report_path.exists():
        return result | {"status": "FAIL", "reason": "the harness failed (see its log)"}
    fields = stable_fields(json.loads(report_path.read_text("utf-8")))
    result["stable_fields"] = len(fields)
    if reference is not None:
        ref = stable_fields(json.loads(reference.read_text("utf-8")))
        diff = sorted(k for k in set(ref) | set(fields) if ref.get(k) != fields.get(k))
        result["reference"] = {"compared": len(ref), "different": diff[:20]}
        if diff:
            return result | {"status": "FAIL", "reason": f"{len(diff)} fields differ"}
        return result | {"status": "PASS"}
    # it ran and passed, but nothing shows its results are the accepted ones
    return result | {"status": "NOT_COMPARED", "reason": "no --reference report was given"}


def run_e2e(out: Path, python: str) -> dict[str, Any]:
    log = out / "logs" / "playwright.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    npx = "npx.cmd" if os.name == "nt" else "npx"
    started = time.monotonic()
    with log.open("w", encoding="utf-8") as fh:
        code = subprocess.call(
            [npx, "playwright", "test"],
            cwd=ROOT / "ui",
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=os.environ | {"WYNK_PYTHON": python},
        )
    return {
        "status": "PASS" if code == 0 else "FAIL",
        "exit_code": code,
        "seconds": round(time.monotonic() - started, 1),
    }


def scan_for_secrets(out: Path) -> dict[str, Any]:
    needles = list(_SYNTHETIC_SECRETS)
    for entry in os.environ.get("WYNK_DATA_KEYS", "").split(","):
        material = entry.partition(":")[2].strip()
        if len(material) >= 16:
            needles.append(material)
    hits = []
    scanned = 0
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        if path.suffix == ".xml":  # JUnit XML repeats test ids, which name no value
            continue
        scanned += 1
        text = path.read_bytes().decode("utf-8", "replace")
        hits += [
            f"{path.relative_to(out)}: secret #{i}" for i, n in enumerate(needles) if n in text
        ]
    return {"status": "FAIL" if hits else "PASS", "files_scanned": scanned, "hits": hits[:20]}


def overall(gates: dict[str, dict[str, Any]]) -> str:
    statuses = {g["status"] for g in gates.values()}
    if "FAIL" in statuses:
        return "FAIL"
    return "BLOCKED" if statuses - {"PASS"} else "PASS"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--with-e2e", action="store_true", help="run the Playwright UI lifecycle")
    p.add_argument("--reference", type=Path, help="a lifecycle-report.json to compare against")
    p.add_argument("--only", nargs="*", help="run only these gates (names)")
    args = p.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    gates: dict[str, dict[str, Any]] = {}
    for gate in GATES:
        if args.only and gate.name not in args.only:
            continue
        gates[gate.name] = run_gate(gate, out, args.python)
        print(f"{gate.name}: {gates[gate.name]['status']}", flush=True)
    if not args.only or "lifecycle_harness" in args.only:
        gates["lifecycle_harness"] = run_harness(out, args.python, args.reference)
        print(f"lifecycle_harness: {gates['lifecycle_harness']['status']}", flush=True)
    if args.with_e2e:
        gates["ui_lifecycle_playwright"] = run_e2e(out, args.python)
    else:
        gates["ui_lifecycle_playwright"] = {"status": "NOT_RUN", "reason": "--with-e2e not given"}
    for name, reason in BLOCKED.items():
        gates[name] = {"status": "BLOCKED", "reason": reason}
    gates["secret_leakage"] = scan_for_secrets(out)  # last: it scans what the others wrote
    report = {"record_schema": SCHEMA, "provider_mode": "FAKE", "gates": gates}
    report["overall"] = overall(gates)
    (out / "release-readiness.json").write_text(json.dumps(report, indent=1), "utf-8")
    for name, g in gates.items():
        counts = f" ({g['passed']} passed, {g['failed']} failed, {g['skipped']} skipped)"
        print(f"{name}: {g['status']}{counts if 'passed' in g else ''}")
    print(f"overall: {report['overall']}")
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
