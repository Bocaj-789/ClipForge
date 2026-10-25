import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Paged, RenderedClip } from "../api/types";
import { Empty, Notice, ReviewPill } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtDuration } from "../lib/format";

const FILTERS = ["needs_review", "approved", "rejected", "all"] as const;

export default function Clips() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("needs_review");
  const q = usePoll<Paged<RenderedClip>>((s) => api(`/clips?limit=60&review_status=${filter}`, { signal: s }), 8000, [
    filter,
  ]);
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.clips.title}</h1>
        <p className="muted">{t.clips.scoreNote}</p>
      </header>
      <div className="segmented" role="group" aria-label="Filtrera på granskning">
        {FILTERS.map((f) => (
          <button key={f} aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {f === "all" ? "Alla" : t.review[f]}
          </button>
        ))}
      </div>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && q.data.items.length === 0 ? (
        <Empty>{t.clips.empty}</Empty>
      ) : (
        <div className="clip-grid">
          {q.data?.items.map((c) => (
            <article key={c.id} className="clip">
              <Link to={`/klipp/${c.id}`} className="frame" aria-label={`Öppna ${c.title_suggestion}`}>
                <img src={c.thumbnail_url} alt="" loading="lazy" />
                <span className="dur num">{fmtDuration(c.duration_seconds)}</span>
              </Link>
              <div className="spread">
                <ReviewPill status={c.review_status} />
                {c.proposal && (
                  <span className="num small" title="Heuristisk poäng 0–100">
                    {c.proposal.final_score}
                  </span>
                )}
              </div>
              <Link to={`/klipp/${c.id}`} className="title-line" style={{ color: "var(--text)" }}>
                {c.title_suggestion}
              </Link>
              {c.source?.title && <span className="small muted">{c.source.title}</span>}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
