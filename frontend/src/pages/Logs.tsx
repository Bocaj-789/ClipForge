import { useState } from "react";
import { api } from "../api/client";
import { Empty, Notice, Pill } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtTime } from "../lib/format";

interface Ev {
  id: string;
  timestamp: string;
  actor: string;
  severity: string;
  action: string;
  object_type: string | null;
  object_id: string | null;
  result: string;
  details: Record<string, unknown>;
}

export default function Logs() {
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState("");
  const q = usePoll<{ items: Ev[]; total: number }>(
    (s) =>
      api(
        `/logs?limit=200${query ? `&q=${encodeURIComponent(query)}` : ""}${severity ? `&severity=${severity}` : ""}`,
        {
          signal: s,
        },
      ),
    10000,
    [query, severity],
  );
  return (
    <div className="page">
      <header className="page-head spread">
        <h1>{t.logs.title}</h1>
        <a className="button small" href="/api/v1/logs/export">
          {t.logs.export}
        </a>
      </header>
      <div className="row">
        <label>
          {t.logs.search}
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={100} />
        </label>
        <label>
          Allvarlighet
          <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="">Alla</option>
            <option value="info">Info</option>
            <option value="warning">Varning</option>
            <option value="error">Fel</option>
          </select>
        </label>
      </div>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && q.data.items.length === 0 ? (
        <Empty>{t.logs.empty}</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Tid</th>
                <th scope="col">Händelse</th>
                <th scope="col">Av</th>
                <th scope="col">Resultat</th>
                <th scope="col">Detaljer</th>
              </tr>
            </thead>
            <tbody>
              {q.data?.items.map((e) => (
                <tr key={e.id}>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>
                    {fmtTime(e.timestamp)}
                  </td>
                  <td>
                    {e.action}
                    {e.object_type && <div className="muted">{e.object_type}</div>}
                  </td>
                  <td>{e.actor === "user" ? "Du" : "Systemet"}</td>
                  <td>
                    <Pill
                      tone={
                        e.severity === "warning"
                          ? "wait"
                          : e.severity === "error" || e.result !== "ok"
                            ? "fail"
                            : "done"
                      }
                    >
                      {e.result}
                    </Pill>
                  </td>
                  <td className="muted" style={{ maxWidth: 380, overflowWrap: "anywhere" }}>
                    {Object.keys(e.details).length ? JSON.stringify(e.details) : ""}
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
