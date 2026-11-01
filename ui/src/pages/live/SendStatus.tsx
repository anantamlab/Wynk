import { ArrowClockwise, Question, Spinner, Warning } from "@phosphor-icons/react";
import type { ReactNode } from "react";
import { ApiError } from "@/api";
import { ApiErrorState } from "@/components/app/ui";
import type { Block, SendState } from "@/lib/idempotency";

/**
 * What happened to an idempotent request, said plainly. Nothing here resends on its own except
 * the same-key retries `sendIdempotent` already made; every "again" is a button.
 */
export function SendStatus<T>({
  state,
  noun,
  onRetry,
  onNewKey,
  block = null,
}: {
  state: SendState<T>;
  /** "experiment" / "invocation" */
  noun: string;
  /** resend with the SAME key (unresolved) */
  onRetry: () => void;
  /** send the form as it is now with a NEW key (deliberate) */
  onNewKey: () => void;
  /** why the page's submit button is disabled for the form as it is now */
  block?: Block;
}) {
  if (state.phase === "idle") return null;
  const status = <Status state={state} noun={noun} onRetry={onRetry} onNewKey={onNewKey} />;
  if (block !== "other_pending") return status;
  return (
    <div className="space-y-2">
      {status}
      <p data-testid="send-blocked" className="text-xs text-ink-soft">
        These values differ from the unconfirmed {noun} (Idempotency-Key <span className="font-mono">{state.key}</span>
        ). Submit the same values to recover it with its key, or send these values as a new request with a new key.
      </p>
    </div>
  );
}

function Status<T>({ state, noun, onRetry, onNewKey }: { state: Exclude<SendState<T>, { phase: "idle" }>; noun: string; onRetry: () => void; onNewKey: () => void }) {
  const key = <KeyLine value={state.key} />;
  if (state.phase === "recovered")
    return state.outcome === "ambiguous" ? (
      <Box phase="recovered-ambiguous" icon={<Warning size={18} weight="fill" className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />}>
        <p className="font-semibold">Outcome unknown (before the page reloaded)</p>
        <p className="text-ink-soft">
          A model call for this {noun} may have reached the provider, and its result was never stored. Wynk does not resend it. Running it again
          is a new request with a new key and may call the model again.
        </p>
        {key}
        <button type="button" className="btn btn-quiet btn-sm mt-2" onClick={onNewKey}>
          Run again with a new key
        </button>
      </Box>
    ) : (
      <Box phase="recovered" icon={<Question size={18} weight="fill" className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />}>
        <p className="font-semibold">Not confirmed before the page reloaded</p>
        <p className="text-ink-soft">
          A {noun} was sent and its answer never arrived. The server may already have it. Submit the same values again: they go out with the
          same key, so the server answers with the original {noun} instead of starting a second one. Wynk kept only the key, not the values.
        </p>
        {key}
        <button type="button" className="btn btn-quiet btn-sm mt-2" onClick={onNewKey}>
          Send the current values as a new request (new key)
        </button>
      </Box>
    );
  if (state.phase === "sending")
    return (
      <div role="status" data-testid="send-status" data-phase="sending" className="space-y-1 text-xs text-ink-soft">
        <p className="flex items-center gap-1.5">
          <Spinner size={14} className="animate-spin" aria-hidden="true" />
          {state.waiting
            ? state.waiting.code === "inference_in_progress"
              ? `The ${noun} is still running on the server; checking again with the same key (attempt ${state.waiting.attempt}).`
              : `No confirmed answer yet [${state.waiting.code}]; retrying with the same key, so it can never run twice (attempt ${state.waiting.attempt}).`
            : `Sending the ${noun}…`}
        </p>
        {key}
      </div>
    );
  switch (state.kind) {
    case "done":
      return state.replayed ? (
        <p data-testid="send-status" data-phase="replayed" className="text-xs text-ink-soft">
          The server had already received this request (<span className="font-mono">Idempotent-Replayed</span>) and answered with the original
          result; nothing ran twice. {key}
        </p>
      ) : null;
    case "unresolved":
      return (
        <Box phase="unresolved" icon={<Question size={18} weight="fill" className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />}>
          <p className="font-semibold">Not confirmed</p>
          <p className="text-ink-soft">
            The server did not confirm the {noun} [{state.error.code}]: {state.error.message} It may already have it. Retrying sends the same
            key, so the server answers with the original {noun} instead of starting a second one.
          </p>
          {key}
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className="btn btn-quiet btn-sm" onClick={onRetry}>
              <ArrowClockwise size={14} aria-hidden="true" /> Retry with the same key
            </button>
            <button type="button" className="btn btn-quiet btn-sm" onClick={onNewKey}>
              Send the current values as a new request (new key)
            </button>
          </div>
        </Box>
      );
    case "ambiguous":
      return (
        <Box phase="ambiguous" icon={<Warning size={18} weight="fill" className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />}>
          <p className="font-semibold">Outcome unknown</p>
          <p className="text-ink-soft">
            {state.error.message} A model call for this {noun} may have reached the provider, and its result was never stored. Wynk does not
            resend it. Running it again is a new request with a new key and may call the model again.
          </p>
          <Details error={state.error} />
          {key}
          <button type="button" className="btn btn-quiet btn-sm mt-2" onClick={onNewKey}>
            Run again with a new key
          </button>
        </Box>
      );
    case "deployment_changed":
      return (
        <Box phase="deployment_changed" icon={<Warning size={18} weight="fill" className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />}>
          <p className="font-semibold">The deployment changed before this {noun} ran</p>
          <p className="text-ink-soft">{state.error.message} The version this key is bound to no longer serves. Nothing was sent to a model.</p>
          {key}
          <button type="button" className="btn btn-quiet btn-sm mt-2" onClick={onNewKey}>
            Run on the current deployment (new key)
          </button>
        </Box>
      );
    case "conflict":
      return (
        <div className="space-y-2" data-testid="send-status" data-phase="conflict">
          <ApiErrorState error={state.error} />
          {key}
          <button type="button" className="btn btn-quiet btn-sm" onClick={onNewKey}>
            Send as a new request (new key)
          </button>
        </div>
      );
    case "refused":
      return (
        <div className="space-y-2" data-testid="send-status" data-phase="refused">
          <ApiErrorState error={state.error} />
          {key}
        </div>
      );
  }
}

function Box({ phase, icon, children }: { phase: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div role="alert" data-testid="send-status" data-phase={phase} className="flex items-start gap-3 rounded-[10px] border border-warn/50 bg-warn-wash px-4 py-3 text-sm">
      {icon}
      <div className="min-w-0 flex-1 space-y-1">{children}</div>
    </div>
  );
}

function Details({ error }: { error: ApiError }) {
  const facts = Object.entries(error.details).filter(([, v]) => v !== null && v !== "");
  return (
    <p className="font-mono text-[11px] text-ink-soft">
      <span data-testid="error-code">{error.code}</span>
      {facts.map(([k, v]) => (
        <span key={k}>
          {" · "}
          {k.replaceAll("_", " ")} {String(v)}
        </span>
      ))}
    </p>
  );
}

function KeyLine({ value }: { value: string }) {
  return (
    <p className="font-mono text-[11px] text-ink-soft">
      Idempotency-Key <span data-testid="idempotency-key">{value}</span>
    </p>
  );
}
