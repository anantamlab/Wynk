import { Flask, Plus } from "@phosphor-icons/react";
import { Link } from "react-router";
import { api, type JobSummaryWire } from "@/api";
import { ApiErrorState, EmptyState, LoadingState, Panel } from "@/components/app/ui";
import { date } from "@/lib/format";
import { useResource } from "@/lib/useResource";
import { useProject } from "@/pages/ProjectLayout";
import { isTerminal, JobStateBadge, lifecycle, lineageOf, reasonText, StaleNotice } from "./common";

/**
 * The project's experiments, from the product API. Jobs are workspace-scoped; a project's jobs are
 * the ones over its datasets (each job view names its dataset).
 */
export function useProjectJobs(projectId: string, poll = false) {
  return useResource(
    async () => {
      const [datasets, jobs] = await Promise.all([api().listDatasets(projectId), lifecycle().listJobs()]);
      const ids = new Set(datasets.map((d) => d.id));
      return jobs.filter((j) => ids.has(j.dataset_id)).sort((a, b) => b.created_at.localeCompare(a.created_at));
    },
    [projectId],
    { pollMs: poll ? 2000 : undefined, shouldPoll: (jobs: JobSummaryWire[]) => jobs.some((j) => !isTerminal(j.state)) },
  );
}

export function LiveExperimentsPage() {
  const { project } = useProject();
  const jobs = useProjectJobs(project.id, true);

  return (
    <Panel
      title="Experiments"
      meta="Durable jobs on the Wynk API: fixed baseline vs random search vs Wynk, at equal budgets"
      actions={
        <Link to="new" className="btn btn-primary btn-sm">
          <Plus size={14} aria-hidden="true" /> New experiment
        </Link>
      }
    >
      {jobs.state === "loading" && <LoadingState label="Loading experiments" />}
      {jobs.state === "error" && <ApiErrorState error={jobs.cause} title="Could not load experiments" onRetry={jobs.reload} />}
      {jobs.state === "ready" && (
        <>
          <StaleNotice error={jobs.stale} />
          {jobs.data.length === 0 ? (
            <EmptyState icon={Flask} title="No experiments in this project">
              Configure one over a registered dataset version and its splits.
            </EmptyState>
          ) : (
            <table className="w-full text-sm" aria-label="Experiments">
              <thead className="text-left text-xs text-ink-soft">
                <tr>
                  <th className="py-1.5 font-medium">Experiment</th>
                  <th className="py-1.5 font-medium">Dataset</th>
                  <th className="py-1.5 font-medium">Lineage</th>
                  <th className="py-1.5 font-medium">State</th>
                  <th className="py-1.5 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((j) => (
                  <tr key={j.job_id} data-testid="job-row" data-job-id={j.job_id} className="border-t border-line">
                    <td className="py-2">
                      <Link to={j.job_id} className="font-mono text-xs font-medium text-magenta-ink hover:underline">
                        {j.job_id}
                      </Link>
                      {j.synthetic && <span className="ml-2 text-[11px] text-warn">synthetic model</span>}
                    </td>
                    <td className="py-2 font-mono text-xs">
                      {j.dataset_id} v{j.dataset_version}
                    </td>
                    <td className="py-2 font-mono text-xs">{lineageOf(j)}</td>
                    <td className="py-2" title={reasonText(j.reason) ?? undefined}>
                      <JobStateBadge state={j.state} />
                    </td>
                    <td className="py-2 text-xs text-ink-soft">{date(j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Panel>
  );
}
