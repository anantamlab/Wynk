/*
  Wire types and runtime checks for the experiment → promotion → deployment → inference routes of
  the product API (`api/product.py`, `experiments/jobs.py`, `experiments/promotion.py`,
  `experiments/deployment.py`). Screens read these objects as the server sends them (snake_case),
  so there is no mapping layer that could invent or drop a value.

  Compact views (jobs, models, deployments, workflow versions, inferences) are checked key for key,
  like `decode.ts`. Large evidence documents (the artifact body, a promotion record, provenance) are
  checked for the fields screens read; the rest stays as the server sent it. The fixture
  `fixtures/product-api.lifecycle.v1.json` is captured from the real backend and its structure is
  pinned by `tests/test_product_api_ui_lifecycle_fixture.py`.
*/
import { check } from "./decode";
import type { DatasetSpecWire, FieldWire } from "./wire";

const { fail, object, str, optStr, bool, int, num, arr, oneOf } = check;
type Rec = Record<string, unknown>;

/** an object with at least these keys (large evidence documents) */
function has(v: unknown, path: string, keys: readonly string[]): Rec {
  if (typeof v !== "object" || v === null || Array.isArray(v)) fail(path, "an object", v);
  const o = v as Rec;
  const missing = keys.filter((k) => !(k in o));
  if (missing.length) fail(`${path} (missing ${missing.join(", ")})`, "the contract's fields", v);
  return o;
}
const optNum = (v: unknown, path: string): number | null => (v === null ? null : num(v, path));
const optInt = (v: unknown, path: string): number | null => (v === null ? null : int(v, path));
const numbers = (v: unknown, path: string): Record<string, number | null> => {
  const o = has(v, path, []);
  for (const [k, x] of Object.entries(o)) optNum(x, `${path}.${k}`);
  return o as Record<string, number | null>;
};
const strings = (v: unknown, path: string) => arr(v, path, str);

/* ---------------------------------------------------------------- models */

export interface ModelPricingWire {
  version: string;
  currency: string;
  prompt_per_million: number;
  completion_per_million: number;
}

/** api/product.py ModelView: a registry entry without its endpoint or any credential */
export interface ModelWire {
  name: string;
  provider: string;
  adapter: string;
  model_id: string;
  revision: string | null;
  model_hash: string;
  context_window: number | null;
  capabilities: string[];
  pricing: ModelPricingWire | null;
  enabled: boolean;
}

function model(v: unknown, path: string): ModelWire {
  const o = object(v, path, ["name", "provider", "adapter", "model_id", "revision", "model_hash", "context_window", "capabilities", "pricing", "enabled"]);
  let pricing: ModelPricingWire | null = null;
  if (o.pricing !== null) {
    const p = object(o.pricing, `${path}.pricing`, ["version", "currency", "prompt_per_million", "completion_per_million"]);
    pricing = {
      version: str(p.version, `${path}.pricing.version`),
      currency: str(p.currency, `${path}.pricing.currency`),
      prompt_per_million: num(p.prompt_per_million, `${path}.pricing.prompt_per_million`),
      completion_per_million: num(p.completion_per_million, `${path}.pricing.completion_per_million`),
    };
  }
  return {
    name: str(o.name, `${path}.name`),
    provider: str(o.provider, `${path}.provider`),
    adapter: str(o.adapter, `${path}.adapter`),
    model_id: str(o.model_id, `${path}.model_id`),
    revision: optStr(o.revision, `${path}.revision`),
    model_hash: str(o.model_hash, `${path}.model_hash`),
    context_window: optInt(o.context_window, `${path}.context_window`),
    capabilities: strings(o.capabilities, `${path}.capabilities`),
    pricing,
    enabled: bool(o.enabled, `${path}.enabled`),
  };
}

export const modelList = (v: unknown): ModelWire[] => arr(object(v, "body", ["models"]).models, "models", model);

/* ---------------------------------------------------------------- experiment jobs */

