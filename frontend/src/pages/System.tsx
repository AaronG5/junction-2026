import { useState } from "react";
import { api } from "../api/client";
import { HowWellItWorks, MethodSteps } from "../components/Method";
import { ErrorState, Loading } from "../components/States";
import type { NarrativeCheckStatus } from "../api/types";
import { isDemoMode } from "../api/client";
import { DATA_MODES, DataMode, updateSettings, useSettings, useSettingsSync } from "../lib/settings";
import { useApi } from "../lib/useApi";
import { useDocumentTitle } from "../lib/useDocumentTitle";

const MODE_ORDER: DataMode[] = ["central", "hybrid", "local"];

/** Where answers come from, and the AI running on this computer. */
export function System() {
  useDocumentTitle("System");
  const settings = useSettings();
  const sync = useSettingsSync();
  const status = useApi(() => api.system(), []);
  const check = useApi(() => api.narrativeStatus(), []);
  const [showHow, setShowHow] = useState(false);
  const isLocal = settings.dataMode === "local";

  return (
    <div className="page">
      <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <h1 className="page-title">System</h1>
        <p className="muted">Where your answers come from, and the AI running on this computer.</p>
      </header>

      <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <fieldset className="modes">
          <legend><h2 className="h2" style={{ fontSize: 18 }}>Data source</h2></legend>
          {MODE_ORDER.map((m) => (
            <label key={m} className="mode" data-checked={settings.dataMode === m}>
              {/* Name = the mode only; the longer text is its description. */}
              <input type="radio" name="data-mode" value={m} checked={settings.dataMode === m}
                aria-labelledby={`mode-${m}-name`} aria-describedby={`mode-${m}-text`}
                onChange={() => updateSettings({ dataMode: m })} />
              <span style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
                <b id={`mode-${m}-name`}>{DATA_MODES[m].name}</b>
                <span id={`mode-${m}-text`} className="muted" style={{ fontSize: 13 }}>{DATA_MODES[m].text}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="card privacy">
          <p data-testid="privacy-note" style={{ flex: "1 1 360px", minWidth: 0 }}>{DATA_MODES[settings.dataMode].privacy}</p>
          <label className="share" data-disabled={isLocal}>
            <input type="checkbox" checked={settings.shareAnalyses} disabled={isLocal} aria-describedby="share-note"
              onChange={(e) => updateSettings({ shareAnalyses: e.target.checked })} />
            <span>Share my analyses with the central server</span>
          </label>
          <small id="share-note" className="muted" style={{ flexBasis: "100%" }}>
            {isLocal ? "Not available when nothing may leave this computer." : "Off by default. Shared analyses help everyone, but reveal which links you checked."}
          </small>
        </div>
        {sync.state === "failed" ? (
          <p className="ferror" role="alert">{sync.message}</p>
        ) : (
          <small className="muted" data-testid="settings-sync">
            {isDemoMode() ? "Demo mode: saved in this browser only."
              : sync.state === "saving" ? "Saving…"
              : sync.state === "saved" ? "Saved for everyone using Veritas Lens on this computer."
              : sync.message ?? "Saved in this browser."}
          </small>
        )}
      </section>

      {status.error ? (
        <ErrorState error={status.error} onRetry={status.reload} />
      ) : !status.data ? (
        <Loading label="Checking the local AI" />
      ) : (
        <>
          <section className="card" aria-labelledby="ai-h" style={{ gap: 16 }}>
            <div className="row-between">
              <h2 id="ai-h" className="h2" style={{ fontSize: 18 }}>AI on this computer</h2>
              <RunState running={status.data.ai.running} />
            </div>
            <div className="split" style={{ gap: 16 }}>
              <Fact label="Chat and analysis model" value={status.data.ai.chatModel} note={status.data.ai.chatModelNote} mono />
              <Fact label="Text fingerprints" value={status.data.ai.embeddingModel} note={status.data.ai.embeddingNote} mono />
              <Fact label="This computer" value={status.data.ai.hardware} note={status.data.ai.hardwareNote} />
            </div>
            {status.data.ai.upgrade && (
              <div className="upgrade">
                <span style={{ flex: "1 1 360px", minWidth: 0 }}>
                  <b>A stronger model fits this computer:</b> {status.data.ai.upgrade.model} ({status.data.ai.upgrade.downloadGb} GB download).{" "}
                  {status.data.ai.upgrade.note}
                </span>
                <button type="button" className="btn btn-ghost" aria-expanded={showHow} onClick={() => setShowHow(!showHow)}>
                  {showHow ? "Hide" : "How to switch"}
                </button>
              </div>
            )}
            {showHow && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13.5 }}>
                <span>Close Veritas Lens, then run the launcher with this option. If the download is interrupted, it continues where it stopped.</span>
                <code className="mono codeline">launch.bat --reselect</code>
                <span className="muted" style={{ fontSize: 12.5 }}>On Linux or macOS: <code className="mono">./launch.sh --reselect</code></span>
              </div>
            )}
          </section>

          <section className="card" aria-labelledby="svc-h">
            <h2 id="svc-h" className="h2" style={{ fontSize: 18 }}>Services</h2>
            <div style={{ overflowX: "auto" }}>
              <table className="table" style={{ minWidth: 560 }}>
                <tbody>
                  {status.data.services.map((s) => (
                    <tr key={s.name}>
                      <td><b>{s.name}</b></td>
                      <td className="muted">{s.role}</td>
                      <td className="mono" style={{ fontSize: 12.5 }}>{s.address}</td>
                      <td style={{ textAlign: "right" }}><RunState running={s.running} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <small className="muted">
              These are the addresses the app uses on this computer. Whether the dashboard itself is
              reachable from the network is chosen when you start it (<span className="mono">--host</span>;
              127.0.0.1 means this computer only).
            </small>
          </section>
        </>
      )}

      {check.error ? (
        <ErrorState error={check.error} onRetry={check.reload} />
      ) : !check.data ? (
        <Loading label="Checking the narrative check" />
      ) : (
        <NarrativeCheckCard s={check.data} />
      )}
      <HowWellItWorks headingId="well-h" />
    </div>
  );
}

const n = (x: number) => x.toLocaleString("en-US");

function NarrativeCheckCard({ s }: { s: NarrativeCheckStatus }) {
  const t = s.thresholds;
  return (
    <section className="card" aria-labelledby="nc-h" style={{ gap: 16 }}>
      <div className="row-between">
        <h2 id="nc-h" className="h2" style={{ fontSize: 18 }}>Narrative check</h2>
        <RunState running={s.available} label={s.available ? "Ready" : "Not available"} />
      </div>
      {!s.available ? (
        <span className="muted">{s.reason ?? "The narrative check can't run on this server."}</span>
      ) : (
        <>
          {s.claims === 0 && s.telegramPosts === 0 && (
            <span className="ferror">
              The narrative bank isn't downloaded. Run <code className="mono">uv run python backend/fetch_narrative_bank.py</code>, then restart.
            </span>
          )}
          <div className="split" style={{ gap: 16 }}>
            <Fact label="Narrative claims" value={n(s.claims)} note="EUvsDisinfo, Overload and Antibot4Navalny. These decide the verdict." />
            <Fact label="State-media narratives" value={n(s.echoClaims)} note="RT and RIA Novosti. Shown as context only." />
            <Fact label="Telegram source posts" value={n(s.telegramPosts)} note="Public posts from pro-Kremlin channels, for the copy match." />
          </div>
          <div className="split" style={{ gap: 16 }}>
            <Fact label="Judge" value={s.judge ? "On" : "Off"}
              note={s.judge ? "The fine-tuned Gemma 4 E2B judge decides whether a post asserts a claim." : "No judge verdicts: only copies of Telegram posts are flagged."} />
            <Fact label="Fingerprints" value={s.embeddingModel ?? "EmbeddingGemma"} note={s.dim ? `${s.dim} numbers per text.` : "Same model as the rest of Veritas Lens."} mono />
            <Fact label="Thresholds" mono
              value={["copy_strong", "candidate_gate", "echo_min"].filter((k) => k in t).map((k) => `${k} ${t[k]}`).join(" · ") || "defaults"}
              note="Copy match, judge gate and state-media echo, as similarity (0 to 1)." />
          </div>
        </>
      )}
      <details className="ncheck-more">
        <summary>How it works</summary>
        <MethodSteps />
      </details>
    </section>
  );
}

function RunState({ running, label }: { running: boolean; label?: string }) {
  return (
    <span className="runstate" data-running={running}>
      <i aria-hidden="true" />{label ?? (running ? "Running" : "Stopped")}
    </span>
  );
}

function Fact({ label, value, note, mono = false }: { label: string; value: string; note: string; mono?: boolean }) {
  return (
    <div style={{ flex: "1 1 260px", minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
      <span className="lbl">{label}</span>
      <span className={mono ? "mono" : undefined} style={{ fontWeight: 500 }}>{value}</span>
      <span className="muted" style={{ fontSize: 13 }}>{note}</span>
    </div>
  );
}
