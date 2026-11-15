/**
 * PROVIDENCE — the mark.
 *
 * The eye of providence, cut in the same press language as the rest of the
 * page: a lens, not an almond — two arcs mitred to a point at each end, the way
 * a plate overshoots its own join. At the centre is the trace: the red origin
 * dot every thread on the board starts from, laid down twice and a hair out of
 * register — the same overprint the hero's knocked-out word is printed with.
 *
 * Nothing in it is ornament. The lens is what the product does (it looks hard
 * at one post), the dot is what it finds (where the post came from), and the
 * two colours are the only two the page has: ink, and the trace.
 *
 * One drawing at every size. It takes its height from the type beside it (em,
 * never px) and its ink from `currentColor`, so it reddens with the wordmark on
 * hover; only the trace is its own colour, and the dark plate re-points that at
 * --red-wood. The favicon is the same cut, in public/favicon.svg.
 */
export function ProvidenceMark({ className }: { className?: string }) {
  return (
    <svg className={className ? `mark ${className}` : "mark"} viewBox="0 0 64 44"
         aria-hidden="true" focusable="false">
      {/* Points on the axis at x=4 and x=60; the upper lid carries more bulge
          than the lower, so it reads as an eye rather than a vesica. */}
      <path className="mark-lens" d="M4 22.8C17 1.2 47 1.2 60 22.8C47 42.3 17 42.3 4 22.8Z" />
      <circle className="mark-trace-off" cx="33.5" cy="21.3" r="7.2" />
      <circle className="mark-trace" cx="32" cy="22.6" r="7.2" />
    </svg>
  );
}
