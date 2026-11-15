import type { Verdict } from "../api/types";
import { VERDICT_LABEL } from "../lib/filters";

/** Mark pressed into the stamp, so colour never carries the meaning alone. */
export const VERDICT_ICON: Record<Verdict, string> = {
  true: "✓",
  false: "✕",
  misleading: "!",
  unverified: "?",
};

/**
 * The verdict as a struck badge: [✕ FALSE]. Verdict colours are a scale of
 * their own, deliberately muted, so a verdict can never be mistaken for the
 * red of a trace.
 */
export function VerdictStamp({ verdict, label }: { verdict: Verdict; label?: string }) {
  return (
    <span className="stamp" data-verdict={verdict}>
      <span className={`vdot v-${verdict}`} aria-hidden="true">{VERDICT_ICON[verdict]}</span>
      {label ?? VERDICT_LABEL[verdict]}
    </span>
  );
}

/** Verdict with icon + text label. Kept as the name the dashboard pages use. */
export function VerdictPill({ verdict, label }: { verdict: Verdict; label?: string }) {
  return <VerdictStamp verdict={verdict} label={label} />;
}

/** Small marker for lists and filters. Always place it next to a text label. */
export function StatusDot({ verdict }: { verdict: Verdict | "all" }) {
  return <span className={verdict === "all" ? "sdot" : `sdot v-${verdict}`} aria-hidden="true" />;
}