export const JOB_STATES = ["PENDING", "RUNNING", "COMPLETED", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "INTERRUPTED"] as const;
export type JobStateWire = (typeof JOB_STATES)[number];
/** store/jobs.py TERMINAL: nothing more will happen without a request */
export const TERMINAL_JOB_STATES: readonly JobStateWire[] = ["COMPLETED", "CANCELLED", "FAILED"];
const UNIT_STATES = ["PENDING", "RUNNING", "COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"] as const;
export type UnitStateWire = (typeof UNIT_STATES)[number];
export const STRATEGIES = ["fixed", "random", "aco", "aco_warm"] as const;
export type StrategyWire = (typeof STRATEGIES)[number];

/** experiments/jobs.py JobSummary */
export interface JobSummaryWire {
  job_id: string;
  state: JobStateWire;
  reason: string | null;
  created_at: string;
  updated_at: string;
  experiment_id: string;
  dataset_id: string;
  dataset_version: number;
  task_id: string;
  synthetic: boolean;
  artifact_available: boolean;
}

export interface AttemptCountsWire {
  completed: number;
  in_flight: number;
  errored: number;
  reused_by_proof: number;
}

/** experiments/jobs.py UnitView: one (strategy, seed) run of a job */
export interface UnitWire {
  strategy: string;
  seed: number;
  run_id: string;
  state: UnitStateWire;
  reason: string | null;
  detail: string | null;
  lease_owner: string | null;
  rounds: number;
  candidate_evaluations: number;
  usage: Record<string, number | null>;
  settled_usage: Record<string, number | null>;
  open_reservation: Record<string, number | null> | null;
  champion_genome_hash: string | null;
  champion_validation_score: number | null;
  curve: Rec[];
  stop_reason: string | null;
  attempts: AttemptCountsWire;
  sources: Record<string, string> | null;
}

/** experiments/jobs.py JobView */
export interface JobWire extends JobSummaryWire {
  detail: string | null;
  cancel_requested_at: string | null;
  definition_hash: string;
  contract_hash: string;
  protocol_hash: string;
  problem_id: string;
  budget: Record<string, number | null>;
  strategies: string[];
  seeds: number[];
  units: UnitWire[];
}

const SUMMARY_KEYS = ["job_id", "state", "reason", "created_at", "updated_at", "experiment_id", "dataset_id", "dataset_version", "task_id", "synthetic", "artifact_available"] as const;

function summaryFields(o: Rec, path: string): JobSummaryWire {
  return {
    job_id: str(o.job_id, `${path}.job_id`),
    state: oneOf(o.state, `${path}.state`, JOB_STATES),
    reason: optStr(o.reason, `${path}.reason`),
    created_at: str(o.created_at, `${path}.created_at`),
    updated_at: str(o.updated_at, `${path}.updated_at`),
    experiment_id: str(o.experiment_id, `${path}.experiment_id`),
    dataset_id: str(o.dataset_id, `${path}.dataset_id`),
    dataset_version: int(o.dataset_version, `${path}.dataset_version`),
    task_id: str(o.task_id, `${path}.task_id`),
    synthetic: bool(o.synthetic, `${path}.synthetic`),
    artifact_available: bool(o.artifact_available, `${path}.artifact_available`),
  };
}

export const jobList = (v: unknown): JobSummaryWire[] =>
  arr(object(v, "body", ["jobs"]).jobs, "jobs", (x, p) => summaryFields(object(x, p, SUMMARY_KEYS), p));

