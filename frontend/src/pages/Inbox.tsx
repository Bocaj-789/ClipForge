import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Candidate, Paged } from "../api/types";
import { UploadButton } from "../components/Upload";
import { CandPill, Empty, JobPill, Notice, Progress, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtDuration, fmtRelative } from "../lib/format";

export function CandidateRow({ c, onChange }: { c: Candidate; onChange: () => void }) {
  const toast = useToast();
  const j = c.latest_job;
  const prioritize = async () => {
    try {
      await api(`/candidates/${c.id}/prioritize`, { body: { urgent: true } });
      onChange();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  return (
    <li>
      <div className="grow stack" style={{ gap: "0.35rem" }}>
        <Link to={`/video/${c.id}`} className="title-line" style={{ color: "var(--text)" }}>
          {c.title ?? c.canonical_url ?? "Namnlös video"}
        </Link>
        <div className="row small muted">
          <CandPill status={c.status} />
          <span>{t.priority[c.priority]}</span>
          {c.creator_name && <span>{c.creator_name}</span>}
          {c.duration_seconds != null && <span className="num">{fmtDuration(c.duration_seconds)}</span>}
          <span>{fmtRelative(c.discovered_at)}</span>
        </div>
        {c.blocked_reason && (
          <p className="small" style={{ color: "var(--straw)" }}>
            {c.blocked_reason}
          </p>
        )}
        {c.last_error && c.status === "failed" && (
          <p className="small" style={{ color: "var(--fail)" }}>
            {c.last_error}
          </p>
        )}
      </div>
      <div className="row">
        {j && ["pending", "running", "retry_wait"].includes(j.status) && (
          <span className="row small">
            <JobPill status={j.status} />
            <span className="muted">{t.pipeline.stages[j.job_type]}</span>
            {j.status === "running" && <Progress value={j.progress_percent} />}
          </span>
        )}
        {c.priority > 0 && ["queued", "discovered", "analyzing"].includes(c.status) && (
          <button className="small" onClick={prioritize}>
            Gör brådskande
          </button>
        )}
        {!c.has_media && ["blocked", "awaiting_media"].includes(c.status) && (
          <UploadButton
            label={t.inbox.attach}
            params={{ candidate_id: c.id }}
            onDone={() => {
              toast(t.inbox.added);
              onChange();
            }}
          />
        )}
      </div>
    </li>
  );
}

export default function Inbox() {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [urgent, setUrgent] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const list = usePoll<Paged<Candidate>>((s) => api("/candidates?origin=manual&limit=100", { signal: s }), 4000);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const r = await api<{ duplicate: boolean; priority_raised: boolean }>("/inbox", {
        body: { url, urgent, note: note || null },
      });
      toast(r.duplicate ? (r.priority_raised ? t.inbox.duplicate : t.inbox.duplicateSame) : t.inbox.added);
      setUrl("");
      setNote("");
      list.reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const uploadParams = { urgent: String(urgent) };
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.inbox.title}</h1>
        <p className="muted">{t.inbox.lead}</p>
      </header>

      <form className="panel" onSubmit={submit}>
        <label htmlFor="url">{t.inbox.url}</label>
        <div className="url-input">
          <input
            id="url"
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={t.inbox.urlPlaceholder}
            required
            maxLength={2048}
            aria-invalid={formError ? true : undefined}
          />
          <button className="primary" type="submit" disabled={busy || !url.trim()}>
            {t.inbox.submit}
          </button>
        </div>
        {formError && <Notice tone="fail">{formError}</Notice>}
        <label className="check">
          <input type="checkbox" checked={urgent} onChange={(e) => setUrgent(e.target.checked)} />
          {t.inbox.urgent}
        </label>
        <div className="form-grid">
          <label>
            {t.inbox.note}
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
          </label>
        </div>
        <p className="help">{t.inbox.youtubeNote}</p>
        <div className="spread">
          <span className="help">{t.inbox.uploadHelp}</span>
          <UploadButton
            label={t.inbox.upload}
            params={uploadParams}
            onDone={(r) => {
              toast(r.duplicate ? t.inbox.duplicateSame : t.inbox.added);
              list.reload();
            }}
          />
        </div>
      </form>

      <section className="section" aria-labelledby="inbox-list">
        <div className="spread">
          <h2 id="inbox-list">I kön och nyligen tillagt</h2>
          <span className="small muted">{t.inbox.steps}</span>
        </div>
        {list.error && <Notice tone="fail">{list.error}</Notice>}
        {list.data && list.data.items.length === 0 ? (
          <Empty>{t.inbox.empty}</Empty>
        ) : (
          <ul className="list">
            {list.data?.items.map((c) => (
              <CandidateRow key={c.id} c={c} onChange={list.reload} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
