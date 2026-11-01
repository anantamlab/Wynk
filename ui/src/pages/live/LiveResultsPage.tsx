import { SealCheck, Warning } from "@phosphor-icons/react";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { ApiError, type PromotionWire, type RunWire } from "@/api";
import { ApiErrorState, Fact, LoadingState, Panel } from "@/components/app/ui";
import { useResource } from "@/lib/useResource";
import { fmtCost, fmtInt, fmtScore, fmtSecs, lifecycle, short, STRATEGY_LABEL } from "./common";

const stagesText = (r: RunWire) => (r.champion ? r.champion.genome.stages.map((s) => s.kind).join(" → ") : "—");

/**
 * Results of a COMPLETED experiment: the canonical artifact (verified on every read by the server),
 * its provenance, and the held-out promotion. Every number is the server's.
 */
export function LiveResultsPage() {
  const { experimentId = "" } = useParams();
  const data = useResource(async () => {
    const l = lifecycle();
    const [artifact, verify, provenance] = await Promise.all([l.getArtifact(experimentId), l.verifyArtifact(experimentId), l.getProvenance(experimentId)]);
    return { artifact, verify, provenance };
  }, [experimentId]);
  const promotion = useResource(
    () =>
      lifecycle()
        .getPromotion(experimentId)
        .catch((e) => {
          if (e instanceof ApiError && e.code === "promotion_not_found") return null;
          throw e;
        }),
    [experimentId],
  );

  if (data.state === "loading") return <LoadingState label="Loading results" />;
  if (data.state === "error") return <ApiErrorState error={data.cause} title="No results for this experiment" onRetry={data.reload} />;
  const { artifact: a, verify: v, provenance: p } = data.data;
  const body = a.artifact;
  const best = new Set(body.summary.highest_mean_champion_validation_score);
  const source = body.provenance as Record<string, unknown>;

  return (
    <div className="space-y-4" data-testid="live-results" data-job-id={a.job_id}>
      <div>
        <Link to=".." relative="path" className="text-xs text-ink-soft hover:text-ink">
          ← Experiment {a.job_id}
        </Link>
        <h2 className="mt-1 text-xl font-bold">Results</h2>
        <p className="mt-0.5 text-xs text-ink-soft">
          Measured on the optimization split, compared on validation. The held-out test split is opened only by promotion.
          {body.synthetic && <span className="ml-1 font-semibold text-warn">Synthetic stand-in model.</span>}
        </p>
      </div>

      <Panel title="Comparison" meta="Equal candidate-evaluation budgets per strategy">
        <table className="w-full text-sm" aria-label="Strategy comparison">
          <thead className="text-left text-xs text-ink-soft">
            <tr>
              <th className="py-1.5 font-medium">Strategy</th>
              <th className="py-1.5 font-medium">Seed</th>
              <th className="py-1.5 font-medium">Validation score</th>
              <th className="py-1.5 font-medium">Pass rate</th>
              <th className="py-1.5 font-medium">Evaluations</th>
              <th className="py-1.5 font-medium">Cost</th>
              <th className="py-1.5 font-medium">Tokens</th>
              <th className="py-1.5 font-medium">Model calls</th>
              <th className="py-1.5 font-medium">p95 latency</th>
              <th className="py-1.5 font-medium">Champion workflow</th>
            </tr>
          </thead>
          <tbody>
            {body.runs.map((r) => (
              <tr key={r.run_id} data-testid="result-row" data-strategy={r.strategy} className="border-t border-line align-top">
                <td className="py-2">
                  {STRATEGY_LABEL[r.strategy] ?? r.strategy}
                  {best.has(r.strategy) && <span className="ml-1 text-[11px] font-semibold text-good">best on validation</span>}
                </td>
                <td className="py-2">{r.seed}</td>
                <td className="py-2 font-semibold" data-testid="result-score">
                  {fmtScore(r.champion?.validation.score_mean)}
                </td>
                <td className="py-2">{fmtScore(r.champion?.validation.pass_rate)}</td>
                <td className="py-2">{fmtInt(r.usage.candidate_evaluations)}</td>
                <td className="py-2" data-testid="result-cost">
                  {fmtCost(r.usage.cost)}
                </td>
                <td className="py-2">{fmtInt(r.usage.tokens)}</td>
                <td className="py-2">{fmtInt(r.usage.model_calls)}</td>
                <td className="py-2">{fmtSecs(r.latency_s.p95)}</td>
                <td className="py-2 font-mono text-[11px]">{stagesText(r)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
          {Object.entries(body.splits).map(([split, n]) => (
            <Fact key={split} label={`${split} rows`} testId={`split-${split}`}>
              {n}
            </Fact>
          ))}
        </dl>
        <p className="mt-2 text-xs text-ink-soft" data-testid="test-runs">
          Test-split runs during the search: {body.test_runs}
        </p>
      </Panel>

      <Panel
        title="Provenance"
        meta={
          v.verified ? (
            <span data-testid="verified" className="inline-flex items-center gap-1 text-good">
              <SealCheck size={14} weight="fill" aria-hidden="true" /> Verified by the server ({v.checks.length} checks, {v.declared_metric_checks} metric
              checks)
            </span>
          ) : (
            <span data-testid="verified" className="inline-flex items-center gap-1 text-bad">
              <Warning size={14} weight="fill" aria-hidden="true" /> Not verified
            </span>
          )
        }
      >
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <Fact label="Artifact id" testId="artifact-id">
            <span className="font-mono text-xs">{a.artifact_id}</span>
          </Fact>
          <Fact label="Provenance id">
            <span className="font-mono text-xs">{p.provenance_id}</span>
          </Fact>
          <Fact label="Experiment sha256">
            <span className="font-mono text-xs">{short(a.experiment_sha256, 32)}</span>
          </Fact>
          <Fact label="Dataset">
            <span className="font-mono text-xs">
              {String(source.dataset_id ?? "?")} v{String(source.dataset_version ?? "?")} · splits {short(String(source.splits_hash ?? ""), 12)}
            </span>
          </Fact>
          <Fact label="Pinned model hashes" wide>
            <span className="font-mono text-xs">{body.model_hashes.map((h) => short(h, 16)).join(", ")}</span>
          </Fact>
        </dl>
      </Panel>

      <PromotionPanel jobId={a.job_id} promotion={promotion} />
    </div>
  );
}

function PromotionPanel({ jobId, promotion }: { jobId: string; promotion: ReturnType<typeof useResource<PromotionWire | null>> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const promote = async () => {
    setBusy(true);
    setError(null);
    try {
      await lifecycle().promote(jobId);
    } catch (e) {
      setError(e);
    }
    setBusy(false);
    promotion.reload();
  };
  return (
    <Panel title="Champion promotion" meta="Selects one challenger on validation, then opens the held-out test split once">
      {promotion.state === "loading" && <LoadingState rows={1} />}
      {promotion.state === "error" && <ApiErrorState error={promotion.cause} title="Could not load the promotion" onRetry={promotion.reload} />}
      {promotion.state === "ready" && promotion.data === null && (
        <div className="space-y-2">
          <p className="text-sm text-ink-soft">Not promoted. Promotion runs the held-out evaluation now and records an immutable decision.</p>
          <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={promote}>
            {busy ? "Evaluating on the held-out split…" : "Promote on the held-out test split"}
          </button>
        </div>
      )}
      {promotion.state === "ready" && promotion.data && <PromotionView p={promotion.data} />}
      {error !== null && <ApiErrorState error={error} title="Promotion did not finish" />}
    </Panel>
  );
}

function PromotionView({ p }: { p: PromotionWire }) {
  const held = p.record?.heldout.challenger ?? null;
  return (
    <div data-testid="promotion" data-decision={p.decision ?? ""} data-state={p.state} className="space-y-2 text-sm">
      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Fact label="State">{p.state}</Fact>
        <Fact label="Decision" testId="promotion-decision">
          {p.decision ?? "—"}
        </Fact>
        <Fact label="Held-out score (challenger)" testId="heldout-score">
          {fmtScore(held?.score_mean)}
        </Fact>
        <Fact label="Lineage">
          <span className="font-mono text-xs">{p.lineage_id}</span>
        </Fact>
        {p.record?.champion && (
          <Fact label="Champion" testId="champion-id">
            <span className="font-mono text-xs">{p.record.champion.champion_id}</span> (v{p.record.champion.version})
          </Fact>
        )}
        <Fact label="Reasons">{p.record?.reason_codes.join(", ") ?? (p.reason ?? "—")}</Fact>
      </dl>
      {p.state !== "DECIDED" && (
        <p className="text-xs text-warn">
          {p.reason ?? "In progress"}
          {p.detail ? `: ${p.detail}` : ""}. Held-out calls: {p.heldout_attempts.completed} completed, {p.heldout_attempts.in_flight} without a stored result.
        </p>
      )}
      {p.decision === "PROMOTED" && (
        <Link to="../../../workflows" relative="path" className="btn btn-quiet btn-sm">
          Publish and deploy in Workflows
        </Link>
      )}
    </div>
  );
}