function unit(v: unknown, path: string): UnitWire {
  const o = object(v, path, [
    "strategy", "seed", "run_id", "state", "reason", "detail", "lease_owner", "rounds", "candidate_evaluations", "usage", "settled_usage",
    "open_reservation", "champion_genome_hash", "champion_validation_score", "curve", "stop_reason", "attempts", "sources",
  ]);
  const a = object(o.attempts, `${path}.attempts`, ["completed", "in_flight", "errored", "reused_by_proof"]);
  return {
    strategy: str(o.strategy, `${path}.strategy`),
    seed: int(o.seed, `${path}.seed`),
    run_id: str(o.run_id, `${path}.run_id`),
    state: oneOf(o.state, `${path}.state`, UNIT_STATES),
    reason: optStr(o.reason, `${path}.reason`),
    detail: optStr(o.detail, `${path}.detail`),
    lease_owner: optStr(o.lease_owner, `${path}.lease_owner`),
    rounds: int(o.rounds, `${path}.rounds`),
    candidate_evaluations: int(o.candidate_evaluations, `${path}.candidate_evaluations`),
    usage: numbers(o.usage, `${path}.usage`),
    settled_usage: numbers(o.settled_usage, `${path}.settled_usage`),
    open_reservation: o.open_reservation === null ? null : numbers(o.open_reservation, `${path}.open_reservation`),
    champion_genome_hash: optStr(o.champion_genome_hash, `${path}.champion_genome_hash`),
    champion_validation_score: optNum(o.champion_validation_score, `${path}.champion_validation_score`),
    curve: arr(o.curve, `${path}.curve`, (x, p) => has(x, p, [])),
    stop_reason: optStr(o.stop_reason, `${path}.stop_reason`),
    attempts: {
      completed: int(a.completed, `${path}.attempts.completed`),
      in_flight: int(a.in_flight, `${path}.attempts.in_flight`),
      errored: int(a.errored, `${path}.attempts.errored`),
      reused_by_proof: int(a.reused_by_proof, `${path}.attempts.reused_by_proof`),
    },
    sources: o.sources === null ? null : (has(o.sources, `${path}.sources`, []) as Record<string, string>),
  };
}

export function job(v: unknown, path = "job"): JobWire {
  const o = object(v, path, [
    ...SUMMARY_KEYS, "detail", "cancel_requested_at", "definition_hash", "contract_hash", "protocol_hash", "problem_id", "budget", "strategies", "seeds", "units",
  ]);
  return {
    ...summaryFields(o, path),
    detail: optStr(o.detail, `${path}.detail`),
    cancel_requested_at: optStr(o.cancel_requested_at, `${path}.cancel_requested_at`),
    definition_hash: str(o.definition_hash, `${path}.definition_hash`),
    contract_hash: str(o.contract_hash, `${path}.contract_hash`),
    protocol_hash: str(o.protocol_hash, `${path}.protocol_hash`),
    problem_id: str(o.problem_id, `${path}.problem_id`),
    budget: numbers(o.budget, `${path}.budget`),
    strategies: strings(o.strategies, `${path}.strategies`),
    seeds: arr(o.seeds, `${path}.seeds`, int),
    units: arr(o.units, `${path}.units`, unit),
  };
}

/** The request body of POST /experiments (api/product.py CreateExperiment). */
export interface CreateExperimentWire {
  dataset_id: string;
  dataset_version: number;
  splits_hash: string;
  contract: {
    task_id: string;
    contract_version: number;
    task_type: "classification" | "structured_extraction" | "question_answering";
    instructions: string;
    input_schema: { fields: FieldWire[] };
    output_schema: { fields: FieldWire[] };
    dataset: DatasetSpecWire;
    evaluation: { evaluator: string; config?: Record<string, unknown> };
    constraints: Record<string, number | null>;
  };
  plan: {
    model: { provider: string; model: string };
    expected_model_hash: string;
    budget: { max_candidate_evaluations: number };
    seeds: number[];
    strategies: StrategyWire[];
  };
}

/* ---------------------------------------------------------------- results and provenance */

export interface StageWire {
  kind: string;
  [option: string]: unknown;
}

/** one strategy run in the artifact body (`runs[]`): measured on optimization + validation only */
export interface RunWire {
  strategy: string;
  seed: number;
  run_id: string;
  stop_reason: string | null;
  rounds: number;
  usage: Record<string, number | null>;
  latency_s: Record<string, number | null>;
  split_usage: Record<string, number | null>;
  champion: {
    genome_hash: string;
    feasible: boolean;
    genome: { stages: StageWire[] };
    validation: { score_mean: number | null; pass_rate: number | null; runs: number };
  } | null;
}

export interface ArtifactWire {
  job_id: string;
  experiment_id: string;
  artifact_schema: string;
  artifact_id: string;
  provenance_id: string;
  experiment_sha256: string;
  artifact: {
    schema: string;
    synthetic: boolean;
    test_runs: number;
    splits: Record<string, number>;
    model_hashes: string[];
    runs: RunWire[];
    provenance: Rec;
    summary: { by_strategy: Record<string, Rec>; highest_mean_champion_validation_score: string[] };
  };
}

