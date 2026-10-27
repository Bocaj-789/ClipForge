import { useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Candidate, Paged } from "../api/types";
import { ConfirmButton, Empty, Notice, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { fmtTime } from "../lib/format";

const ORIGINS: [string, string][] = [
  ["", "Alla"],
  ["manual_url", "Länk"],
  ["local_upload", "Uppladdad fil"],
  ["discovery", "Upptäckt"],
];

export default function Videos() {
  const [origin, setOrigin] = useState("");
  const toast = useToast();
  const videos = usePoll<Paged<Candidate>>(
    (s) => api(`/candidates?limit=100&unpublished=true${origin ? `&origin=${origin}` : ""}`, { signal: s }),
    0,
    [origin],
  );

  async function remove(c: Candidate) {
    try {
      await api(`/candidates/${c.id}`, { method: "DELETE" });
      toast("Videon togs bort.");
      videos.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  }

  const items = videos.data?.items ?? [];
  const originLabel = (v: string) => ORIGINS.find(([k]) => k === v)?.[1] ?? v;

  return (
    <div className="page">
      <header className="page-head">
        <h1>Videor</h1>
        <p className="muted">
          Videor som ännu inte är publicerade eller schemalagda. Ta bort de du inte vill ha kvar. Publicerade videor
          visas inte här.
        </p>
      </header>

      <div className="spread">
        <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
          Källa
          <select value={origin} onChange={(e) => setOrigin(e.target.value)}>
            {ORIGINS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <span className="small muted">{items.length} visade</span>
      </div>

      {videos.error && <Notice tone="fail">{videos.error}</Notice>}
      {videos.data && items.length === 0 && <Empty>Inga videor.</Empty>}

      <ul className="list">
        {items.map((c) => (
          <li key={c.id}>
            <div className="grow stack" style={{ gap: "0.3rem" }}>
              <span className="title-line">{c.title || c.canonical_url || "Namnlös video"}</span>
              <div className="row small muted">
                <span>{originLabel(c.origin)}</span>
                <span>{c.status}</span>
                <span title={fmtTime(c.discovered_at)}>{fmtTime(c.discovered_at)}</span>
              </div>
            </div>
            <ConfirmButton
              className="small ghost"
              message="Ta bort videon och dess klipp från ClipForge? Videon på YouTube påverkas inte."
              onConfirm={() => remove(c)}
            >
              Ta bort
            </ConfirmButton>
          </li>
        ))}
      </ul>
    </div>
  );
}
