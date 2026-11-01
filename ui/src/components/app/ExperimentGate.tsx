import { Info } from "@phosphor-icons/react";
import type { ReactNode } from "react";
import { api } from "@/api";

/**
 * Experiment screens (configure, run, results, workflows) exist twice and never mix:
 *   - live (the default): `live`, built only on the product API's real experiment routes;
 *   - mock (`?api=mock`): `children`, the simulated demo, labelled as simulated on every screen.
 * The live adapter is the only one with a `lifecycle`, so a mock run can never reach a live screen
 * and a live screen never renders mock data.
 */
export function ExperimentGate({ children, live }: { children: ReactNode; live: ReactNode }) {
  if (api().lifecycle) return <>{live}</>;
  return (
    <div className="space-y-4">
      <SimulatedNotice />
      {children}
    </div>
  );
}

export function SimulatedNotice() {
  return (
    <div
      role="note"
      data-testid="simulated-notice"
      className="flex items-start gap-2.5 rounded-[10px] border border-dashed border-warn/50 bg-warn-wash px-4 py-2.5 text-xs text-warn"
    >
      <Info size={16} weight="fill" className="mt-px shrink-0" aria-hidden="true" />
      <p>
        <span className="font-semibold">Simulated experiment (mock demo data).</span> No model was called and no optimization ran: candidates,
        scores, costs, latencies and champions here are invented by the mock adapter. Real experiments run in live mode, the default (open the
        app without <span className="font-mono">?api=mock</span>).
      </p>
    </div>
  );
}
