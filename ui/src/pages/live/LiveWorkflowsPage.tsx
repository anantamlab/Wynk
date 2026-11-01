import { TreeStructure } from "@phosphor-icons/react";
import { useMemo, useState } from "react";
import { ApiError, type DeploymentWire, type FieldWire, type InferenceWire, type InvokeTarget, type WorkflowVersionWire } from "@/api";
import { ApiErrorState, EmptyState, Fact, LoadingState, Panel } from "@/components/app/ui";
import { date } from "@/lib/format";
import { useIdempotentSend } from "@/lib/idempotency";
import { useResource } from "@/lib/useResource";
import { useProject } from "@/pages/ProjectLayout";
import { useProjectJobs } from "./LiveExperimentsPage";
import { fmtCost, fmtInt, lifecycle, lineageOf, short } from "./common";
import { SendStatus } from "./SendStatus";

const notFound = (code: string) => (e: unknown) => {
  if (e instanceof ApiError && e.code === code) return null;
  throw e;
};

/**
 * Champions, published workflow versions and their deployment, per lineage of this project's
 * experiments. Every deployment move is fenced by the revision this page read; the server refuses
 * it with `stale_deployment` when someone else moved the lineage first.
 */
export function LiveWorkflowsPage() {
  const { project } = useProject();
  const jobs = useProjectJobs(project.id);
  const lineages = useMemo(() => (jobs.state === "ready" ? [...new Set(jobs.data.map(lineageOf))].sort() : []), [jobs]);

  if (jobs.state === "loading") return <LoadingState label="Loading workflows" />;
  if (jobs.state === "error") return <ApiErrorState error={jobs.cause} title="Could not load workflows" onRetry={jobs.reload} />;
  if (lineages.length === 0)
    return (
      <Panel title="Workflows">
        <EmptyState icon={TreeStructure} title="No workflow lineages yet">
          A lineage starts with an experiment. Promote its champion on the held-out split, then publish it here.
        </EmptyState>
      </Panel>
    );
  return (
    <div className="space-y-4">
      {lineages.map((l) => (
        <LineagePanel key={l} lineage={l} />
      ))}
    </div>
  );
}

