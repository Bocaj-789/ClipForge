import { useState, type FormEvent } from "react";
import { api, errorMessage } from "../api/client";
import type { Channel } from "../api/types";
import { ConfirmButton, Empty, Notice, Pill, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtTime } from "../lib/format";

const DAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"];

function ChannelForm({ initial, onSaved }: { initial?: Channel; onSaved: () => void }) {
  const [name, setName] = useState(initial?.display_name ?? "");
  const [perDay, setPerDay] = useState(initial?.max_uploads_per_day ?? 1);
  const [spacing, setSpacing] = useState(initial?.min_spacing_minutes ?? 240);
  const [visibility, setVisibility] = useState(initial?.default_visibility ?? "private");
  const [days, setDays] = useState<number[]>(initial?.schedule.days ?? [0, 1, 2, 3, 4, 5, 6]);
  const [from, setFrom] = useState(initial?.schedule.windows?.[0]?.[0] ?? "09:00");
  const [to, setTo] = useState(initial?.schedule.windows?.[0]?.[1] ?? "21:00");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const body = {
      display_name: name,
      max_uploads_per_day: perDay,
      min_spacing_minutes: spacing,
      default_visibility: visibility,
      schedule: { days, windows: [[from, to]] },
    };
    try {
      if (initial) await api(`/channels/${initial.id}/settings`, { method: "PATCH", body });
      else await api("/channels", { body });
      if (!initial) setName("");
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  return (
    <form className="stack" onSubmit={submit}>
      {error && <Notice tone="fail">{error}</Notice>}
      <div className="form-grid">
        <label>
          {t.channels.name}
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
        </label>
        <label>
          {t.channels.perDay}
          <input type="number" min={0} max={20} value={perDay} onChange={(e) => setPerDay(Number(e.target.value))} />
        </label>
        <label>
          {t.channels.spacing}
          <input
            type="number"
            min={10}
            max={1440}
            value={spacing}
            onChange={(e) => setSpacing(Number(e.target.value))}
          />
        </label>
        <label>
          {t.channels.visibility}
          <select value={visibility} onChange={(e) => setVisibility(e.target.value)}>
            <option value="private">Privat</option>
            <option value="unlisted">Olistad</option>
            <option value="public">Offentlig</option>
          </select>
        </label>
      </div>
      <fieldset className="row" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="small muted" style={{ marginBottom: "0.4rem" }}>
          {t.channels.windows}
        </legend>
        {DAYS.map((d, i) => (
          <label key={d} className="check small">
            <input
              type="checkbox"
              checked={days.includes(i)}
              onChange={(e) => setDays(e.target.checked ? [...days, i].sort() : days.filter((x) => x !== i))}
            />
            {d}
          </label>
        ))}
        <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
          från <input type="time" value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className="row" style={{ flexDirection: "row", alignItems: "center" }}>
          till <input type="time" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
      </fieldset>
      <div>
        <button className={initial ? "" : "primary"} type="submit">
          {initial ? t.common.save : t.channels.add}
        </button>
      </div>
    </form>
  );
}

export default function Channels() {
  const toast = useToast();
  const q = usePoll<{ items: Channel[]; oauth_note: string }>((s) => api("/channels", { signal: s }), 15000);
  const [editing, setEditing] = useState<string | null>(null);
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.channels.title}</h1>
        <p className="muted">{t.channels.lead}</p>
      </header>
      <Notice tone="info">{t.channels.oauthPlanned}</Notice>
      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && q.data.items.length === 0 ? (
        <Empty>{t.channels.empty}</Empty>
      ) : (
        <ul className="list">
          {q.data?.items.map((c) => (
            <li key={c.id} style={{ flexDirection: "column", alignItems: "stretch" }}>
              <div className="spread">
                <div className="stack" style={{ gap: "0.3rem" }}>
                  <span className="title-line">{c.display_name}</span>
                  <div className="row small muted">
                    <Pill tone={c.oauth_connected ? "done" : "muted"}>
                      {c.oauth_connected ? "Ansluten" : t.channels.oauthMissing}
                    </Pill>
                    <Pill tone={c.auto_publish_enabled ? "work" : "muted"}>
                      {c.auto_publish_enabled ? "Autopublicering på" : "Granskning krävs"}
                    </Pill>
                    <span>max {c.max_uploads_per_day}/dag</span>
                    <span>{c.timezone}</span>
                    <span>
                      {t.channels.nextSlot}: {fmtTime(c.next_slot)}
                    </span>
                  </div>
                </div>
                <div className="row">
                  <button
                    className="small"
                    onClick={() => setEditing(editing === c.id ? null : c.id)}
                    aria-expanded={editing === c.id}
                  >
                    {editing === c.id ? t.common.close : "Redigera"}
                  </button>
                  <ConfirmButton
                    className="small ghost"
                    message="Ta bort kanalkonfigurationen?"
                    onConfirm={async () => {
                      try {
                        await api(`/channels/${c.id}`, { method: "DELETE" });
                        q.reload();
                      } catch (e) {
                        toast(errorMessage(e), "fail");
                      }
                    }}
                  >
                    {t.common.delete}
                  </ConfirmButton>
                </div>
              </div>
              {editing === c.id && (
                <ChannelForm
                  initial={c}
                  onSaved={() => {
                    toast("Kanalen sparades.");
                    q.reload();
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      <section className="panel">
        <h2>{t.channels.add}</h2>
        <ChannelForm onSaved={q.reload} />
      </section>
    </div>
  );
}
