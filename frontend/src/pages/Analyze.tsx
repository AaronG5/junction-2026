import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { MAX_CHECK_TEXT, api, isSupportedLink } from "../api/client";
import { ANALYZE_STEPS, type PostReport, type TextSegment } from "../api/types";
import { LanguageBar } from "../components/LanguageBar";
import { INPUT_PROBLEM, NarrativeReport, TEXT_EXAMPLES, inputKind, useNarrativeCheck } from "../components/NarrativeCheck";
import { ErrorState } from "../components/States";
import { VerdictPill } from "../components/Verdict";
import { useDocumentTitle } from "../lib/useDocumentTitle";

const EXAMPLES = [
  { label: "Telegram link", url: "https://t.me/kanalas_b/4521" },
  { label: "Facebook link", url: "https://www.facebook.com/tiesabecenzuros/posts/1188" },
];

type Run =
  | { state: "idle" }
  | { state: "running"; step: number }
  | { state: "done"; report: PostReport }
  | { state: "failed"; error: unknown };

/**
 * Analyze a post: paste its text to run the narrative check, or a Telegram/Facebook
 * link to watch the link analysis steps and read the full report.
 */
export function Analyze() {
  useDocumentTitle("Analyze a post");
  const [params, setParams] = useSearchParams();
  const [url, setUrl] = useState(params.get("url") ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [run, setRun] = useState<Run>({ state: "idle" });
  const latest = useRef(0);
  const check = useNarrativeCheck();

  const start = useCallback((link: string) => {
    if (!isSupportedLink(link)) {
      setProblem(INPUT_PROBLEM.otherLink!);
      return;
    }
    setProblem(null);
    check.reset();
    const id = ++latest.current;
    setRun({ state: "running", step: 0 });
    api.analyze(link.trim(), (step) => {
      if (id === latest.current) setRun({ state: "running", step });
    }).then(
      (report) => { if (id === latest.current) setRun({ state: "done", report }); },
      (error) => { if (id === latest.current) setRun({ state: "failed", error }); },
    );
  }, [check.reset]);

  // A link in the address (?url=...) is analyzed right away.
  const fromAddress = params.get("url");
  useEffect(() => {
    if (fromAddress) {
      setUrl(fromAddress);
      start(fromAddress);
    }
  }, [fromAddress, start]);
  useEffect(() => () => { latest.current += 1; }, []);

  const checkText = (text: string) => {
    latest.current += 1;
    setRun({ state: "idle" });
    setProblem(null);
    if (fromAddress) setParams({}, { replace: true });
    check.start(text);
  };

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const kind = inputKind(url);
    if (kind === "text") checkText(url);
    else if (kind !== "link") setProblem(INPUT_PROBLEM[kind] ?? null);
    else if (url.trim() !== fromAddress) setParams({ url: url.trim() }, { replace: true });
    else start(url);
  };

  const running = run.state === "running" || check.run.state === "running";

  return (
    <div className="page">
      <header style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <h1 className="page-title">Analyze a post</h1>
        <form onSubmit={submit} className="analyze-form" noValidate>
          <div className="analyze-field">
            <label htmlFor="link" style={{ fontSize: 13, fontWeight: 600 }}>
              Text of a post, or a Telegram or Facebook link
            </label>
            <textarea
              id="link" className="field field-text" value={url} rows={4} maxLength={MAX_CHECK_TEXT}
              placeholder="Paste a post in any language, or a link like https://t.me/channel/1234"
              aria-invalid={Boolean(problem)} aria-describedby="link-msg"
              onChange={(e) => { setUrl(e.target.value); setProblem(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(); }}
            />
            <span id="link-msg" className={problem ? "ferror" : "muted"} style={{ fontSize: 13 }} role={problem ? "alert" : undefined}>
              {problem ?? "Text gets the narrative check. A post link from a public Telegram channel or group, or a Facebook page or group, gets the full report."}
            </span>
          </div>
          <button type="submit" className="btn" disabled={running} style={{ minWidth: 120 }}>
            {running ? "Analyzing…" : "Analyze"}
          </button>
        </form>
        {run.state !== "done" && check.run.state === "idle" && (
          <div className="examples">
            <span>Try an example:</span>
            {TEXT_EXAMPLES.map((ex) => (
              <button key={ex.label} type="button" className="chip chip-btn" disabled={running}
                onClick={() => { setUrl(ex.text); checkText(ex.text); }}>{ex.label}</button>
            ))}
            {EXAMPLES.map((ex) => (
              <Link key={ex.url} className="chip" to={`/dashboard/analyze?url=${encodeURIComponent(ex.url)}`}>{ex.label}</Link>
            ))}
          </div>
        )}
      </header>

      {check.run.state !== "idle" && <NarrativeReport run={check.run} onRetry={() => check.start(check.run.state === "idle" ? "" : check.run.text)} />}
      {run.state === "running" && <Progress step={run.step} />}
      {run.state === "failed" && <ErrorState error={run.error} onRetry={() => start(url)} />}
      {run.state === "done" && <Report report={run.report} />}
    </div>
  );
}

function Progress({ step }: { step: number }) {
  return (
    <section className="card" aria-labelledby="progress-h" style={{ gap: 14 }}>
      <span id="progress-h" className="lbl">Analyzing</span>
      <ol className="steps" aria-live="polite">
        {ANALYZE_STEPS.map((label, i) => {
          const status = i < step ? "done" : i === step ? "active" : "pending";
          return (
            <li key={label} data-status={status} aria-current={status === "active" ? "step" : undefined}>
              <span className={`step-dot step-${status}`} aria-hidden="true">{status === "done" ? "✓" : status === "active" ? "…" : ""}</span>
              <span>{label}</span>
              {status === "done" && <span className="visually-hidden"> (done)</span>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Segments({ parts }: { parts: TextSegment[] }) {
  return (
    <>
      {parts.map((p, i) =>
        p.flag ? (
          <mark key={i} className="flagged" title={p.flag}>
            {p.text}
            <span className="visually-hidden"> (flagged: {p.flag})</span>
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}

function Report({ report: r }: { report: PostReport }) {
  const [english, setEnglish] = useState(false);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <p className="muted" style={{ fontSize: 13 }}>Answered from {r.answeredFrom} · analyzed just now</p>

      <section className="tiles" aria-label="Summary">
        <div className="card tile">
          <span className="lbl">Verdict</span>
          <span style={{ alignSelf: "flex-start" }}><VerdictPill verdict={r.verdict.verdict} /></span>
          <span className="muted tile-note">{r.verdict.summary}</span>
        </div>
        <div className="card tile">
          <span className="lbl">Manipulation signals</span>
          <span className="tile-num num">{r.manipulation} <small>/ 10</small></span>
          <div className="meter" role="img" aria-label={`${r.manipulation} out of 10`}><div style={{ width: `${r.manipulation * 10}%` }} /></div>
        </div>
        <div className="card tile">
          <span className="lbl">Spread</span>
          <span className="tile-num num">{r.spread.copies} <small>copies</small></span>
          <span className="muted tile-note">{r.spread.languages} languages · within {r.spread.hours} hours</span>
        </div>
        <div className="card tile">
          <span className="lbl">Earliest source</span>
          <span className="tile-num" style={{ fontSize: 20 }}>{r.earliest.name}</span>
          <span className="muted tile-note">{r.earliest.note}</span>
        </div>
      </section>

      <div className="split">
        <div style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
          <article className="card" aria-labelledby="post-h">
            <h2 id="post-h" className="visually-hidden">The post</h2>
            <div className="row-between">
              {r.post.sourceId ? (
                <Link to={`/dashboard/sources/${r.post.sourceId}`} style={{ fontWeight: 700, fontSize: 15, textDecoration: "none" }}>{r.post.author}</Link>
              ) : (
                <span style={{ fontWeight: 700, fontSize: 15 }}>{r.post.author}</span>
              )}
              <span className="muted" style={{ fontSize: 12.5 }}>{r.post.where}</span>
            </div>
            <p className="post-text" lang={english ? "en" : undefined} data-testid="post-text">
              <Segments parts={english ? r.post.translation : r.post.original} />
            </p>
            <div className="row-between" style={{ alignItems: "center" }}>
              <div role="group" aria-label="Post language" className="seg">
                <button type="button" aria-pressed={!english} onClick={() => setEnglish(false)}>Original ({r.post.originalLanguage})</button>
                <button type="button" aria-pressed={english} onClick={() => setEnglish(true)}>English</button>
              </div>
              <span className="muted" style={{ fontSize: 12.5 }}>Underlined: wording Gemma flagged as manipulative</span>
            </div>
          </article>

          <section className="card" aria-labelledby="chain-h">
            <div className="row-between">
              <h2 id="chain-h" className="h2">How it spread</h2>
              <span className="muted" style={{ fontSize: 12.5 }}>Earliest first · {r.spread.copies} copies found</span>
            </div>
            <ol className="chain">
              {r.chain.map((n, i) => (
                <li key={i} className="chain-item">
                  {n.relation && (
                    <span className="chain-edge" aria-label={`then, by ${n.relation}:`}>
                      <span aria-hidden="true">→</span>
                      <small>{n.relation}</small>
                    </span>
                  )}
                  <span className={n.isThisPost ? "chain-node chain-here" : "chain-node"}>
                    <b>{n.name}</b>
                    <small className="muted">{n.isThisPost ? "This post" : n.kind}</small>
                    <small className="mono">{n.language} · {n.time}</small>
                  </span>
                </li>
              ))}
              {r.moreCopies > 0 && (
                <li className="chain-item"><span style={{ fontWeight: 600, fontSize: 13, alignSelf: "center" }}>+{r.moreCopies} more copies</span></li>
              )}
            </ol>
            <span className="muted" style={{ fontSize: 12.5 }}>Arrows show how each link was found: platform data (forward, share) or text similarity (translation, copy).</span>
          </section>

          <section className="card" aria-labelledby="claims-h">
            <h2 id="claims-h" className="h2">Claims in this post</h2>
            {r.claims.map((c) => (
              <div key={c.id} className="claim-row">
                <div className="row-between" style={{ alignItems: "flex-start" }}>
                  <span style={{ fontWeight: 600, fontSize: 15, flex: "1 1 320px", minWidth: 0 }}>{c.text}</span>
                  <VerdictPill verdict={c.verdict} />
                </div>
                <span className="muted" style={{ fontSize: 13.5, maxWidth: "72ch" }}>{c.explanation}</span>
                <span className="muted" style={{ fontSize: 12.5 }}>
                  {c.evidenceSummary} · {c.seenSummary} · <Link to="/dashboard/claims" style={{ fontWeight: 600 }}>Open claim</Link>
                </span>
              </div>
            ))}
          </section>

          <section className="card" aria-labelledby="sim-h">
            <div className="row-between">
              <h2 id="sim-h" className="h2">Same story elsewhere</h2>
              <span className="muted" style={{ fontSize: 12.5 }}>Matched by meaning, across languages</span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table className="table">
                <thead>
                  <tr><th>Lang</th><th>Where</th><th>Text</th><th>Link</th><th style={{ textAlign: "right" }}>Similarity</th></tr>
                </thead>
                <tbody>
                  {r.similar.map((s, i) => (
                    <tr key={i}>
                      <td className="mono">{s.language}</td>
                      <td style={{ whiteSpace: "nowrap" }}><b>{s.author}</b><br /><small className="muted">{s.where}</small></td>
                      <td className="muted">{s.text}</td>
                      <td style={{ whiteSpace: "nowrap" }}>{s.relation}</td>
                      <td className="mono num" style={{ textAlign: "right" }}>{s.similarity.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <aside style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
          <section className="card" aria-labelledby="cat-h">
            <h2 id="cat-h" className="h2">Categories</h2>
            {r.categories.map((k) => (
              <div key={k.name} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div className="row-between"><b>{k.name}</b><span className="mono num">{k.score.toFixed(2)}</span></div>
                <div className="meter meter-blue" role="img" aria-label={`${k.name}: ${k.score.toFixed(2)}`}><div style={{ width: `${k.score * 100}%` }} /></div>
                <small className="muted">{k.method}</small>
              </div>
            ))}
          </section>

          <section className="card" aria-labelledby="gemma-h">
            <h2 id="gemma-h" className="h2">Gemma's reading</h2>
            <div className="kv"><span className="lbl">Tone</span><b>{r.reading.tone}</b></div>
            <div className="kv"><span className="lbl">Likely intent</span><span>{r.reading.intent}</span></div>
            <div className="kv">
              <span className="lbl">Techniques found</span>
              {r.reading.techniques.map((t) => (
                <div key={t.name}><b>{t.name}</b><br /><span className="muted" style={{ fontSize: 13 }}>{t.detail}</span></div>
              ))}
            </div>
            <small className="mono muted">{r.reading.model}</small>
          </section>

          {r.cluster ? (
            <section className="card" aria-labelledby="cl-h">
              <span className="lbl">Cluster {r.cluster.id}</span>
              <h2 id="cl-h" className="h2">{r.cluster.name}</h2>
              <span className="muted" style={{ fontSize: 13 }}>{r.cluster.summary}</span>
              <LanguageBar languages={r.cluster.languages} />
            </section>
          ) : (
            <section className="card" aria-labelledby="cl-h">
              <span className="lbl">Narrative</span>
              <h2 id="cl-h" className="h2">Not part of a known narrative yet</h2>
              <span className="muted" style={{ fontSize: 13 }}>
                It isn't close to any cluster from the latest grouping. If similar posts keep appearing, the next run may group them.
              </span>
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}