function run(v: unknown, path: string): RunWire {
  const o = has(v, path, ["strategy", "seed", "run_id", "stop_reason", "rounds", "usage", "latency_s", "split_usage", "champion"]);
  let best: RunWire["champion"] = null;
  if (o.champion !== null) {
    const c = has(o.champion, `${path}.champion`, ["genome_hash", "feasible", "genome", "validation"]);
    const g = has(c.genome, `${path}.champion.genome`, ["stages"]);
    const val = has(c.validation, `${path}.champion.validation`, ["score_mean", "pass_rate", "runs"]);
    best = {
      genome_hash: str(c.genome_hash, `${path}.champion.genome_hash`),
      feasible: bool(c.feasible, `${path}.champion.feasible`),
      genome: { stages: arr(g.stages, `${path}.champion.genome.stages`, (x, p) => has(x, p, ["kind"]) as StageWire) },
      validation: {
        score_mean: optNum(val.score_mean, `${path}.champion.validation.score_mean`),
        pass_rate: optNum(val.pass_rate, `${path}.champion.validation.pass_rate`),
        runs: int(val.runs, `${path}.champion.validation.runs`),
      },
    };
  }
  return {
    strategy: str(o.strategy, `${path}.strategy`),
    seed: int(o.seed, `${path}.seed`),
    run_id: str(o.run_id, `${path}.run_id`),
    stop_reason: optStr(o.stop_reason, `${path}.stop_reason`),
    rounds: int(o.rounds, `${path}.rounds`),
    usage: numbers(o.usage, `${path}.usage`),
    latency_s: numbers(o.latency_s, `${path}.latency_s`),
    split_usage: numbers(o.split_usage, `${path}.split_usage`),
    champion: best,
  };
}

export function artifact(v: unknown): ArtifactWire {
  const o = object(v, "body", ["job_id", "experiment_id", "artifact", "artifact_schema", "artifact_id", "provenance_id", "experiment_sha256"]);
  const a = has(o.artifact, "artifact", ["schema", "synthetic", "test_runs", "splits", "model_hashes", "runs", "provenance", "summary"]);
  const s = has(a.summary, "artifact.summary", ["by_strategy", "highest_mean_champion_validation_score"]);
  return {
    job_id: str(o.job_id, "job_id"),
    experiment_id: str(o.experiment_id, "experiment_id"),
    artifact_schema: str(o.artifact_schema, "artifact_schema"),
    artifact_id: str(o.artifact_id, "artifact_id"),
    provenance_id: str(o.provenance_id, "provenance_id"),
    experiment_sha256: str(o.experiment_sha256, "experiment_sha256"),
    artifact: {
      schema: str(a.schema, "artifact.schema"),
      synthetic: bool(a.synthetic, "artifact.synthetic"),
      test_runs: int(a.test_runs, "artifact.test_runs"),
      splits: numbers(a.splits, "artifact.splits") as Record<string, number>,
      model_hashes: strings(a.model_hashes, "artifact.model_hashes"),
      runs: arr(a.runs, "artifact.runs", run),
      provenance: has(a.provenance, "artifact.provenance", []),
      summary: {
        by_strategy: has(s.by_strategy, "artifact.summary.by_strategy", []) as Record<string, Rec>,
        highest_mean_champion_validation_score: strings(s.highest_mean_champion_validation_score, "artifact.summary.highest_mean_champion_validation_score"),
      },
    },
  };
}

export interface VerifyWire {
  job_id: string;
  artifact_id: string;
  provenance_id: string;
  experiment_sha256: string;
  scientific_sha256: string;
  verified: boolean;
  checks: string[];
  declared_metric_checks: number;
  field_classes: Record<string, number>;
  promotion: Rec | null;
}

export function verify(v: unknown): VerifyWire {
  const o = object(v, "body", ["job_id", "artifact_id", "provenance_id", "experiment_sha256", "scientific_sha256", "verified", "checks", "declared_metric_checks", "field_classes", "promotion"]);
  return {
    job_id: str(o.job_id, "job_id"),
    artifact_id: str(o.artifact_id, "artifact_id"),
    provenance_id: str(o.provenance_id, "provenance_id"),
    experiment_sha256: str(o.experiment_sha256, "experiment_sha256"),
    scientific_sha256: str(o.scientific_sha256, "scientific_sha256"),
    verified: bool(o.verified, "verified"),
    checks: strings(o.checks, "checks"),
    declared_metric_checks: int(o.declared_metric_checks, "declared_metric_checks"),
    field_classes: numbers(o.field_classes, "field_classes") as Record<string, number>,
    promotion: o.promotion === null ? null : has(o.promotion, "promotion", []),
  };
}

