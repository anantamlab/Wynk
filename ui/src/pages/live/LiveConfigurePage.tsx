import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api, type CreateExperimentWire, type DatasetSpecWire, type JobWire, type ModelWire, type StrategyWire } from "@/api";
import { ApiErrorState, Field, LoadingState, Panel } from "@/components/app/ui";
import { useIdempotentSend } from "@/lib/idempotency";
import { useResource } from "@/lib/useResource";
import { useProject } from "@/pages/ProjectLayout";
import { lifecycle, STRATEGY_LABEL } from "./common";
import { SendStatus } from "./SendStatus";

type TaskType = CreateExperimentWire["contract"]["task_type"];

/** core/task_contract.py: evaluators each task type admits (the server re-checks every pairing). */
const EVALUATORS: Record<TaskType, string[]> = {
  question_answering: ["exact_match", "token_f1", "numeric_tolerance"],
  classification: ["classification_accuracy", "exact_match"],
  structured_extraction: ["exact_match", "json_schema_validity", "numeric_tolerance"],
};
const CHOOSABLE: StrategyWire[] = ["fixed", "random", "aco", "aco_warm"];
/** per-example run caps the runtime enforces; a contract must state them (core/run_contract.py) */
const DEFAULT_CAPS = {
  maximum_tokens_per_example: 20000,
  maximum_wall_time_s: 120,
  maximum_tool_calls: 20,
  maximum_retries: 2,
  maximum_model_calls: 4,
};
const CAP_LABEL: Record<keyof typeof DEFAULT_CAPS, string> = {
  maximum_tokens_per_example: "Max tokens per example",
  maximum_wall_time_s: "Max seconds per example",
  maximum_tool_calls: "Max tool calls per example",
  maximum_retries: "Max retries per example",
  maximum_model_calls: "Max model calls per example",
};

/** The TaskContract input/output schema is the dataset's own columns and types. */
export function schemaFor(spec: DatasetSpecWire, columns: string[]) {
  return {
    fields: columns.map((name) => {
      const col = spec.columns.find((c) => c.name === name);
      return { name, type: (col?.type ?? "string") as "string", required: !(col?.nullable ?? false) };
    }),
  };
}

export function buildExperiment(input: {
  spec: DatasetSpecWire;
  splitsHash: string;
  taskId: string;
  taskType: TaskType;
  instructions: string;
  evaluator: string;
  model: ModelWire;
  strategies: StrategyWire[];
  maxCandidates: number;
  seeds: number[];
  caps: Record<string, number>;
}): CreateExperimentWire {
  const { spec } = input;
  return {
    dataset_id: spec.dataset_id,
    dataset_version: spec.dataset_version,
    splits_hash: input.splitsHash,
    contract: {
      task_id: input.taskId,
      contract_version: 1,
      task_type: input.taskType,
      instructions: input.instructions.trim(),
      input_schema: schemaFor(spec, [...spec.input_columns, ...spec.context_columns]),
      output_schema: schemaFor(spec, spec.target_columns),
      dataset: spec,
      evaluation: { evaluator: input.evaluator },
      constraints: input.caps,
    },
    plan: {
      model: { provider: input.model.provider, model: input.model.model_id },
      expected_model_hash: input.model.model_hash,
      budget: { max_candidate_evaluations: input.maxCandidates },
      seeds: input.seeds,
      strategies: input.strategies,
    },
  };
}

