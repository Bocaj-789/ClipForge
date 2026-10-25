import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type {
  Channel,
  Publication,
  PublishResult,
  RenderedClip,
} from "../api/types";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { AvailPill, Notice, PubPill, useToast } from "./ui";

const OUTCOME: Record<string, string> = {
  created: t.clipPublish.queued,
  retried: t.clipPublish.queued,
  already_uploaded: t.clipPublish.already,
  in_progress: t.clipPublish.inProgress,
  blocked: t.clipPublish.blocked,
  not_found: t.clipPublish.blocked,
};

export default function PublishPanel({ clip }: { clip: RenderedClip }) {
  const toast = useToast();
  const channels = usePoll<{ items: Channel[] }>(
    (s) => api("/channels", { signal: s }),
    0,
  );
  const history = usePoll<{ items: Publication[] }>(
    (s) => api(`/clips/${clip.id}/publications`, { signal: s }),
    4000,
    [clip.id],
  );
  const connected = (channels.data?.items ?? []).filter(
    (c) => c.connection_state === "connected",
  );
  const [picked, setPicked] = useState<string[] | null>(null);
  const [visibility, setVisibility] = useState("");
  const [when, setWhen] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PublishResult | null>(null);
  useEffect(() => {
    if (picked === null && channels.data) setPicked(connected.map((c) => c.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channels.data]);
  const selected = picked ?? [];
  const approved = clip.review_status === "approved";

  async function submit() {
    setBusy(true);
    try {
      const r = await api<PublishResult>(`/clips/${clip.id}/publish`, {
        method: "POST",
        body: {
          channel_ids: selected,
          visibility: visibility || null,
          schedule_at: when ? new Date(when).toISOString() : null,
        },
      });
      setResult(r);
      if (r.created) toast(`${r.created} uppladdning(ar) köades.`);
      history.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel stack" aria-labelledby="pub-h">
      <h2 id="pub-h">{t.clipPublish.title}</h2>
      {channels.data && connected.length === 0 ? (
        <p className="small">
          {t.clipPublish.noChannels} <Link to="/kanaler">{t.nav.channels}</Link>
        </p>
      ) : (
        <>
          {!approved && (
            <Notice tone="info">{t.clipPublish.approveFirst}</Notice>
          )}
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="small muted">{t.clipPublish.channels}</legend>
            <div className="stack" style={{ gap: "0.3rem" }}>
              {connected.map((c) => (
                <label key={c.id} className="check">
                  <input
                    type="checkbox"
                    checked={selected.includes(c.id)}
                    onChange={(e) =>
                      setPicked(
                        e.target.checked
                          ? [...selected, c.id]
                          : selected.filter((x) => x !== c.id),
                      )
                    }
                  />
                  {c.display_name}
                  <span className="small muted">
                    {" "}
                    (
                    {t.clipPublish.vis[c.default_visibility] ??
                      c.default_visibility}
                    )
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="form-grid">
            <label>
              {t.clipPublish.visibility}
              <select
                value={visibility}
                onChange={(e) => setVisibility(e.target.value)}
              >
                <option value="">{t.clipPublish.channelDefault}</option>
                <option value="private">{t.clipPublish.vis.private}</option>
                <option value="unlisted">{t.clipPublish.vis.unlisted}</option>
                <option value="public">{t.clipPublish.vis.public}</option>
              </select>
            </label>
            <label>
              {t.clipPublish.schedule}
              <input
                type="datetime-local"
                value={when}
                onChange={(e) => setWhen(e.target.value)}
              />
              <span className="help">{t.clipPublish.scheduleHelp}</span>
            </label>
          </div>
          <div>
            <button
              className="primary"
              onClick={submit}
              disabled={busy || !approved || selected.length === 0}
            >
              {busy ? t.clipPublish.submitting : t.clipPublish.submit}
            </button>
          </div>
          {result && (
            <ul className="list" aria-label="Resultat per kanal">
              {result.results.map((r) => (
                <li
                  key={r.channel_id}
                  style={{ flexDirection: "column", alignItems: "stretch" }}
                >
                  <strong>
                    {r.channel_name}: {OUTCOME[r.outcome] ?? r.outcome}
                  </strong>
                  {r.reasons.map((x) => (
                    <span key={x} className="small">
                      {x}
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {history.data && history.data.items.length > 0 && (
        <div className="stack">
          <h3>{t.clipPublish.history}</h3>
          <ul className="list" aria-label={t.clipPublish.history}>
            {history.data.items.map((p) => (
              <li
                key={p.id}
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <div className="row">
                  <strong>{p.channel_name}</strong>
                  <PubPill status={p.status} />
                  {p.upload_succeeded && (
                    <AvailPill availability={p.availability} />
                  )}
                  {p.external_url && (
                    <a
                      href={p.external_url}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {t.publishing.open}
                    </a>
                  )}
                </div>
                {p.safe_error_message && (
                  <span className="small">{p.safe_error_message}</span>
                )}
                {p.upload_succeeded &&
                  !p.is_public &&
                  p.availability !== "missing" && (
                    <span className="small muted">
                      {t.publishing.notPublicYet}
                    </span>
                  )}
              </li>
            ))}
          </ul>
          <Link className="small" to="/publicering">
            {t.publishing.title}
          </Link>
        </div>
      )}
    </section>
  );
}
