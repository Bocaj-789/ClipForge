import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { RenderedClip } from "../api/types";
import PublishPanel from "../components/PublishPanel";
import { ScoreBreakdown } from "../components/Scores";
import {
  ConfirmButton,
  Notice,
  ReviewPill,
  useToast,
} from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtBytes, fmtDuration, fmtTime } from "../lib/format";

export default function ClipDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const q = usePoll<RenderedClip>(
    (s) => api(`/clips/${id}`, { signal: s }),
    0,
    [id],
  );
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  useEffect(() => {
    if (q.data) {
      setTitle(q.data.title_suggestion);
      setDesc(q.data.description_suggestion);
    }
  }, [q.data]);
  if (!q.data)
    return (
      <div className="page">
        {q.error ? <Notice tone="fail">{q.error}</Notice> : t.common.loading}
      </div>
    );
  const c = q.data;
  const act = async (
    path: string,
    opts: Parameters<typeof api>[1] = { method: "POST" },
  ) => {
    try {
      await api(`/clips/${c.id}${path}`, opts);
      q.reload();
      return true;
    } catch (e) {
      toast(errorMessage(e), "fail");
      return false;
    }
  };
  const p = c.proposal;
  return (
    <div className="page">
      <header className="page-head">
        <Link to="/klipp" className="small">
          {t.nav.clips}
        </Link>
        <h1 style={{ fontSize: "var(--s-3)" }}>{c.title_suggestion}</h1>
        <div className="row">
          <ReviewPill status={c.review_status} />
        </div>
      </header>
      <div className="detail">
        <div className="stack">
          <video
            src={c.video_url}
            poster={c.thumbnail_url}
            controls
            preload="metadata"
            playsInline
          >
            <track
              kind="captions"
              src={c.captions_url}
              srcLang="sv"
              label="Svenska"
            />
          </video>
          <div className="row">
            <button
              className="primary"
              onClick={() => act("/approve")}
              disabled={c.review_status === "approved"}
            >
              {t.clips.approve}
            </button>
            <button
              onClick={() => act("/reject")}
              disabled={c.review_status === "rejected"}
            >
              {t.clips.reject}
            </button>
          </div>
          <div className="row small">
            <a className="button small" href={`${c.video_url}?download=1`}>
              {t.clips.download}
            </a>
            <a className="button small" href={c.captions_url}>
              {t.clips.captions}
            </a>
          </div>
          <dl className="facts small">
            <dt>Längd</dt>
            <dd className="num">{fmtDuration(c.duration_seconds)}</dd>
            <dt>Format</dt>
            <dd className="num">
              {c.width}×{c.height}, {c.render_profile}
            </dd>
            <dt>Storlek</dt>
            <dd className="num">{fmtBytes(c.file_size_bytes)}</dd>
            <dt>Renderad</dt>
            <dd>
              {fmtTime(c.created_at)}
              {c.render_seconds ? ` på ${c.render_seconds} s` : ""}
            </dd>
          </dl>
        </div>
        <div className="stack">
          {c.warnings.map((w) => (
            <Notice key={w}>{w}</Notice>
          ))}
          <form
            className="panel"
            onSubmit={async (e) => {
              e.preventDefault();
              if (
                await act("", {
                  method: "PATCH",
                  body: {
                    title_suggestion: title,
                    description_suggestion: desc,
                  },
                })
              )
                toast("Texterna sparades.");
            }}
          >
            <label>
              {t.clips.titleLabel}
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={100}
                required
              />
              <span className="help num">{title.length}/95</span>
            </label>
            <label>
              {t.clips.descLabel}
              <textarea
                value={desc}
                onChange={(e) => setDesc(e.target.value)}
                rows={6}
                maxLength={4500}
              />
            </label>
            <p className="small muted">{c.hashtags.join(" ")}</p>
            <div>
              <button type="submit">{t.common.save}</button>
            </div>
          </form>
          <PublishPanel clip={c} />
          {p && (
            <section className="panel" aria-labelledby="score-h">
              <div className="spread">
                <h2 id="score-h">{t.clips.score}</h2>
                <span className="score">{p.final_score}</span>
              </div>
              <ScoreBreakdown p={p} />
              <p className="small muted">{t.clips.scoreNote}</p>
            </section>
          )}
          {c.source && (
            <section className="panel">
              <h2>{t.clips.source}</h2>
              <p>
                <Link to={`/video/${c.source.id}`}>
                  {c.source.title ?? "Källvideo"}
                </Link>
                {c.source.creator_name ? `, ${c.source.creator_name}` : ""}
              </p>
              {p && (
                <p className="small muted num">
                  {fmtDuration(p.start_seconds)}–{fmtDuration(p.end_seconds)} i
                  originalet
                </p>
              )}
              {p && (
                <blockquote className="small" style={{ margin: 0 }}>
                  {p.transcript_excerpt}
                </blockquote>
              )}
            </section>
          )}
          <div>
            <ConfirmButton
              className="danger"
              message={t.clips.deleteConfirm}
              onConfirm={async () => {
                if (await act("", { method: "DELETE" })) nav("/klipp");
              }}
            >
              {t.common.delete}
            </ConfirmButton>
          </div>
        </div>
      </div>
    </div>
  );
}
