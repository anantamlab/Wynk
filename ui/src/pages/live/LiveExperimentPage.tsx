import { useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import type { JobWire } from "@/api";
import { ApiErrorState, Fact, LoadingState, Meter, Panel } from "@/components/app/ui";
import { date } from "@/lib/format";
import { useResource } from "@/lib/useResource";
import { fmtCost, fmtInt, fmtScore, isTerminal, JobStateBadge, lifecycle, lineageOf, reasonText, short, StaleNotice, STRATEGY_LABEL } from "./common";

export const POLL_MS = 1000;
export const MAX_POLL_MS = 8000;

/** One experiment job, polled until it reaches a terminal state. */
export function LiveExperimentPage() {
  const { experimentId = "" } = useParams();
  const replayed = (useLocation().state as { replayed?: boolean } | null)?.replayed;
  const job = useResource(() => lifecycle().getJob(experimentId), [experimentId], {
    pollMs: POLL_MS,
    maxPollMs: MAX_POLL_MS,
    shouldPoll: (j: JobWire) => !isTerminal(j.state),
  });
  const [action, setAction] = useState<{ busy: string | null; error: unknown; failed?: string }>({ busy: null, error: null });

  if (job.state === "loading") return <LoadingState label="Loading experiment" />;
  if (job.state === "error") return <ApiErrorState error={job.cause} title="Could not load this experiment" onRetry={job.reload} />;
  const j = job.data;

  const act = async (name: "cancel" | "resume") => {
    setAction({ busy: name, error: null });
    try {
      await (name === "cancel" ? lifecycle().cancelJob(j.job_id) : lifecycle().resumeJob(j.job_id));
      setAction({ busy: null, error: null });
    } catch (error) {
      setAction({ busy: null, error, failed: name });
    }
    job.reload();
  };
  const budget = j.budget.max_candidate_evaluations ?? null;
  const done = j.units.reduce((n, u) => n + u.candidate_evaluations, 0);
  const inFlight = j.units.reduce((n, u) => n + u.attempts.in_flight, 0);
  const cancellable = ["PENDING", "RUNNING", "INTERRUPTED"].includes(j.state);

  return (
    <div className="space-y-4" data-testid="live-experiment" data-job-id={j.job_id} data-state={j.state}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to=".." relative="path" className="text-xs text-ink-soft hover:text-ink">
            ← Experiments
          </Link>
          <h2 className="mt-1 flex items-center gap-2 font-mono text-lg font-bold">
            {j.job_id} <JobStateBadge state={j.state} />
          </h2>
          <p className="mt-0.5 text-xs text-ink-soft">
            {j.dataset_id} v{j.dataset_version} · lineage <span className="font-mono">{lineageOf(j)}</span> · created {date(j.created_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {j.state === "INTERRUPTED" && (
            <button type="button" className="btn btn-primary btn-sm" disabled={action.busy !== null} onClick={() => act("resume")}>
              {action.busy === "resume" ? "Resuming…" : "Resume"}
            </button>
          )}
          {cancellable && (
            <button type="button" className="btn btn-quiet btn-sm" disabled={action.busy !== null} onClick={() => act("cancel")}>
              {action.busy === "cancel" ? "Cancelling…" : "Cancel"}
            </button>
          )}
          {j.state === "COMPLETED" && (
            <Link to="results" className="btn btn-primary btn-sm">
              Results and provenance
            </Link>
          )}
        </div>
      </div>

      {replayed && (
        <p data-testid="create-replayed" className="text-xs text-ink-soft">
          The server already had this request (same Idempotency-Key) and returned the experiment it created then. No second experiment was
          created.
        </p>
      )}
      <StaleNotice error={job.stale} />
      {action.error !== null && <ApiErrorState error={action.error} title={`Could not ${action.failed ?? "change"} this experiment`} />}
      {(j.state === "INTERRUPTED" || j.state === "FAILED") && (
        <div role="alert" data-testid="job-reason" data-reason={j.reason ?? ""} className="rounded-[10px] border border-warn/50 bg-warn-wash px-4 py-3 text-sm">
          <p className="font-semibold">{j.state === "INTERRUPTED" ? "Interrupted" : "Failed"}</p>
          <p className="text-ink-soft">{reasonText(j.reason) ?? "The server gave no reason."}</p>
          {j.detail && <p className="mt-1 font-mono text-[11px] text-ink-soft">{j.detail}</p>}
          {inFlight > 0 && (
            <p className="mt-1 text-xs text-warn" data-testid="in-flight">
              {inFlight} model call{inFlight === 1 ? "" : "s"} started without a stored result.
            </p>
          )}
        </div>
      )}

      <Panel title="Progress" meta={isTerminal(j.state) ? "Final" : "Measured so far; updates while it runs"}>
        {budget !== null && <Meter value={done} max={budget * Math.max(1, j.units.length)} label="Candidate evaluations" />}
        <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
          <Fact label="Candidate evaluations" testId="progress-evaluations">
            {done}
            {budget !== null ? ` / ${budget * j.units.length}` : ""}
          </Fact>
          <Fact label="Strategies">{j.strategies.map((s) => STRATEGY_LABEL[s] ?? s).join(", ")}</Fact>
          <Fact label="Seeds">{j.seeds.join(", ")}</Fact>
          <Fact label="Model">{j.synthetic ? "synthetic stand-in" : "registry model"}</Fact>
        </dl>
        <table className="mt-4 w-full text-sm" aria-label="Runs">
          <thead className="text-left text-xs text-ink-soft">
            <tr>
              <th className="py-1.5 font-medium">Strategy</th>
              <th className="py-1.5 font-medium">Seed</th>
              <th className="py-1.5 font-medium">State</th>
              <th className="py-1.5 font-medium">Evaluations</th>
              <th className="py-1.5 font-medium">Best validation score</th>
              <th className="py-1.5 font-medium">Cost</th>
              <th className="py-1.5 font-medium">Tokens</th>
              <th className="py-1.5 font-medium">Model calls</th>
            </tr>
          </thead>
          <tbody>
            {j.units.map((u) => (
              <tr key={u.run_id} data-testid="unit-row" data-strategy={u.strategy} data-state={u.state} className="border-t border-line">
                <td className="py-2">{STRATEGY_LABEL[u.strategy] ?? u.strategy}</td>
                <td className="py-2">{u.seed}</td>
                <td className="py-2" title={reasonText(u.reason) ?? undefined}>
                  <JobStateBadge state={u.state} />
                  {u.reason && <span className="ml-1 font-mono text-[11px] text-ink-soft">{u.reason}</span>}
                </td>
                <td className="py-2">{u.candidate_evaluations}</td>
                <td className="py-2" data-testid="unit-score">
                  {fmtScore(u.champion_validation_score)}
                </td>
                <td className="py-2">{fmtCost(u.usage.cost)}</td>
                <td className="py-2">{fmtInt(u.usage.tokens)}</td>
                <td className="py-2">{fmtInt(u.usage.model_calls)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Identity">
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <Fact label="Experiment id">
            <span className="font-mono text-xs">{short(j.experiment_id, 24)}</span>
          </Fact>
          <Fact label="Contract hash">
            <span className="font-mono text-xs">{short(j.contract_hash, 24)}</span>
          </Fact>
          <Fact label="Definition hash">
            <span className="font-mono text-xs">{short(j.definition_hash, 24)}</span>
          </Fact>
          <Fact label="Problem id">
            <span className="font-mono text-xs">{short(j.problem_id, 24)}</span>
          </Fact>
        </dl>
      </Panel>
    </div>
  );
}
