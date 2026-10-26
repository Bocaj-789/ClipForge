import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import { Notice, useToast } from "../components/ui";
import { t } from "../i18n/sv";

type Val = string | number | boolean | string[] | null;
type Settings = Record<string, Val>;
interface Resp {
  settings: Settings;
  transcription_models: Record<
    string,
    { size: string; ram: string; note: string }
  >;
  confirm_required: string[];
}
type Field = {
  key: string;
  label: string;
  kind: "bool" | "int" | "float" | "text" | "select" | "list";
  options?: [string, string][];
  help?: string;
};

const G = t.settings.groups;
const GROUPS: [string, Field[]][] = [
  [
    G.processing,
    [
      { key: "discovery_enabled", label: "Automatisk upptäckt", kind: "bool" },
      {
        key: "max_discoveries_per_day",
        label: "Max kontroller av källor per dygn",
        kind: "int",
      },
      {
        key: "max_renders_per_day",
        label: "Max renderingar per dag",
        kind: "int",
      },
      {
        key: "auto_render_top_n",
        label: "Rendera automatiskt de bästa N förslagen per video",
        kind: "int",
      },
      { key: "max_input_file_mb", label: "Största videofil (MB)", kind: "int" },
      {
        key: "max_input_duration_minutes",
        label: "Längsta video (minuter)",
        kind: "int",
      },
      {
        key: "youtube_media_provider",
        label: "Hämta YouTube-video automatiskt",
        kind: "select",
        options: [
          ["yt_dlp", "Ja, när rättigheterna är bekräftade (yt-dlp)"],
          ["off", "Nej, bara metadata – jag bifogar filen"],
        ],
        help: "ClipForge kringgår aldrig inloggning, DRM eller andra begränsningar. Kan YouTube inte lämna ut videon väntar jobbet på mediaåtkomst.",
      },
      {
        key: "heavy_concurrency",
        label: "Tunga jobb samtidigt",
        kind: "int",
        help: "1 rekommenderas på en M1 med 16 GB. Kräver omstart av arbetaren.",
      },
      {
        key: "download_concurrency",
        label: "Nedladdningar samtidigt",
        kind: "int",
        help: "Hur många videor som hämtas parallellt (1 till 5). Kräver omstart av arbetaren.",
      },
      {
        key: "max_concurrent_jobs",
        label: "Jobb samtidigt totalt",
        kind: "int",
        help: "Övre gräns för nedladdningar och tunga jobb tillsammans (1 till 9). Gäller över båda inställningarna ovan. Kräver omstart av arbetaren.",
      },
      {
        key: "low_resource_mode",
        label: "Lågresursläge",
        kind: "bool",
        help: "Färre trådar och snabbare men mindre noggrann transkribering.",
      },
      {
        key: "allowed_direct_media_hosts",
        label: "Tillåtna mediavärdar (kommaseparerade)",
        kind: "list",
        help: "Filer hämtas bara härifrån och bara med bekräftade rättigheter.",
      },
    ],
  ],
  [
    G.clips,
    [
      { key: "min_clip_seconds", label: "Kortaste klipp (s)", kind: "int" },
      { key: "max_clip_seconds", label: "Längsta klipp (s)", kind: "int" },
      {
        key: "default_render_profile",
        label: "Renderingsprofil",
        kind: "select",
        options: [
          ["fast_draft", "Snabbt utkast (720×1280)"],
          ["shorts_standard", "Standard (1080×1920)"],
          ["high_quality", "Hög kvalitet (långsam)"],
        ],
      },
      {
        key: "default_caption_style",
        label: "Undertextstil",
        kind: "select",
        options: [
          ["bold", "Fet"],
          ["clean", "Ren"],
          ["karaoke", "Karaoke"],
        ],
      },
      {
        key: "default_crop_mode",
        label: "Beskärning",
        kind: "select",
        options: [
          ["center_crop", "Beskär till 9:16"],
          ["fit_blur", "Hela bilden på suddig bakgrund"],
        ],
      },
      {
        key: "loudness_normalization",
        label: "Normalisera ljudnivå",
        kind: "bool",
      },
      {
        key: "caption_font_path",
        label: "Typsnittsfil för undertexter (valfri)",
        kind: "text",
      },
    ],
  ],
  [
    G.transcription,
    [
      { key: "transcription_model", label: "Whisper-modell", kind: "select" },
      {
        key: "transcription_language",
        label: "Språk",
        kind: "select",
        options: [
          ["auto", "Identifiera automatiskt"],
          ["sv", "Svenska"],
          ["en", "Engelska"],
        ],
      },
      {
        key: "transcription_download_allowed",
        label: "Tillåt nedladdning av vald modell",
        kind: "bool",
        help: "Modellen laddas ned från Hugging Face till data/models första gången den behövs.",
      },
    ],
  ],
  [
    G.ai,
    [
      {
        key: "llm_provider",
        label: "Klippbedömning",
        kind: "select",
        options: [
          ["heuristic", "Regelbaserad heuristik"],
          ["ollama", "Ollama (lokal modell)"],
        ],
      },
      {
        key: "ollama_model",
        label: "Ollama-modell",
        kind: "text",
        help: "Måste redan vara installerad med ollama pull. Inget laddas ned automatiskt.",
      },
      {
        key: "learning_enabled",
        label: "Lär av statistik (när den finns)",
        kind: "bool",
      },
    ],
  ],
  [
    G.storage,
    [
      { key: "storage_budget_gb", label: "Lagringsbudget (GB)", kind: "float" },
      { key: "storage_warn_percent", label: "Varna vid (%)", kind: "int" },
      {
        key: "storage_critical_percent",
        label: "Pausa hämtning/rendering vid (%)",
        kind: "int",
      },
      {
        key: "min_free_disk_gb",
        label: "Minsta lediga diskutrymme (GB)",
        kind: "float",
      },
      {
        key: "source_media_retention_hours",
        label: "Ta bort nedladdade videor efter (timmar)",
        kind: "int",
      },
      { key: "timezone", label: "Tidszon", kind: "text" },
    ],
  ],
  [
    G.publishing,
    [
      {
        key: "auto_publish_global_enabled",
        label: "Publicera färdiga klipp automatiskt (huvudbrytare)",
        kind: "bool",
        help: "Varje färdigt klipp laddas upp till alla anslutna kanaler som är med i automatisk publicering. Privat om du inte tillåtit offentlig publicering för kanalen.",
      },
      {
        key: "max_uploads_per_day_global",
        label: "Max automatiska uppladdningar per dag, alla kanaler",
        kind: "int",
        help: "Räknas per kanal och klipp: 3 kanaler × 4 klipp = 12.",
      },
      {
        key: "youtube_daily_insert_limit",
        label: "Lokal gräns för YouTube-uppladdningar per dygn (API-kvot)",
        kind: "int",
        help: "Googles standard är 100 per dygn och projekt. Nollställs vid midnatt Pacific Time.",
      },
      {
        key: "youtube_daily_unit_limit",
        label: "Lokal gräns för övriga YouTube-anrop (enheter per dygn)",
        kind: "int",
        help: "Googles standard är 10 000 enheter.",
      },
      {
        key: "youtube_daily_search_limit",
        label: "Lokal gräns för sökningar (API-kvot)",
        kind: "int",
        help: "Googles standard är 100 sökningar per dygn. Används av Creative Commons-källor.",
      },
    ],
  ],
  [
    G.remote,
    [
      {
        key: "remote_access_enabled",
        label: "Fjärråtkomst planerad",
        kind: "bool",
        help: "Servern lyssnar alltid bara på 127.0.0.1. Fjärråtkomst kräver en separat, säkrad tunnel (se docs/SECURITY.md).",
      },
    ],
  ],
];

