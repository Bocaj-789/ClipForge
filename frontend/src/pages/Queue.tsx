import { useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Job, Paged } from "../api/types";
import { Empty, JobPill, Notice, Progress, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtRelative, fmtTime } from "../lib/format";

const FILTERS: [string, string][] = [
  ["pending,running,retry_wait", "Aktiva"],
  ["failed,blocked", "Problem"],
  ["succeeded", "Klara"],
  ["cancelled", "Avbrutna"],
  ["", t.queue.filterAll],
];

export default function Queue() {
  const [filter, setFilter] = useState(FILTERS[0]![0]);
  const [type, setType] = useState("");
  const toast = useToast();
  const sort = filter.startsWith("pending") ? "priority" : "updated";
  const q = usePoll<Paged<Job> & { counts: Record<string, number> }>(
    (s) =>
      api(`/jobs?limit=100&sort=${sort}${filter ? `&status=${filter}` : ""}${type ? `&job_type=${type}` : ""}`, {
        signal: s,
      }),
    3000,
    [filter, type],
  );
  const act = async (id: string, action: string) => {
    try {
      await api(`/jobs/${id}/${action}`, { method: "POST" });
      q.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  const counts = q.data?.counts ?? {};
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.queue.title}</h1>
        <p className="muted">
          Brådskande inlämningar tas först när en arbetsplats blir ledig. Pågående jobb avbryts inte av nya. Vart femte
          jobb tas i ankomstordning så att inget fastnar.
        </p>
      </header>
      <div className="spread">
        <div className="segmented" role="group" aria-label="Filtrera på status">
          {FILTERS.map(([v, label]) => (
            <button key={label} aria-pressed={filter === v} onClick={() => setFilter(v)}>
              {label}
              {v &&
                ` (${v
                  .split(",")
                  .map((s) => counts[s] ?? 0)
                  .reduce((a, b) => a + b, 0)})`}
            </button>
          ))}
        </div>
        <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
          Typ
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">Alla</option>
            {Object.entries(t.pipeline.stages).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && q.data.items.length === 0 ? (
        <Empty>{t.queue.empty}</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Steg</th>
                <th scope="col">Prioritet</th>
                <th scope="col">{t.queue.attempts}</th>
                <th scope="col">Tid</th>
                <th scope="col">Meddelande</th>
                <th scope="col">
                  <span className="sr-only">Åtgärder</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {q.data?.items.map((j) => (
                <tr key={j.id}>
                  <td>
                    <JobPill status={j.status} />
                    {j.status === "running" && (
                      <div style={{ marginTop: 6 }}>
                        <Progress value={j.progress_percent} />
                      </div>
                    )}
                  </td>
                  <td>
                    {j.candidate_video_id ? (
                      <Link to={`/video/${j.candidate_video_id}`}>{t.pipeline.stages[j.job_type] ?? j.job_type}</Link>
                    ) : (
                      (t.pipeline.stages[j.job_type] ?? j.job_type)
                    )}
                    {j.stage && <div className="muted">{j.stage}</div>}
                  </td>
                  <td>{t.priority[j.priority]}</td>
                  <td className="num">
                    {j.attempt_count}/{j.max_attempts}
                  </td>
                  <td className="num">
                    {j.status === "retry_wait" && j.next_attempt_at ? (
                      <span title={fmtTime(j.next_attempt_at)}>
                        {t.queue.next} {fmtRelative(j.next_attempt_at)}
                      </span>
                    ) : (
                      <span title={fmtTime(j.updated_at)}>{fmtRelative(j.updated_at)}</span>
                    )}
                  </td>
                  <td style={{ maxWidth: 420 }}>{j.safe_error_message}</td>
                  <td>
                    <div className="row" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                      {["failed", "blocked", "cancelled", "retry_wait"].includes(j.status) && (
                        <button className="small" onClick={() => act(j.id, "retry")}>
                          {t.common.retry}
                        </button>
                      )}
                      {["pending", "retry_wait", "running", "blocked"].includes(j.status) && !j.cancel_requested && (
                        <button className="small ghost" onClick={() => act(j.id, "cancel")}>
                          {t.queue.cancel}
                        </button>
                      )}
                      {j.cancel_requested && <span className="small muted">Avbryts…</span>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
