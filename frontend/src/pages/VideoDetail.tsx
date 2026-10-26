import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Candidate, Job, Proposal } from "../api/types";
import { ScoreBreakdown } from "../components/Scores";
import { UploadButton } from "../components/Upload";
import { CandPill, ConfirmButton, Empty, JobPill, Notice, Progress, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtDuration, fmtRelative } from "../lib/format";

interface Detail {
  candidate: Candidate;
  description: string | null;
  transcript: {
    language: string;
    engine: string;
    model: string;
    processing_seconds: number;
    chars: number;
    preview: string;
  } | null;
  proposals: Proposal[];
  jobs: Job[];
}

function ProposalRow({ p, onChange }: { p: Proposal; onChange: () => void }) {
  const toast = useToast();
  const [style, setStyle] = useState("");
  const [crop, setCrop] = useState("");
  const [offset, setOffset] = useState(p.crop_offset ?? 0.5);
  const render = async () => {
    try {
      await api(`/proposals/${p.id}/render`, {
        body: {
          caption_style: style || null,
          crop_mode: crop || null,
          crop_offset: p.crop_offset != null || offset !== 0.5 ? offset : null,
        },
      });
      toast("Rendering köad.");
      onChange();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  return (
    <li>
      <div className="grow stack" style={{ gap: "0.5rem" }}>
        <div className="spread">
          <span className="title-line">{p.title_suggestion}</span>
          <span className="score">{p.final_score}</span>
        </div>
        <span className="small muted num">
          {fmtDuration(p.start_seconds)}–{fmtDuration(p.end_seconds)} ({Math.round(p.duration_seconds)} s),{" "}
          {t.proposalStatus[p.status] ?? p.status}
        </span>
        <p className="small">{p.transcript_excerpt}</p>
        <details>
          <summary className="small">Poängdetaljer</summary>
          <ScoreBreakdown p={p} />
        </details>
        <div className="row small">
          <select aria-label="Undertextstil" value={style} onChange={(e) => setStyle(e.target.value)}>
            <option value="">Standardstil</option>
            <option value="bold">Fet</option>
            <option value="clean">Ren</option>
            <option value="karaoke">Karaoke</option>
            <option value="none">Inga undertexter</option>
          </select>
          <select aria-label="Beskärning" value={crop} onChange={(e) => setCrop(e.target.value)}>
            <option value="">Standardbeskärning</option>
            <option value="center_crop">Beskär till 9:16</option>
            <option value="fit_blur">Hela bilden på suddig bakgrund</option>
          </select>
          <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
            Bildutsnitt
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={offset}
              onChange={(e) => setOffset(Number(e.target.value))}
              aria-valuetext={offset < 0.4 ? "vänster" : offset > 0.6 ? "höger" : "mitten"}
            />
          </label>
          <button className="small" onClick={render}>
            {t.video.render}
          </button>
        </div>
      </div>
    </li>
  );
}

export default function VideoDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const q = usePoll<Detail>((s) => api(`/candidates/${id}`, { signal: s }), 4000, [id]);
  if (!q.data) return <div className="page">{q.error ? <Notice tone="fail">{q.error}</Notice> : t.common.loading}</div>;
  const { candidate: c, transcript, proposals, jobs } = q.data;
  const act = async (path: string, opts: Parameters<typeof api>[1] = { method: "POST" }) => {
    try {
      await api(`/candidates/${c.id}${path}`, opts);
      q.reload();
      return true;
    } catch (e) {
      toast(errorMessage(e), "fail");
      return false;
    }
  };
  const busy = jobs.some((j) => ["pending", "running", "retry_wait"].includes(j.status));
  return (
    <div className="page">
      <header className="page-head">
        <Link to="/inkorg" className="small">
          {t.nav.inbox}
        </Link>
        <h1 style={{ fontSize: "var(--s-3)" }}>{c.title ?? "Namnlös video"}</h1>
        <div className="row small muted">
          <CandPill status={c.status} />
          {c.creator_name && <span>{c.creator_name}</span>}
          {c.duration_seconds != null && <span className="num">{fmtDuration(c.duration_seconds)}</span>}
          {c.canonical_url && (
            <a href={c.canonical_url} target="_blank" rel="noopener noreferrer">
              Öppna original
            </a>
          )}
        </div>
      </header>

      {c.blocked_reason && (
        <Notice>
          <div className="stack" style={{ gap: "0.5rem" }}>
            <span>{c.blocked_reason}</span>
            {!c.has_media && (
              <UploadButton label={t.inbox.attach} params={{ candidate_id: c.id }} onDone={() => q.reload()} />
            )}
          </div>
        </Notice>
      )}

      <div className="grid-2">
        <section className="panel">
          <h2>{t.video.transcript}</h2>
          {transcript ? (
            <>
              <p className="small muted">
                {transcript.engine} {transcript.model}, språk {transcript.language},{" "}
                {Math.round(transcript.processing_seconds)} s
              </p>
              <p className="small" style={{ maxHeight: 180, overflow: "auto" }}>
                {transcript.preview}
              </p>
            </>
          ) : (
            <p className="muted">Ingen transkription ännu.</p>
          )}
          <div className="row">
            <button onClick={() => act("/reprocess")} disabled={busy || !c.has_media}>
              {t.video.reprocess}
            </button>
            <button className="ghost" onClick={() => act("/skip")} disabled={c.status === "skipped"}>
              {t.video.skip}
            </button>
            <ConfirmButton
              className="danger"
              message={t.video.deleteConfirm}
              onConfirm={async () => {
                if (await act("", { method: "DELETE" })) nav("/inkorg");
              }}
            >
              {t.common.delete}
            </ConfirmButton>
          </div>
        </section>
      </div>

      <section className="section" aria-labelledby="props">
        <h2 id="props">{t.video.proposals}</h2>
        {proposals.length === 0 ? (
          <Empty>{t.video.noProposals}</Empty>
        ) : (
          <ul className="list">
            {proposals.map((p) => (
              <ProposalRow key={p.id} p={p} onChange={q.reload} />
            ))}
          </ul>
        )}
      </section>

      <section className="section" aria-labelledby="jobs">
        <h2 id="jobs">{t.video.jobs}</h2>
        <ul className="list">
          {jobs.map((j) => (
            <li key={j.id}>
              <div className="row grow">
                <JobPill status={j.status} />
                <span>{t.pipeline.stages[j.job_type]}</span>
                {j.status === "running" && (
                  <Progress value={j.progress_percent} label={t.pipeline.stages[j.job_type]} showValue />
                )}
                <span className="small muted">{fmtRelative(j.updated_at)}</span>
              </div>
              {j.safe_error_message && <p className="small">{j.safe_error_message}</p>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
