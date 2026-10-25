import { api } from "../api/client";
import { Notice } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";

interface A {
  renders_per_day: Record<string, number>;
  renders_total: number;
  review: Record<string, number>;
  transcription_seconds_per_media_second_median: number | null;
  render_seconds_per_clip_second_median: number | null;
  jobs_by_status: Record<string, number>;
  platform_metrics: { available: boolean; reason: string };
}

export default function Analytics() {
  const q = usePoll<A>((s) => api("/analytics/summary", { signal: s }), 30000);
  const a = q.data;
  const max = a ? Math.max(1, ...Object.values(a.renders_per_day)) : 1;
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.analytics.title}</h1>
        <p className="muted">{t.analytics.lead}</p>
      </header>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {a && (
        <>
          <div className="grid-2">
            <section className="panel">
              <h2>Bearbetning</h2>
              <dl className="facts">
                <dt>{t.analytics.renders}</dt>
                <dd className="num">{a.renders_total}</dd>
                <dt>{t.analytics.trSpeed}</dt>
                <dd className="num">{a.transcription_seconds_per_media_second_median ?? t.analytics.none}</dd>
                <dt>{t.analytics.rdSpeed}</dt>
                <dd className="num">{a.render_seconds_per_clip_second_median ?? t.analytics.none}</dd>
                <dt>Granskning</dt>
                <dd className="num">
                  {a.review.approved ?? 0} godkända, {a.review.rejected ?? 0} avvisade, {a.review.needs_review ?? 0}{" "}
                  väntar
                </dd>
              </dl>
            </section>
            <section className="panel">
              <h2>YouTube-statistik</h2>
              <p className="muted">
                {t.analytics.unavailable}. {a.platform_metrics.reason}
              </p>
            </section>
          </div>
          <section className="section" aria-labelledby="per-day">
            <h2 id="per-day">{t.analytics.perDay}</h2>
            {Object.keys(a.renders_per_day).length === 0 ? (
              <p className="muted">{t.analytics.none}</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Datum</th>
                      <th scope="col">Antal</th>
                      <th scope="col" style={{ width: "60%" }}>
                        <span className="sr-only">Stapel</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(a.renders_per_day).map(([d, n]) => (
                      <tr key={d}>
                        <td className="num">{d}</td>
                        <td className="num">{n}</td>
                        <td>
                          <div className="meter" aria-hidden="true">
                            <span style={{ width: `${(100 * n) / max}%`, background: "var(--purple)" }} />
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