export interface ProvenanceWire {
  job_id: string;
  artifact_id: string;
  provenance_id: string;
  experiment_sha256: string;
  promotion_id: string | null;
  provenance: Rec;
}

export function provenance(v: unknown): ProvenanceWire {
  const o = object(v, "body", ["job_id", "artifact_id", "provenance_id", "experiment_sha256", "promotion_id", "provenance"]);
  return {
    job_id: str(o.job_id, "job_id"),
    artifact_id: str(o.artifact_id, "artifact_id"),
    provenance_id: str(o.provenance_id, "provenance_id"),
    experiment_sha256: str(o.experiment_sha256, "experiment_sha256"),
    promotion_id: optStr(o.promotion_id, "promotion_id"),
    provenance: has(o.provenance, "provenance", []),
  };
}

/* ---------------------------------------------------------------- promotion and champions */

export const PROMOTION_STATES = ["OPEN", "INTERRUPTED", "FAILED", "DECIDED"] as const;

/** experiments/promotion.py PromotionView */
export interface PromotionWire {
  promotion_id: string;
  job_id: string;
  lineage_id: string;
  state: (typeof PROMOTION_STATES)[number];
  reason: string | null;
  detail: string | null;
  created_at: string;
  updated_at: string;
  test_opened_at: string | null;
  incumbent_id: string | null;
  incumbent_version: number;
  challenger: Rec | null;
  decision: "PROMOTED" | "REJECTED" | null;
  /** the decision record, once DECIDED */
  record: {
    reason_codes: string[];
    champion: { champion_id: string; version: number } | null;
    heldout: { challenger: { score_mean: number | null; pass_rate: number | null; runs: number; feasible: boolean } | null; incumbent: Rec | null };
  } | null;
  heldout_attempts: AttemptCountsWire;
  artifact: Record<string, string> | null;
}

export function promotion(v: unknown): PromotionWire {
  const o = object(v, "body", [
    "promotion_id", "job_id", "lineage_id", "state", "reason", "detail", "created_at", "updated_at", "test_opened_at", "incumbent_id", "incumbent_version",
    "challenger", "decision", "record", "heldout_attempts", "artifact",
  ]);
  let record: PromotionWire["record"] = null;
  if (o.record !== null) {
    const r = has(o.record, "record", ["reason_codes", "champion", "heldout"]);
    const h = has(r.heldout, "record.heldout", ["challenger", "incumbent"]);
    let challenger: NonNullable<PromotionWire["record"]>["heldout"]["challenger"] = null;
    if (h.challenger !== null) {
      const c = has(h.challenger, "record.heldout.challenger", ["score_mean", "pass_rate", "runs", "feasible"]);
      challenger = {
        score_mean: optNum(c.score_mean, "record.heldout.challenger.score_mean"),
        pass_rate: optNum(c.pass_rate, "record.heldout.challenger.pass_rate"),
        runs: int(c.runs, "record.heldout.challenger.runs"),
        feasible: bool(c.feasible, "record.heldout.challenger.feasible"),
      };
    }
    let current = null;
    if (r.champion !== null) {
      const c = has(r.champion, "record.champion", ["champion_id", "version"]);
      current = { champion_id: str(c.champion_id, "record.champion.champion_id"), version: int(c.version, "record.champion.version") };
    }
    record = {
      reason_codes: strings(r.reason_codes, "record.reason_codes"),
      champion: current,
      heldout: { challenger, incumbent: h.incumbent === null ? null : has(h.incumbent, "record.heldout.incumbent", []) },
    };
  }
  const a = object(o.heldout_attempts, "heldout_attempts", ["completed", "in_flight", "errored", "reused_by_proof"]);
  return {
    promotion_id: str(o.promotion_id, "promotion_id"),
    job_id: str(o.job_id, "job_id"),
    lineage_id: str(o.lineage_id, "lineage_id"),
    state: oneOf(o.state, "state", PROMOTION_STATES),
    reason: optStr(o.reason, "reason"),
    detail: optStr(o.detail, "detail"),
    created_at: str(o.created_at, "created_at"),
    updated_at: str(o.updated_at, "updated_at"),
    test_opened_at: optStr(o.test_opened_at, "test_opened_at"),
    incumbent_id: optStr(o.incumbent_id, "incumbent_id"),
    incumbent_version: int(o.incumbent_version, "incumbent_version"),
    challenger: o.challenger === null ? null : has(o.challenger, "challenger", []),
    decision: o.decision === null ? null : oneOf(o.decision, "decision", ["PROMOTED", "REJECTED"] as const),
    record,
    heldout_attempts: {
      completed: int(a.completed, "heldout_attempts.completed"),
      in_flight: int(a.in_flight, "heldout_attempts.in_flight"),
      errored: int(a.errored, "heldout_attempts.errored"),
      reused_by_proof: int(a.reused_by_proof, "heldout_attempts.reused_by_proof"),
    },
    artifact: o.artifact === null ? null : (has(o.artifact, "artifact", []) as Record<string, string>),
  };
}

