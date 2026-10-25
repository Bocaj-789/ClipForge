import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { AgentStatus, Job, Paged, Summary } from "../api/types";
import { ConfirmButton, Empty, JobPill, Notice, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtBytes, fmtRelative } from "../lib/format";

// The pipeline is a real sequence, so it is shown as one. Colour follows the temper scale.
const STAGES: { type: string; heat: string }[] = [
  { type: "fetch_metadata", heat: "var(--straw)" },
  { type: "analyze", heat: "var(--straw)" },
  { type: "transcribe", heat: "var(--bronze)" },
  { type: "extract_clips", heat: "var(--bronze)" },
  { type: "render", heat: "var(--purple)" },
];

export function AgentControls({ agent, onChange }: { agent: AgentStatus; onChange: () => void }) {
  const toast = useToast();
  const act = async (path: string) => {
    try {
      await api(`/agent/${path}`, { method: "POST" });
      onChange();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  const paused = ["paused", "stopped", "draining"].includes(agent.desired_state);
  return (
    <div className="controls">
      {paused ? (
        <button className="primary" onClick={() => act("resume")}>
          {t.agent.resume}
        </button>
      ) : (
        <>
          <button onClick={() => act("pause")}>{t.agent.pause}</button>
          <button onClick={() => act("drain")}>{t.agent.drain}</button>
        </>
      )}
      <ConfirmButton
        className="danger"
        message={t.agent.stopConfirm}
        onConfirm={() => act("stop")}
        confirmLabel={t.agent.stop}
      >
        {t.agent.stop}
      </ConfirmButton>
    </div>
  );
}

export default function Overview() {
  const summary = usePoll<Summary>((s) => api("/dashboard/summary", { signal: s }), 4000);
  const active = usePoll<Paged<Job>>(
    (s) => api("/jobs?status=pending,running,retry_wait&limit=200", { signal: s }),
    4000,
  );
  const s = summary.data;
  if (!s) {
    return (
      <div className="page">
        {summary.error ? <Notice tone="fail">{summary.error}</Notice> : <p>{t.common.loading}</p>}
      </div>
    );
  }
  const byType = (type: string) => {
    const js = (active.data?.items ?? []).filter((j) => j.job_type === type);
    return { total: js.length, running: js.filter((j) => j.status === "running").length };
  };
  const agent = s.agent;
  return (
    <div className="page">
      <section className="hearth" data-state={agent.state} aria-labelledby="agent-state">
        <div className="stack" style={{ gap: "0.5rem" }}>
          <span className="muted small">ClipForge-agenten</span>
          <div id="agent-state" className="state">
            {t.agent[agent.state]}
          </div>
          {agent.reason && <p className="muted">{agent.reason}</p>}
          {Object.values(agent.degraded).map((d) => (
            <p key={d} className="small" style={{ color: "var(--straw)" }}>
              {d}
            </p>
          ))}
        </div>
        <AgentControls agent={agent} onChange={summary.reload} />
      </section>

      <section className="section" aria-labelledby="pipe">
        <h2 id="pipe">{t.pipeline.title}</h2>
        <ol className="pipeline">
          {STAGES.map((st) => {
            const c = byType(st.type);
            return (
              <li key={st.type} style={{ ["--heat" as string]: c.total ? st.heat : "var(--line)" }}>
                <div className="count">{c.total}</div>
                <div className="label">{t.pipeline.stages[st.type]}</div>
                <div className="sub">{c.running ? `${c.running} pågår` : "\u00a0"}</div>
              </li>
            );
          })}
          <li style={{ ["--heat" as string]: s.clips_awaiting_review ? "var(--purple)" : "var(--line)" }}>
            <div className="count">
              <Link to="/klipp" style={{ color: "inherit", textDecoration: "none" }}>
                {s.clips_awaiting_review}
              </Link>
            </div>
            <div className="label">{t.overview.review}</div>
            <div className="sub">{s.rendered_last_24h} senaste dygnet</div>
          </li>
        </ol>
      </section>

      <div className="grid-2">
        <section className="panel" aria-labelledby="problems">
          <div className="spread">
            <h2 id="problems">{t.overview.errors}</h2>
            <Link to="/ko" className="small">
              {t.nav.queue}
            </Link>
          </div>
          {s.recent_errors.length === 0 ? (
            <p className="muted">{t.overview.noErrors}</p>
          ) : (
            <ul className="list">
              {s.recent_errors.map((j) => (
                <li key={j.id}>
                  <div className="grow">
                    <div className="row">
                      <JobPill status={j.status} />
                      <span className="small muted">{t.pipeline.stages[j.job_type]}</span>
                    </div>
                    <p className="small">{j.safe_error_message}</p>
                  </div>
                  {j.candidate_video_id && (
                    <Link className="small" to={`/video/${j.candidate_video_id}`}>
                      Öppna
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-labelledby="services">
          <h2 id="services">{t.overview.services}</h2>
          <dl className="facts">
            <dt>{t.overview.transcription}</dt>
            <dd>{s.transcription.available ? `Whisper ${s.transcription.model}` : s.transcription.reason}</dd>
            <dt>{t.overview.ai}</dt>
            <dd>{s.llm_provider === "ollama" ? "Ollama (lokal)" : "Regelbaserad heuristik"}</dd>
            <dt>{t.overview.storage}</dt>
            <dd>
              <div className="stack" style={{ gap: "0.35rem" }}>
                <span className="num">
                  {fmtBytes(s.storage.used_bytes)} av {fmtBytes(s.storage.budget_bytes)},{" "}
                  {fmtBytes(s.storage.disk_free_bytes)} ledigt på disken
                </span>
                <div className="meter" data-level={s.storage.level}>
                  <span style={{ width: `${Math.min(100, s.storage.used_percent)}%` }} />
                </div>
                {s.storage.reason && (
                  <span className="small" style={{ color: "var(--straw)" }}>
                    {s.storage.reason}
                  </span>
                )}
              </div>
            </dd>
            <dt>{t.overview.publications}</dt>
            <dd>{s.auto_publish_global_enabled ? t.overview.autoPublishOn : t.overview.autoPublishOff}</dd>
          </dl>
          <p className="small muted">{t.overview.paidApis}</p>
        </section>

        <section className="panel" aria-labelledby="sources">
          <div className="spread">
            <h2 id="sources">{t.overview.sources}</h2>
            <Link to="/upptackt" className="small">
              {t.nav.discovery}
            </Link>
          </div>
          {s.sources.length === 0 ? (
            <Empty>{t.overview.noSources}</Empty>
          ) : (
            <ul className="list">
              {s.sources.map((src) => (
                <li key={src.id}>
                  <span className="grow">{src.name}</span>
                  <span className="small muted">
                    {src.last_error ? (
                      <span style={{ color: "var(--fail)" }}>{src.last_error}</span>
                    ) : src.last_success_at ? (
                      `OK ${fmtRelative(src.last_success_at)}`
                    ) : (
                      "Ej kontrollerad"
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
