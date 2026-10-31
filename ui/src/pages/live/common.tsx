import { CheckCircle, CircleDashed, type Icon, Pause, Prohibit, Spinner, Warning, XCircle } from "@phosphor-icons/react";
import { api, type JobStateWire, type JobSummaryWire, type LifecycleApi, TERMINAL_JOB_STATES } from "@/api";
import { cn } from "@/lib/utils";

/* Shared by the live experiment / workflow screens. Everything here describes server data. */

/** Only rendered behind ExperimentGate's live branch, where the adapter has a lifecycle. */
export function lifecycle(): LifecycleApi {
  const l = api().lifecycle;
  if (!l) throw new Error("live screens need the live adapter");
  return l;
}

/** experiments/promotion.py: a promotion's lineage defaults to `{dataset_id}.{task_id}`. */
export const lineageOf = (j: Pick<JobSummaryWire, "dataset_id" | "task_id">) => `${j.dataset_id}.${j.task_id}`;

export const isTerminal = (s: JobStateWire) => TERMINAL_JOB_STATES.includes(s);

export const short = (hash: string, n = 12) => (hash.length > n ? `${hash.slice(0, n)}…` : hash);

const STATE_STYLE: Record<JobStateWire, { icon: Icon; cls: string; label: string; spin?: boolean }> = {
  PENDING: { icon: CircleDashed, cls: "border-line-strong text-ink-soft", label: "Pending" },
  RUNNING: { icon: Spinner, cls: "border-magenta/60 bg-magenta-wash text-magenta-ink", label: "Running", spin: true },
  CANCEL_REQUESTED: { icon: Spinner, cls: "border-line-strong text-ink-soft", label: "Cancelling", spin: true },
  COMPLETED: { icon: CheckCircle, cls: "border-good/30 bg-good-wash text-good", label: "Completed" },
  CANCELLED: { icon: Prohibit, cls: "border-line-strong text-ink-soft", label: "Cancelled" },
  FAILED: { icon: XCircle, cls: "border-bad/30 bg-bad-wash text-bad", label: "Failed" },
  INTERRUPTED: { icon: Pause, cls: "border-warn/50 bg-warn-wash text-warn", label: "Interrupted" },
};

export function JobStateBadge({ state }: { state: string }) {
  const s = STATE_STYLE[state as JobStateWire] ?? { icon: Warning, cls: "border-line-strong", label: state };
  const IconCmp = s.icon;
  return (
    <span data-testid="job-state" data-state={state} className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold", s.cls)}>
      <IconCmp size={12} weight="bold" className={s.spin ? "animate-spin" : undefined} aria-hidden="true" />
      {s.label}
    </span>
  );
}

/** store/jobs.py Reason: why a job or run is where it is (stable strings). */
export const REASON_COPY: Record<string, string> = {
  process_lost: "Its worker process stopped with nothing in flight. The server resumes it on its own.",
  ambiguous_attempt:
    "A model call started and its result was never stored, so it may have reached the provider. Wynk never resends it on its own; resuming is refused until the call is proven.",
  resume_requested: "Resume was requested; waiting for a worker.",
  runtime_unavailable: "This server could not bind the job's model runtime.",
  cancelled: "The job was cancelled.",
  job_failed: "Another run of this job failed.",
  replay_diverged: "Re-running the stored progress disagreed with it.",
  model_unavailable: "The pinned model was unavailable.",
  experiment_error: "An invariant of the experiment broke; it failed closed.",
  reservation_overflow: "A run exceeded its reserved budget.",
  pricing_error: "The run could not be priced.",
  runner_error: "The workflow runner or evaluator raised an error.",
  assembly_failed: "The result artifact could not be assembled.",
};

export const reasonText = (reason: string | null) => (reason === null ? null : (REASON_COPY[reason] ?? reason.replaceAll("_", " ")));

export const STRATEGY_LABEL: Record<string, string> = {
  fixed: "Fixed baseline",
  random: "Random search",
  aco: "Wynk (ACO)",
  aco_warm: "Wynk (ACO, warm start)",
};

export const fmtScore = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(3));
export const fmtCost = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `$${v.toFixed(v >= 1 ? 2 : 5)}`);
export const fmtInt = (v: number | null | undefined) => (v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-US"));
export const fmtSecs = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v >= 1 ? `${v.toFixed(2)} s` : `${Math.round(v * 1000)} ms`);

/** A poll that failed while the last good answer stays on screen. */
export function StaleNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const code = (error as { code?: string }).code ?? "error";
  return (
    <p role="status" data-testid="stale" className="flex items-center gap-1.5 text-xs text-warn">
      <Warning size={14} weight="fill" aria-hidden="true" />
      Lost contact with the Wynk API <span className="font-mono">[{code}]</span>. Showing the last answer; retrying with backoff.
    </p>
  );
}
