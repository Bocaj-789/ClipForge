import { useState, type FormEvent } from "react";
import { api, errorMessage } from "../api/client";
import type { Candidate, Paged, Source } from "../api/types";
import { ConfirmButton, Empty, Notice, Pill, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtRelative, fmtTime } from "../lib/format";
import { CandidateRow } from "./Inbox";

function AddSource({ onDone }: { onDone: () => void }) {
  const [kind, setKind] = useState("rss");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [interval, setIntervalMin] = useState(180);
  const [keywords, setKeywords] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api("/sources", {
        body: {
          kind,
          name,
          url: kind === "local_folder" ? null : url,
          polling_interval_minutes: interval,
          keywords: keywords
            .split(",")
            .map((k) => k.trim())
            .filter(Boolean),
          category: category || null,
        },
      });
      setName("");
      setUrl("");
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  return (
    <form className="panel" onSubmit={submit}>
      <h2>{t.discovery.add}</h2>
      {error && <Notice tone="fail">{error}</Notice>}
      <div className="form-grid">
        <label>
          {t.discovery.kind}
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {Object.entries(t.discovery.kinds).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t.discovery.name}
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={200}
          />
        </label>
        {kind !== "local_folder" && kind !== "youtube_cc_search" && (
          <label>
            {t.discovery.url}
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              placeholder={
                kind === "rss"
                  ? "https://www.youtube.com/channel/UC…"
                  : "https://www.youtube.com/@kanal"
              }
            />
          </label>
        )}
        <label>
          {t.discovery.interval}
          <input
            type="number"
            min={15}
            max={10080}
            value={interval}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
          />
        </label>
        <label>
          {t.discovery.keywords}
          <input
            value={keywords}
            onChange={(e) => setKeywords(e.target.value)}
          />
        </label>
        <label>
          {t.discovery.category}
          <input
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            maxLength={100}
          />
        </label>
      </div>
      <p className="help">
        {kind === "local_folder"
          ? t.discovery.folderHelp
          : kind === "youtube_cc_search"
            ? "Skriv ämnen i Nyckelord, separerade med komma (t.ex. ekonomi, historia, intervju). ClipForge söker efter videor med YouTubes Creative Commons-licens och godkänner bara de som licensen verifieras för. Kräver CLIPFORGE_YOUTUBE_API_KEY."
            : kind === "rss"
              ? "YouTube-kanaler har ett öppet flöde utan API-nyckel. Ange kanaladressen med kanal-ID (UC…)."
              : "Använder YouTube Data API inom gratiskvoten (cirka 2 kvotenheter per kontroll). Kräver CLIPFORGE_YOUTUBE_API_KEY."}
      </p>
      <div>
        <button className="primary" type="submit">
          {t.discovery.add}
        </button>
      </div>
    </form>
  );
}

export default function Discovery() {
  const toast = useToast();
  const sources = usePoll<{ items: Source[] }>(
    (s) => api("/sources", { signal: s }),
    10000,
  );
  const found = usePoll<Paged<Candidate>>(
    (s) => api("/candidates?origin=discovery&limit=50", { signal: s }),
    10000,
  );
  const act = async (path: string, method = "POST") => {
    try {
      await api(path, { method });
      sources.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  };
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.discovery.title}</h1>
        <p className="muted">{t.discovery.lead}</p>
      </header>
      {sources.error && <Notice tone="fail">{sources.error}</Notice>}
      <section className="section" aria-labelledby="src-h">
        <h2 id="src-h">Källor</h2>
        {sources.data && sources.data.items.length === 0 ? (
          <Empty>{t.discovery.empty}</Empty>
        ) : (
          <ul className="list">
            {sources.data?.items.map((s) => (
              <li key={s.id}>
                <div className="grow stack" style={{ gap: "0.3rem" }}>
                  <span className="title-line">{s.name}</span>
                  <div className="row small muted">
                    <span>{t.discovery.kinds[s.kind]}</span>
                    {s.paused && <Pill tone="wait">{t.agent.paused}</Pill>}
                    <span>var {s.polling_interval_minutes} min</span>
                    <span>{s.candidates} videor</span>
                    <span title={fmtTime(s.last_checked_at)}>
                      {t.discovery.lastCheck}{" "}
                      {s.last_checked_at
                        ? fmtRelative(s.last_checked_at)
                        : t.common.never}
                    </span>
                  </div>
                  {s.last_error && (
                    <span className="small" style={{ color: "var(--fail)" }}>
                      {s.last_error}
                    </span>
                  )}
                </div>
                <div className="row">
                  <button
                    className="small"
                    onClick={() => act(`/sources/${s.id}/check`)}
                  >
                    {t.discovery.checkNow}
                  </button>
                  <button
                    className="small"
                    onClick={() =>
                      act(`/sources/${s.id}/${s.paused ? "resume" : "pause"}`)
                    }
                  >
                    {s.paused ? t.agent.resume : t.agent.pause}
                  </button>
                  <ConfirmButton
                    className="small ghost"
                    message="Ta bort källan? Redan upptäckta videor finns kvar."
                    onConfirm={() => act(`/sources/${s.id}`, "DELETE")}
                  >
                    {t.common.delete}
                  </ConfirmButton>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <AddSource onDone={sources.reload} />
      <section className="section" aria-labelledby="found-h">
        <div className="spread">
          <h2 id="found-h">Senast upptäckta</h2>
          <ConfirmButton
            className="small ghost"
            message="Ta bort de upptäckta videor som inte har börjat analyseras? Köade jobb för dem avbryts. Redan analyserade videor finns kvar."
            onConfirm={async () => {
              try {
                const r = await api<{ removed: number; kept: number }>("/candidates/clear-discovered", {
                  method: "POST",
                });
                toast(`${r.removed} borttagna, ${r.kept} behållna.`);
                found.reload();
                sources.reload();
              } catch (e) {
                toast(errorMessage(e), "fail");
              }
            }}
          >
            Rensa senast upptäckta
          </ConfirmButton>
        </div>
        {found.data && found.data.items.length === 0 ? (
          <Empty>Inget upptäckt ännu.</Empty>
        ) : (
          <ul className="list">
            {found.data?.items.map((c) => (
              <CandidateRow key={c.id} c={c} onChange={found.reload} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
