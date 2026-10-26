import { useState, type ReactNode } from "react";
import { api } from "../api/client";
import { Empty, Notice } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { fmtRelative } from "../lib/format";

type Row = {
  channel_id: string;
  name: string;
  error: string | null;
  subscribers: number | null;
  subscribers_hidden: boolean;
  channel_views: number;
  video_count: number;
  clipforge_videos: number;
  clipforge_views: number;
  clipforge_likes: number;
  clipforge_comments: number;
};

type Totals = {
  channels: number;
  subscribers: number | null;
  subscribers_hidden: boolean;
  channel_views: number;
  video_count: number;
  clipforge_videos: number;
  clipforge_views: number;
  clipforge_likes: number;
  clipforge_comments: number;
};

type StatsResponse = { items: Row[]; totals: Totals; fetched_at: string };

const ALL = "__all__";
const nf = new Intl.NumberFormat("sv-SE");

function fmt(n: number | null | undefined): string {
  return n === null || n === undefined ? "Dold" : nf.format(n);
}

function Card({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="panel stack" style={{ gap: "0.25rem" }}>
      <span className="small muted">{label}</span>
      <strong style={{ fontSize: "1.5rem" }}>{value}</strong>
      {hint && <span className="small muted">{hint}</span>}
    </div>
  );
}

function Cards({ s, hidden }: { s: Omit<Row, "channel_id" | "name" | "error">; hidden: boolean }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
        gap: "0.75rem",
      }}
    >
      <Card
        label="Prenumeranter"
        value={hidden ? "Dold" : fmt(s.subscribers)}
        hint={hidden ? "Kanalen döljer antalet prenumeranter." : undefined}
      />
      <Card label="Totala visningar på kanalen" value={fmt(s.channel_views)} />
      <Card label="Videor på kanalen" value={fmt(s.video_count)} />
      <Card label="Uppladdade via ClipForge" value={fmt(s.clipforge_videos)} />
      <Card label="Visningar på ClipForge-klipp" value={fmt(s.clipforge_views)} />
      <Card label="Gilla-markeringar på ClipForge-klipp" value={fmt(s.clipforge_likes)} />
      <Card label="Kommentarer på ClipForge-klipp" value={fmt(s.clipforge_comments)} />
    </div>
  );
}

export default function Statistics() {
  const [choice, setChoice] = useState(ALL);
  const data = usePoll<StatsResponse>((s) => api("/stats/channels", { signal: s }), 0);
  const items = data.data?.items ?? [];
  const selected = items.find((r) => r.channel_id === choice);

  return (
    <div className="page">
      <header className="page-head">
        <h1>Statistik</h1>
        <p className="muted">
          Siffror från YouTube för dina anslutna kanaler. Välj en kanal, eller se alla kanaler samlat.
        </p>
      </header>

      <div className="spread">
        <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
          Kanal
          <select value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value={ALL}>Alla kanaler (samlat)</option>
            {items.map((r) => (
              <option key={r.channel_id} value={r.channel_id}>
                {r.name}
              </option>
            ))}
          </select>
        </label>
        <div className="row">
          {data.data && (
            <span className="small muted">Hämtad {fmtRelative(data.data.fetched_at)}</span>
          )}
          <button className="small" onClick={() => data.reload()}>
            Uppdatera
          </button>
        </div>
      </div>

      {data.error && <Notice tone="fail">{data.error}</Notice>}
      {data.data && items.length === 0 && (
        <Empty>Inga anslutna kanaler. Lägg till en under Kanaler.</Empty>
      )}

      {data.data && items.length > 0 && choice === ALL && (
        <>
          <section className="section" aria-labelledby="all-h">
            <h2 id="all-h">Alla kanaler samlat</h2>
            <Cards s={data.data.totals} hidden={data.data.totals.subscribers === null} />
          </section>
          <section className="section" aria-labelledby="per-h">
            <h2 id="per-h">Per kanal</h2>
            <div style={{ overflowX: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th>Kanal</th>
                    <th>Prenumeranter</th>
                    <th>Visningar på kanalen</th>
                    <th>Videor</th>
                    <th>ClipForge-klipp</th>
                    <th>ClipForge-visningar</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((r) => (
                    <tr key={r.channel_id}>
                      <td>
                        <button className="small ghost" onClick={() => setChoice(r.channel_id)}>
                          {r.name}
                        </button>
                        {r.error && <span className="small"> {r.error}</span>}
                      </td>
                      <td>{r.error ? "–" : fmt(r.subscribers)}</td>
                      <td>{r.error ? "–" : fmt(r.channel_views)}</td>
                      <td>{r.error ? "–" : fmt(r.video_count)}</td>
                      <td>{r.error ? "–" : fmt(r.clipforge_videos)}</td>
                      <td>{r.error ? "–" : fmt(r.clipforge_views)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      {data.data && selected && choice !== ALL && (
        <section className="section" aria-labelledby="one-h">
          <h2 id="one-h">{selected.name}</h2>
          {selected.error ? (
            <Notice tone="fail">{selected.error}</Notice>
          ) : (
            <Cards s={selected} hidden={selected.subscribers === null} />
          )}
        </section>
      )}
    </div>
  );
}