export function LiveConfigurePage() {
  const { project } = useProject();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const datasets = useResource(() => api().listDatasets(project.id), [project.id]);
  const models = useResource(() => lifecycle().listModels(), []);

  const [datasetId, setDatasetId] = useState(params.get("dataset") ?? "");
  const [version, setVersion] = useState<number | null>(null);
  const [splitsHash, setSplitsHash] = useState("");
  const [taskId, setTaskId] = useState("task");
  const [taskType, setTaskType] = useState<TaskType>("question_answering");
  const [instructions, setInstructions] = useState("");
  const [evaluator, setEvaluator] = useState("exact_match");
  const [modelHash, setModelHash] = useState("");
  const [strategies, setStrategies] = useState<StrategyWire[]>(["fixed", "random", "aco"]);
  const [maxCandidates, setMaxCandidates] = useState(4);
  const [seeds, setSeeds] = useState("0");
  const [caps, setCaps] = useState<Record<string, number>>(DEFAULT_CAPS);
  const send = useIdempotentSend<JobWire>("exp");

  const dataset = datasets.state === "ready" ? datasets.data.find((d) => d.id === datasetId) : undefined;
  useEffect(() => {
    if (datasets.state === "ready" && !datasetId && datasets.data.length > 0) setDatasetId(datasets.data[0].id);
  }, [datasets, datasetId]);
  useEffect(() => {
    if (dataset) setVersion(dataset.latestVersion);
  }, [dataset]);
  useEffect(() => {
    if (models.state === "ready" && !modelHash) setModelHash(models.data.find((m) => m.enabled)?.model_hash ?? "");
  }, [models, modelHash]);
  useEffect(() => {
    if (!EVALUATORS[taskType].includes(evaluator)) setEvaluator(EVALUATORS[taskType][0]);
  }, [taskType, evaluator]);

  const spec = useResource(
    () => (dataset && version !== null ? lifecycle().datasetSpec(dataset.id, version) : Promise.resolve(null)),
    [dataset?.id, version],
  );
  const splits = useResource(() => (dataset && version !== null ? api().listSplits(dataset.id, version) : Promise.resolve([])), [dataset?.id, version]);
  useEffect(() => {
    if (splits.state === "ready") setSplitsHash((h) => (splits.data.some((s) => s.splitsHash === h) ? h : (splits.data[0]?.splitsHash ?? "")));
  }, [splits]);

  const model = models.state === "ready" ? models.data.find((m) => m.model_hash === modelHash) : undefined;
  const seedList = seeds
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map(Number);
  const seedsOk = seedList.length > 0 && seedList.every((n) => Number.isInteger(n) && n >= 0);
  const body = useMemo(
    () =>
      spec.state === "ready" && spec.data && model && splitsHash && seedsOk && strategies.length > 0 && instructions.trim()
        ? buildExperiment({ spec: spec.data, splitsHash, taskId, taskType, instructions, evaluator, model, strategies, maxCandidates, seeds: seedList, caps })
        : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec, model, splitsHash, seeds, strategies, instructions, taskId, taskType, evaluator, maxCandidates, caps],
  );

  const create = (key: string) => lifecycle().createJob(body!, key);
  const finish = (outcome: Awaited<ReturnType<typeof send.submit>>) => {
    if (outcome?.kind === "done") navigate(`../${outcome.value.job_id}`, { relative: "path", state: { replayed: outcome.replayed } });
  };
  const fingerprint = body ? JSON.stringify(body) : "";
  const sending = send.state.phase === "sending";

  if (datasets.state === "loading" || models.state === "loading") return <LoadingState label="Loading datasets and models" />;
  if (datasets.state === "error") return <ApiErrorState error={datasets.cause} title="Could not load datasets" onRetry={datasets.reload} />;

  return (
    <form
      className="space-y-4"
      onSubmit={async (e) => {
        e.preventDefault();
        if (body) finish(await send.submit(fingerprint, create));
      }}
    >
      <Panel title="New experiment" meta="Runs on the Wynk API: every number afterwards is measured by the server">
        {models.state === "error" && <ApiErrorState error={models.cause} title="No model registry on this server" onRetry={models.reload} />}
        {datasets.data.length === 0 ? (
          <p className="text-sm text-ink-soft">
            Register a dataset first.{" "}
            <Link to="../../datasets/new" relative="path" className="font-medium text-magenta-ink hover:underline">
              Add dataset
            </Link>
          </p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <Field label="Dataset">
              {(p) => (
                <select {...p} className="input" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                  {datasets.data.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name} ({d.id})
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Dataset version">
              {(p) => (
                <select {...p} className="input" value={version ?? ""} onChange={(e) => setVersion(Number(e.target.value))}>
                  {dataset?.versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version} · {v.rowCount} rows
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field
              label="Splits"
              error={splits.state === "ready" && splits.data.length === 0 ? "This version has no splits yet. Create them on the dataset page." : undefined}
            >
              {(p) => (
                <select {...p} className="input font-mono text-xs" value={splitsHash} onChange={(e) => setSplitsHash(e.target.value)}>
                  {splits.state === "ready" &&
                    splits.data.map((s) => (
                      <option key={s.splitsHash} value={s.splitsHash}>
                        {s.splitsHash.slice(0, 12)}… · {s.sizes.optimization}/{s.sizes.validation}/{s.sizes.test}
                      </option>
                    ))}
                </select>
              )}
            </Field>
            <Field label="Model" hint={model ? `${model.provider} · ${model.model_id}${model.revision ? ` @ ${model.revision}` : ""} · pinned ${model.model_hash.slice(0, 12)}…` : undefined}>
              {(p) => (
                <select {...p} className="input" value={modelHash} onChange={(e) => setModelHash(e.target.value)}>
                  {models.state === "ready" &&
                    models.data.map((m) => (
                      <option key={m.model_hash} value={m.model_hash} disabled={!m.enabled}>
                        {m.name}
                        {m.enabled ? "" : " (disabled)"}
                        {m.pricing ? ` · ${m.pricing.currency} ${m.pricing.prompt_per_million}/${m.pricing.completion_per_million} per 1M tok` : " · unpriced"}
                      </option>
                    ))}
                </select>
              )}
            </Field>
            <Field label="Task id" hint="The lineage is dataset id + task id.">
              {(p) => <input {...p} className="input font-mono" value={taskId} onChange={(e) => setTaskId(e.target.value)} />}
            </Field>
            <Field label="Task type">
              {(p) => (
                <select {...p} className="input" value={taskType} onChange={(e) => setTaskType(e.target.value as TaskType)}>
                  <option value="question_answering">Question answering</option>
                  <option value="classification">Classification</option>
                  <option value="structured_extraction">Structured extraction</option>
                </select>
              )}
            </Field>
            <Field label="Evaluator">
              {(p) => (
                <select {...p} className="input" value={evaluator} onChange={(e) => setEvaluator(e.target.value)}>
                  {EVALUATORS[taskType].map((ev) => (
                    <option key={ev} value={ev}>
                      {ev}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Instructions" className="sm:col-span-2" hint="What the workflow must do with each row.">
              {(p) => <textarea {...p} className="input min-h-[72px]" value={instructions} onChange={(e) => setInstructions(e.target.value)} />}
            </Field>
          </div>
        )}
      </Panel>

      <Panel title="Search plan" meta="Each strategy gets the same candidate-evaluation budget">
        <fieldset className="flex flex-wrap gap-4 text-sm">
          <legend className="sr-only">Strategies</legend>
          {CHOOSABLE.map((s) => (
            <label key={s} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={strategies.includes(s)}
                onChange={(e) => setStrategies((cur) => (e.target.checked ? [...cur, s] : cur.filter((x) => x !== s)))}
              />
              {STRATEGY_LABEL[s]}
            </label>
          ))}
        </fieldset>
        <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Field label="Candidate evaluations per strategy">
            {(p) => <input {...p} type="number" min={1} className="input" value={maxCandidates} onChange={(e) => setMaxCandidates(Number(e.target.value))} />}
          </Field>
          <Field label="Seeds" hint="Comma-separated" error={seedsOk ? undefined : "Seeds are non-negative integers."}>
            {(p) => <input {...p} className="input" value={seeds} onChange={(e) => setSeeds(e.target.value)} />}
          </Field>
          {(Object.keys(DEFAULT_CAPS) as (keyof typeof DEFAULT_CAPS)[]).map((k) => (
            <Field key={k} label={CAP_LABEL[k]}>
              {(p) => (
                <input {...p} type="number" min={1} className="input" value={caps[k]} onChange={(e) => setCaps((c) => ({ ...c, [k]: Number(e.target.value) }))} />
              )}
            </Field>
          ))}
        </div>
      </Panel>

      {spec.state === "error" && <ApiErrorState error={spec.cause} title="Could not read the dataset version" onRetry={spec.reload} />}
      <SendStatus
        state={send.state}
        noun="experiment"
        onRetry={async () => finish(await send.retry(create))}
        onNewKey={async () => finish(await send.sendWithNewKey(fingerprint, create))}
      />
      <div className="flex justify-end">
        <button type="submit" className="btn btn-primary" disabled={!body || sending}>
          {sending ? "Creating…" : "Create experiment"}
        </button>
      </div>
    </form>
  );
}