function LineagePanel({ lineage }: { lineage: string }) {
  const data = useResource(async () => {
    const l = lifecycle();
    const [champion, versions, history] = await Promise.all([
      l.getChampion(lineage).catch(notFound("champion_not_found")),
      l.listWorkflowVersions(lineage),
      l.getDeploymentHistory(lineage).catch(notFound("deployment_not_found")),
    ]);
    return { champion, versions, history };
  }, [lineage]);
  const [action, setAction] = useState<{ busy: string | null; error: unknown; failed?: string }>({ busy: null, error: null });

  const act = async (name: string, run: () => Promise<unknown>) => {
    setAction({ busy: name, error: null });
    try {
      await run();
      setAction({ busy: null, error: null });
    } catch (error) {
      setAction({ busy: null, error, failed: name });
    }
    data.reload();
  };

  return (
    <Panel title={`Lineage ${lineage}`} meta="Promoted champions become immutable workflow versions">
      {data.state === "loading" && <LoadingState rows={2} />}
      {data.state === "error" && <ApiErrorState error={data.cause} title="Could not load this lineage" onRetry={data.reload} />}
      {data.state === "ready" &&
        (() => {
          const { champion, versions, history } = data.data;
          const d: DeploymentWire | null = history;
          const published = champion !== null && versions.some((v) => v.champion_id === champion.champion_id);
          const busy = action.busy !== null;
          const stale = action.error instanceof ApiError && action.error.code === "stale_deployment";
          return (
            <div className="space-y-4" data-testid="lineage" data-lineage={lineage} data-revision={d?.revision ?? ""}>
              <dl className="grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
                <Fact label="Champion" testId="lineage-champion">
                  {champion ? (
                    <span className="font-mono text-xs">
                      {champion.champion_id} (v{champion.version})
                    </span>
                  ) : (
                    "None promoted"
                  )}
                </Fact>
                <Fact label="Deployment revision" testId="deployment-revision">
                  {d ? d.revision : "Nothing deployed"}
                </Fact>
                <Fact label="Staging" testId="staging-version">
                  <span className="font-mono text-xs">{d?.staging_version_id ?? "—"}</span>
                </Fact>
                <Fact label="Production" testId="production-version">
                  <span className="font-mono text-xs">{d?.production_version_id ?? "—"}</span>
                </Fact>
              </dl>

              <div className="flex flex-wrap gap-2">
                {champion && !published && (
                  <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => act("publish", () => lifecycle().publish(champion.champion_id))}>
                    {action.busy === "publish" ? "Publishing…" : "Publish champion"}
                  </button>
                )}
                {d?.production_version_id && history?.events.some((e) => e.version_id !== d.production_version_id && ["promote", "rollback"].includes(e.action)) && (
                  <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => act("roll back", () => lifecycle().rollback(lineage, d.revision))}>
                    {action.busy === "roll back" ? "Rolling back…" : `Roll back production (revision ${d.revision})`}
                  </button>
                )}
              </div>
              {action.error !== null && (
                <div className="space-y-2" data-testid="deployment-error">
                  <ApiErrorState error={action.error} title={stale ? "Someone changed this deployment first" : `Could not ${action.failed ?? "change"} the deployment`} />
                  {stale && (
                    <p className="text-xs text-ink-soft">
                      Nothing was changed. The page now shows the current revision; check it and try again.
                    </p>
                  )}
                </div>
              )}

              {versions.length > 0 && (
                <table className="w-full text-sm" aria-label={`Workflow versions of ${lineage}`}>
                  <thead className="text-left text-xs text-ink-soft">
                    <tr>
                      <th className="py-1.5 font-medium">Version</th>
                      <th className="py-1.5 font-medium">State</th>
                      <th className="py-1.5 font-medium">Champion</th>
                      <th className="py-1.5 font-medium">Model hash</th>
                      <th className="py-1.5 font-medium">Published</th>
                      <th className="py-1.5 font-medium">
                        <span className="sr-only">Deployment actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.map((v) => (
                      <tr key={v.version_id} data-testid="version-row" data-version-id={v.version_id} data-state={v.state} className="border-t border-line">
                        <td className="py-2 font-mono text-xs">{v.version_id}</td>
                        <td className="py-2 text-xs font-semibold">{v.state}</td>
                        <td className="py-2 font-mono text-xs">{v.champion_id}</td>
                        <td className="py-2 font-mono text-xs">{short(v.model_hash, 12)}</td>
                        <td className="py-2 text-xs text-ink-soft">{date(v.created_at)}</td>
                        <td className="py-2 text-right">
                          {d && v.state === "CREATED" && (
                            <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => act("stage", () => lifecycle().stage(v.version_id, d.revision))}>
                              {action.busy === "stage" ? "Staging…" : "Stage"}
                            </button>
                          )}
                          {d && v.state === "STAGING" && (
                            <button
                              type="button"
                              className="btn btn-primary btn-sm"
                              disabled={busy}
                              onClick={() => act("promote", () => lifecycle().promoteVersion(v.version_id, d.revision))}
                            >
                              {action.busy === "promote" ? "Promoting…" : "Promote to production"}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {history && history.events.length > 0 && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-ink-soft">Deployment history ({history.events.length} events)</summary>
                  <ol className="mt-2 space-y-1 font-mono text-[11px]" data-testid="deployment-history">
                    {history.events.map((e) => (
                      <li key={e.seq}>
                        r{e.revision_before}→r{e.revision_after} {e.action} {e.version_id}
                        {e.replaced_version_id ? ` (replaced ${e.replaced_version_id})` : ""} · {e.actor} · {date(e.created_at)}
                      </li>
                    ))}
                  </ol>
                </details>
              )}

              <InvokePanel lineage={lineage} deployment={d} versions={versions} />
            </div>
          );
        })()}
    </Panel>
  );
}

type Values = Record<string, string | boolean>;

/** Converts the form's values to the version's typed inputs; empty optional fields are left out. */
export function toInputs(fields: FieldWire[], values: Values): { inputs: Record<string, unknown> } | { error: string } {
  const inputs: Record<string, unknown> = {};
  for (const f of fields) {
    const raw = values[f.name];
    if (f.type === "boolean") {
      inputs[f.name] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw.trim() : "";
    if (text === "") {
      if (f.required) return { error: `${f.name} is required` };
      continue;
    }
    if (f.type === "integer" || f.type === "number") {
      const n = Number(text);
      if (!Number.isFinite(n) || (f.type === "integer" && !Number.isInteger(n))) return { error: `${f.name} must be ${f.type === "integer" ? "an integer" : "a number"}` };
      inputs[f.name] = n;
    } else if (f.type === "string_list") {
      inputs[f.name] = text.split(",").map((s) => s.trim()).filter(Boolean);
    } else {
      inputs[f.name] = typeof raw === "string" ? raw : text;
    }
  }
  return { inputs };
}

const targetLabel = (t: InvokeTarget) => ("lineage" in t ? `production of ${t.lineage}` : `version ${t.versionId}`);

function InvokePanel({ lineage, deployment, versions }: { lineage: string; deployment: DeploymentWire | null; versions: WorkflowVersionWire[] }) {
  const targets = useMemo(() => {
    const out: { id: string; target: InvokeTarget; version: WorkflowVersionWire }[] = [];
    const prod = versions.find((v) => v.version_id === deployment?.production_version_id);
    if (prod) out.push({ id: "production", target: { lineage }, version: prod });
    const staging = versions.find((v) => v.version_id === deployment?.staging_version_id);
    if (staging) out.push({ id: staging.version_id, target: { versionId: staging.version_id }, version: staging });
    return out;
  }, [lineage, deployment, versions]);
  const [targetId, setTargetId] = useState("production");
  const [values, setValues] = useState<Values>({});
  const [invalid, setInvalid] = useState<string | null>(null);
  const send = useIdempotentSend<InferenceWire>("inf");

  if (targets.length === 0) return null;
  const chosen = targets.find((t) => t.id === targetId) ?? targets[0];
  const fields = chosen.version.input_fields;
  const busy = send.state.phase === "sending";

  const request = () => {
    const parsed = toInputs(fields, values);
    if ("error" in parsed) {
      setInvalid(parsed.error);
      return null;
    }
    setInvalid(null);
    const target = chosen.target;
    return {
      fingerprint: JSON.stringify([target, parsed.inputs]),
      run: (key: string) => lifecycle().invoke(target, parsed.inputs, key),
    };
  };
  const submit = () => {
    const r = request();
    if (r) void send.submit(r.fingerprint, r.run);
  };
  const again = () => {
    const r = request();
    if (r) void send.sendWithNewKey(r.fingerprint, r.run);
  };
  // the original request, not the form as it is now
  const retry = () => void send.retry();
  // an ambiguous, changed or conflicting request is sent again only with a new key, deliberately
  const now = toInputs(fields, values);
  const isHeld = send.held !== null && !("error" in now) && send.held === JSON.stringify([chosen.target, now.inputs]);
  const result = send.state.phase === "settled" && send.state.kind === "done" ? send.state.value : null;

  return (
    <div className="rounded-[10px] border border-line px-4 py-3" data-testid="invoke-panel">
      <h3 className="text-sm font-semibold">Run the workflow</h3>
      <p className="mt-0.5 text-xs text-ink-soft">
        Calls the deployed workflow's pinned model. Each run is one request with its own Idempotency-Key; a lost answer is retried with the same key,
        so it never runs twice.
      </p>
      <form
        className="mt-3 space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {targets.length > 1 && (
          <label className="block text-xs">
            <span className="text-ink-soft">Target</span>
            <select className="input mt-1 block" value={chosen.id} onChange={(e) => setTargetId(e.target.value)} disabled={busy}>
              {targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {targetLabel(t.target)}
                </option>
              ))}
            </select>
          </label>
        )}
        {fields.map((f) => (
          <label key={f.name} className="block text-xs">
            <span className="text-ink-soft">
              {f.name} <span className="font-mono">({f.type}{f.required ? "" : ", optional"})</span>
            </span>
            {f.type === "boolean" ? (
              <input
                type="checkbox"
                className="ml-2"
                checked={values[f.name] === true}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.checked })}
                disabled={busy}
              />
            ) : (
              <textarea
                className="input mt-1 block w-full font-mono text-xs"
                rows={f.type === "string" ? 3 : 1}
                name={f.name}
                value={typeof values[f.name] === "string" ? (values[f.name] as string) : ""}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                disabled={busy}
              />
            )}
          </label>
        ))}
        {invalid && (
          <p role="alert" className="text-xs text-bad">
            {invalid}
          </p>
        )}
        <button type="submit" className="btn btn-primary btn-sm" disabled={busy || isHeld}>
          {busy ? "Running…" : `Run on ${targetLabel(chosen.target)}`}
        </button>
      </form>
      <div className="mt-3 space-y-3">
        <SendStatus state={send.state} noun="invocation" onRetry={retry} onNewKey={again} />
        {result && <InferenceResult r={result} />}
      </div>
    </div>
  );
}

function InferenceResult({ r }: { r: InferenceWire }) {
  return (
    <div data-testid="inference" data-inference-id={r.inference_id} data-status={r.status} className="space-y-2 text-sm">
      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Fact label="Status">{r.status}</Fact>
        <Fact label="Inference id">
          <span className="font-mono text-xs">{r.inference_id}</span>
        </Fact>
        <Fact label="Version (revision)">
          <span className="font-mono text-xs">
            {r.workflow_version} (r{r.deployment_revision})
          </span>
        </Fact>
        <Fact label="Cost · tokens">
          {fmtCost(typeof r.usage.cost === "number" ? r.usage.cost : null)} · {fmtInt(typeof r.usage.tokens === "number" ? r.usage.tokens : null)}
        </Fact>
      </dl>
      {r.output && (
        <pre data-testid="inference-output" className="overflow-x-auto rounded-md bg-wash px-3 py-2 font-mono text-xs">
          {JSON.stringify(r.output, null, 2)}
        </pre>
      )}
      {r.failure && (
        <pre data-testid="inference-failure" className="overflow-x-auto rounded-md bg-bad-wash px-3 py-2 font-mono text-xs">
          {JSON.stringify(r.failure, null, 2)}
        </pre>
      )}
    </div>
  );
}