export default function Settings() {
  const toast = useToast();
  const [resp, setResp] = useState<Resp | null>(null);
  const [draft, setDraft] = useState<Settings>({});
  const [error, setError] = useState<string | null>(null);
  const [phrase, setPhrase] = useState("");
  const dlg = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    api<Resp>("/settings")
      .then((r) => {
        setResp(r);
        setDraft(r.settings);
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);
  if (!resp)
    return (
      <div className="page">
        {error ? <Notice tone="fail">{error}</Notice> : t.common.loading}
      </div>
    );

  const changes = Object.fromEntries(
    Object.entries(draft).filter(
      ([k, v]) => JSON.stringify(v) !== JSON.stringify(resp.settings[k]),
    ),
  );
  const risky = resp.confirm_required.filter((k) => changes[k] === true);

  async function save(confirm?: string) {
    setError(null);
    try {
      const r = await api<Resp>("/settings", {
        method: "PATCH",
        body: { changes, confirm },
      });
      setResp(r);
      setDraft(r.settings);
      toast(t.settings.saved);
      dlg.current?.close();
    } catch (e) {
      setError(errorMessage(e));
      dlg.current?.close();
    }
  }

  const set = (k: string, v: Val) => setDraft((d) => ({ ...d, [k]: v }));
  const control = (f: Field) => {
    const v = draft[f.key];
    const id = `s-${f.key}`;
    if (f.kind === "bool")
      return (
        <label className="check" htmlFor={id}>
          <input
            id={id}
            type="checkbox"
            checked={Boolean(v)}
            onChange={(e) => set(f.key, e.target.checked)}
          />
          {f.label}
        </label>
      );
    const options =
      f.key === "transcription_model"
        ? Object.entries(resp.transcription_models).map(
            ([k, m]) =>
              [k, `${k} (${m.size}, ${m.ram} RAM) ${m.note}`] as [
                string,
                string,
              ],
          )
        : f.options;
    return (
      <label htmlFor={id}>
        {f.label}
        {f.kind === "select" ? (
          <select
            id={id}
            value={String(v)}
            onChange={(e) => set(f.key, e.target.value)}
          >
            {options?.map(([k, l]) => (
              <option key={k} value={k}>
                {l}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={id}
            type={f.kind === "int" || f.kind === "float" ? "number" : "text"}
            step={f.kind === "float" ? "0.5" : undefined}
            value={
              f.kind === "list"
                ? ((v as string[]) ?? []).join(", ")
                : String(v ?? "")
            }
            onChange={(e) =>
              set(
                f.key,
                f.kind === "int"
                  ? parseInt(e.target.value || "0", 10)
                  : f.kind === "float"
                    ? parseFloat(e.target.value || "0")
                    : f.kind === "list"
                      ? e.target.value
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean)
                      : e.target.value ||
                        (f.key === "caption_font_path" ? null : ""),
              )
            }
          />
        )}
      </label>
    );
  };

  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.settings.title}</h1>
      </header>
      {error && <Notice tone="fail">{error}</Notice>}
      <form
        className="stack"
        style={{ gap: "1.5rem" }}
        onSubmit={(e) => {
          e.preventDefault();
          if (risky.length) dlg.current?.showModal();
          else save();
        }}
      >
        {GROUPS.map(([title, fields]) => (
          <fieldset key={title} className="panel" style={{ margin: 0 }}>
            <legend className="sr-only">{title}</legend>
            <h2 aria-hidden="true">{title}</h2>
            <div className="form-grid">
              {fields.map((f) => (
                <div key={f.key} className="stack" style={{ gap: "0.3rem" }}>
                  {control(f)}
                  {f.help && <span className="help">{f.help}</span>}
                </div>
              ))}
            </div>
          </fieldset>
        ))}
        <div
          className="row"
          style={{
            position: "sticky",
            bottom: 0,
            background: "var(--ground)",
            padding: "0.75rem 0",
          }}
        >
          <button
            className="primary"
            type="submit"
            disabled={Object.keys(changes).length === 0}
          >
            {t.common.save}{" "}
            {Object.keys(changes).length
              ? `(${Object.keys(changes).length})`
              : ""}
          </button>
          <button
            type="button"
            onClick={() => setDraft(resp.settings)}
            disabled={Object.keys(changes).length === 0}
          >
            Ångra ändringar
          </button>
          <button
            type="button"
            className="ghost"
            onClick={async () => {
              try {
                const r = await api<{ name: string }>("/settings/backup", {
                  method: "POST",
                });
                toast(`${t.settings.backupDone}: data/backups/${r.name}`);
              } catch (e) {
                toast(errorMessage(e), "fail");
              }
            }}
          >
            {t.settings.backup}
          </button>
        </div>
      </form>
      <dialog ref={dlg} aria-labelledby="risky-h">
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            save(phrase);
          }}
        >
          <h2 id="risky-h">{t.common.confirm}</h2>
          <p>{t.settings.confirmRisky}</p>
          <ul>
            {risky.map((k) => (
              <li key={k}>
                {GROUPS.flatMap(([, f]) => f).find((f) => f.key === k)?.label ??
                  k}
              </li>
            ))}
          </ul>
          <label>
            Skriv JA
            <input
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              autoFocus
            />
          </label>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" onClick={() => dlg.current?.close()}>
              {t.common.cancel}
            </button>
            <button
              type="submit"
              className="danger solid"
              disabled={phrase.trim().toUpperCase() !== "JA"}
            >
              {t.common.save}
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