export interface ChampionWire {
  champion_id: string;
  lineage_id: string;
  version: number;
  promotion_id: string;
  created_at: string;
}

export function champion(v: unknown): ChampionWire {
  const o = object(v, "body", ["champion_id", "lineage_id", "version", "promotion_id", "created_at", "record"]);
  return {
    champion_id: str(o.champion_id, "champion_id"),
    lineage_id: str(o.lineage_id, "lineage_id"),
    version: int(o.version, "version"),
    promotion_id: str(o.promotion_id, "promotion_id"),
    created_at: str(o.created_at, "created_at"),
  };
}

/* ---------------------------------------------------------------- workflow versions and deployments */

/** experiments/deployment.py WorkflowVersionView (immutable) */
export interface WorkflowVersionWire {
  version_id: string;
  lineage_id: string;
  champion_id: string;
  state: string;
  created_at: string;
  state_updated_at: string;
  /** the pinned input schema an invocation is validated against */
  input_fields: FieldWire[];
  output_fields: FieldWire[];
  model_hash: string;
  synthetic: boolean;
}

function field(v: unknown, path: string): FieldWire {
  const o = object(v, path, ["name", "type", "required"]);
  return {
    name: str(o.name, `${path}.name`),
    type: oneOf(o.type, `${path}.type`, ["string", "integer", "number", "boolean", "date", "string_list"] as const),
    required: bool(o.required, `${path}.required`),
  };
}

function workflowVersion(v: unknown, path: string): WorkflowVersionWire {
  const o = object(v, path, ["version_id", "lineage_id", "champion_id", "state", "created_at", "state_updated_at", "document"]);
  const d = has(o.document, `${path}.document`, ["contract", "versions", "synthetic"]);
  const c = has(d.contract, `${path}.document.contract`, ["input_schema", "output_schema"]);
  const fields = (s: unknown, p: string) => arr(has(s, p, ["fields"]).fields, `${p}.fields`, field);
  return {
    version_id: str(o.version_id, `${path}.version_id`),
    lineage_id: str(o.lineage_id, `${path}.lineage_id`),
    champion_id: str(o.champion_id, `${path}.champion_id`),
    state: str(o.state, `${path}.state`),
    created_at: str(o.created_at, `${path}.created_at`),
    state_updated_at: str(o.state_updated_at, `${path}.state_updated_at`),
    input_fields: fields(c.input_schema, `${path}.document.contract.input_schema`),
    output_fields: fields(c.output_schema, `${path}.document.contract.output_schema`),
    model_hash: str(has(d.versions, `${path}.document.versions`, ["model_hash"]).model_hash, `${path}.document.versions.model_hash`),
    synthetic: bool(d.synthetic, `${path}.document.synthetic`),
  };
}

export const workflowVersionOne = (v: unknown) => workflowVersion(v, "version");
export const workflowVersionList = (v: unknown): WorkflowVersionWire[] =>
  arr(object(v, "body", ["versions"]).versions, "versions", workflowVersion);

