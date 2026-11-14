import type { Verdict } from "../api/types";
import { VERDICT_LABEL } from "../lib/filters";

/** Icon shown inside the colored dot. Color never carries the meaning alone. */
export const VERDICT_ICON: Record<Verdict, string> = {
  true: "✓",
  false: "✕",
  misleading: "!",
  unverified: "?",
};

/** Verdict with icon + text label, e.g. [✕ False]. */
export function VerdictPill({ verdict, label }: { verdict: Verdict; label?: string }) {
  return (
    <span className="pill" data-verdict={verdict}>
      <span className={`vdot v-${verdict}`} aria-hidden="true">{VERDICT_ICON[verdict]}</span>
      {label ?? VERDICT_LABEL[verdict]}
    </span>
  );
}

/** Small colored dot for lists and filters. Always place it next to a text label. */
export function StatusDot({ verdict }: { verdict: Verdict | "all" }) {
  return <span className={verdict === "all" ? "sdot" : `sdot v-${verdict}`} aria-hidden="true" />;
}
