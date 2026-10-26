import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Paged, Publication, PublishingStatus } from "../api/types";
import {
  AvailPill,
  ConfirmButton,
  Empty,
  Notice,
  Progress,
  PubPill,
  useToast,
} from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtRelative, fmtTime } from "../lib/format";

export function PublicationRow({
  p,
  onChange,
}: {
  p: Publication;
  onChange: () => void;
}) {
  const toast = useToast();
  const act = async (action: string, body?: unknown) => {
    try {
      await api(`/publications/${p.id}/${action}`, {
        method: "POST",
        body: body ?? {},
      });
      onChange();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  const uncertain = p.error_code === "upload_outcome_unknown";
  const recoverable = ["failed", "blocked", "cancelled"].includes(p.status);
  return (
    <tr>
      <td>
        {p.clip_title ? (
          <Link to={`/klipp/${p.rendered_clip_id}`}>{p.clip_title}</Link>
        ) : (
          p.title
        )}
        <div className="small muted">
          {p.automatic ? t.publishing.auto : t.publishing.manual}
        </div>
      </td>
      <td>{p.channel_name}</td>
      <td>
        <div className="stack" style={{ gap: "0.35rem" }}>
          <PubPill status={p.status} />
          {p.status === "uploading" && p.progress_percent != null && (
            <Progress value={p.progress_percent} />
          )}
          {p.status === "queued" &&
            p.job?.status === "retry_wait" &&
            p.job.next_attempt_at && (
              <span className="small muted">
                {t.publishing.nextTry} {fmtRelative(p.job.next_attempt_at)} (
                {p.job.attempt_count}/{p.job.max_attempts})
              </span>
            )}
        </div>
      </td>
      <td>
        {p.upload_succeeded ? (
          <div className="stack" style={{ gap: "0.35rem" }}>
            <AvailPill availability={p.availability} />
            {!p.is_public && p.availability !== "missing" && (
              <span className="small muted">
                {p.availability === "scheduled" && p.scheduled_for
                  ? `Släpps ${fmtTime(p.scheduled_for)}`
                  : t.publishing.notPublicYet}
              </span>
            )}
          </div>
        ) : (
          <span className="muted">–</span>
        )}
      </td>
      <td style={{ maxWidth: 420 }}>
        {p.external_url && (
          <a href={p.external_url} target="_blank" rel="noreferrer noopener">
            {t.publishing.open}
          </a>
        )}
        {p.safe_error_message && (
          <div className="small">{p.safe_error_message}</div>
        )}
        {p.notice && <div className="small">{p.notice}</div>}
        {p.rejection_reason && (
          <div className="small">{p.rejection_reason}</div>
        )}
      </td>
      <td className="num">
        <span title={fmtTime(p.created_at)}>{fmtRelative(p.created_at)}</span>
      </td>
      <td>
        <div
          className="row"
          style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}
        >
          {p.upload_succeeded && (
            <button className="small" onClick={() => act("refresh")}>
              {t.publishing.refresh}
            </button>
          )}
          {recoverable && !uncertain && (
            <button className="small" onClick={() => act("retry")}>
              {t.publishing.retry}
            </button>
          )}
          {uncertain && (
            <ConfirmButton
              className="small"
              message="Kontrollera först i YouTube Studio att videon verkligen saknas. Laddar du upp på nytt och den redan finns får du en dubblett."
              onConfirm={() => act("retry", { start_over: true })}
            >
              {t.publishing.retryFresh}
            </ConfirmButton>
          )}
          {["queued", "uploading", "blocked"].includes(p.status) &&
            !p.job?.cancel_requested && (
              <button className="small ghost" onClick={() => act("cancel")}>
                {t.publishing.cancel}
              </button>
            )}
          {(["failed", "cancelled", "blocked"].includes(p.status) ||
            p.availability === "missing") && (
            <ConfirmButton
              className="small ghost"
              message="Ta bort posten? Klippet kan sedan publiceras till kanalen igen."
              onConfirm={async () => {
                try {
                  await api(`/publications/${p.id}`, { method: "DELETE" });
                  onChange();
                } catch (e) {
                  toast(errorMessage(e), "fail");
                }
              }}
            >
              {t.publishing.forget}
            </ConfirmButton>
          )}
        </div>
      </td>
    </tr>
  );
}

/** An upload that is running now or about to run. Reads only local state, never YouTube. */
function isOngoing(p: Publication): boolean {
  return (
    p.status === "uploading" ||
    p.job?.status === "running" ||
    p.job?.status === "pending"
  );
}

export default function Publications() {
  const [filter, setFilter] = useState("");
  const [ongoing, setOngoing] = useState(false);
  const q = usePoll<Paged<Publication> & { counts: Record<string, number> }>(
    (s) =>
      api(`/publications?limit=100${filter ? `&status=${filter}` : ""}`, {
        signal: s,
      }),
    ongoing ? 2000 : 4000,
    [filter],
  );
  useEffect(() => {
    setOngoing(q.data?.items.some(isOngoing) ?? false);
  }, [q.data]);
  const st = usePoll<PublishingStatus>(
    (s) => api("/publishing/status", { signal: s }),
    20000,
  );
  const counts = q.data?.counts ?? {};
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.publishing.title}</h1>
        <p className="muted">{t.publishing.lead}</p>
      </header>
      {st.data && (
        <section className="panel stack" aria-label={t.publishing.quota}>
          <dl className="facts small">
            <dt>{t.publishing.autoTitle}</dt>
            <dd>
              {st.data.auto_publish_global_enabled
                ? t.publishing.autoOn
                : t.publishing.autoOff}{" "}
              <Link to="/kanaler">{t.nav.channels}</Link>
            </dd>
            <dt>{t.publishing.quota}</dt>
            <dd className="num">
              {st.data.quota.insert.used}/{st.data.quota.insert.limit}{" "}
              {t.publishing.quotaUploads}, {st.data.quota.units.used}/
              {st.data.quota.units.limit} enheter. {t.publishing.quotaReset}{" "}
              {fmtTime(st.data.quota.resets_at)}
            </dd>
          </dl>
        </section>
      )}
      <div className="segmented" role="group" aria-label="Filtrera på status">
        {t.publishing.filters.map(([v, label]) => (
          <button
            key={label}
            aria-pressed={filter === v}
            onClick={() => setFilter(v)}
          >
            {label}
            {v &&
              ` (${v
                .split(",")
                .map((s) => counts[s] ?? 0)
                .reduce((a, b) => a + b, 0)})`}
          </button>
        ))}
      </div>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && q.data.items.length === 0 ? (
        <Empty>{t.publishing.empty}</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Klipp</th>
                <th scope="col">Kanal</th>
                <th scope="col">Uppladdning</th>
                <th scope="col">På YouTube</th>
                <th scope="col">Detaljer</th>
                <th scope="col">Skapad</th>
                <th scope="col">
                  <span className="sr-only">Åtgärder</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {q.data?.items.map((p) => (
                <PublicationRow key={p.id} p={p} onChange={q.reload} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