/** experiments/deployment.py DeploymentView; `revision` fences every change */
export interface DeploymentWire {
  lineage_id: string;
  revision: number;
  staging_version_id: string | null;
  production_version_id: string | null;
}

export interface DeploymentEventWire {
  seq: number;
  action: string;
  version_id: string;
  replaced_version_id: string | null;
  revision_before: number;
  revision_after: number;
  actor: string;
  created_at: string;
}

const deploymentFields = (o: Rec): DeploymentWire => ({
  lineage_id: str(o.lineage_id, "lineage_id"),
  revision: int(o.revision, "revision"),
  staging_version_id: optStr(o.staging_version_id, "staging_version_id"),
  production_version_id: optStr(o.production_version_id, "production_version_id"),
});

export const deployment = (v: unknown): DeploymentWire =>
  deploymentFields(object(v, "body", ["lineage_id", "revision", "staging_version_id", "production_version_id", "created_at", "updated_at"]));

export function deploymentHistory(v: unknown): DeploymentWire & { events: DeploymentEventWire[] } {
  const o = object(v, "body", ["lineage_id", "revision", "staging_version_id", "production_version_id", "events"]);
  return {
    ...deploymentFields(o),
    events: arr(o.events, "events", (x, p) => {
      const e = object(x, p, ["seq", "action", "version_id", "replaced_version_id", "revision_before", "revision_after", "actor", "created_at"]);
      return {
        seq: int(e.seq, `${p}.seq`),
        action: str(e.action, `${p}.action`),
        version_id: str(e.version_id, `${p}.version_id`),
        replaced_version_id: optStr(e.replaced_version_id, `${p}.replaced_version_id`),
        revision_before: int(e.revision_before, `${p}.revision_before`),
        revision_after: int(e.revision_after, `${p}.revision_after`),
        actor: str(e.actor, `${p}.actor`),
        created_at: str(e.created_at, `${p}.created_at`),
      };
    }),
  };
}

/* ---------------------------------------------------------------- inference */

/** experiments/deployment.py InferenceView. `output` is null when the stored record is read back. */
export interface InferenceWire {
  inference_id: string;
  status: string;
  workflow_version: string;
  lineage_id: string;
  addressed_by: string;
  deployment_revision: number;
  champion_id: string;
  output: Record<string, unknown> | null;
  output_sha256: string | null;
  failure: Rec | null;
  usage: Record<string, number | boolean | null>;
  created_at: string;
}

export function inference(v: unknown): InferenceWire {
  const o = object(v, "body", [
    "record_schema", "inference_id", "status", "workflow_version", "lineage_id", "addressed_by", "deployment_revision", "champion_id", "genome_hash",
    "provenance", "model", "versions", "run_id", "request_sha256", "output", "output_sha256", "failure", "usage", "authenticated_principal", "created_at",
  ]);
  return {
    inference_id: str(o.inference_id, "inference_id"),
    status: str(o.status, "status"),
    workflow_version: str(o.workflow_version, "workflow_version"),
    lineage_id: str(o.lineage_id, "lineage_id"),
    addressed_by: str(o.addressed_by, "addressed_by"),
    deployment_revision: int(o.deployment_revision, "deployment_revision"),
    champion_id: str(o.champion_id, "champion_id"),
    output: o.output === null ? null : has(o.output, "output", []),
    output_sha256: optStr(o.output_sha256, "output_sha256"),
    failure: o.failure === null ? null : has(o.failure, "failure", []),
    usage: has(o.usage, "usage", []) as Record<string, number | boolean | null>,
    created_at: str(o.created_at, "created_at"),
  };
}

/* ---------------------------------------------------------------- workspace (#32) */

export interface WorkspaceWire {
  workspace_id: string;
  name: string;
  role: string;
  scopes: string[];
}

export function currentWorkspace(v: unknown): WorkspaceWire {
  const o = object(v, "body", ["workspace", "principal"]);
  const w = has(o.workspace, "workspace", ["workspace_id", "name"]);
  const p = has(o.principal, "principal", ["role", "scopes"]);
  return { workspace_id: str(w.workspace_id, "workspace.workspace_id"), name: str(w.name, "workspace.name"), role: str(p.role, "principal.role"), scopes: strings(p.scopes, "principal.scopes") };
}
