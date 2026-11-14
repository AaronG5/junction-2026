import type { Language } from "../api/types";

/** Chart colors in fixed order (see docs/design/tokens.css). At most 3 slices; the rest folds into the last. */
const SLICE_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)"];

export function LanguageBar({ languages, label = "Languages" }: { languages: Language[]; label?: string }) {
  const slices = languages.slice(0, 3);
  const description = slices.map((l) => `${l.name} ${l.share}%`).join(", ");
  return (
    <div className="langbar">
      <span className="lbl">{label}</span>
      <div className="langbar-track" role="img" aria-label={description}>
        {slices.map((l, i) => (
          <div key={l.code + i} style={{ width: `${l.share}%`, background: SLICE_COLORS[i] }} />
        ))}
      </div>
      <div className="legend">
        {slices.map((l, i) => (
          <span key={l.code + i}>
            <i style={{ background: SLICE_COLORS[i] }} aria-hidden="true" />
            {l.name} {l.share}%
          </span>
        ))}
      </div>
    </div>
  );
}
