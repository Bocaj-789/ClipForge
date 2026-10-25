import type { Proposal } from "../api/types";

const PARTS: [string, string, number][] = [
  ["hook", "Krok", 25],
  ["coherence", "Sammanhang", 25],
  ["interest", "Intresse", 20],
  ["energy", "Energi", 10],
  ["clarity", "Tydlighet", 10],
  ["channel_fit", "Kanalmatch", 10],
];

const PENALTY: Record<string, string> = {
  missing_context: "Saknar sammanhang",
  little_speech: "Lite tal",
  low_audio_quality: "Otydligt ljud",
};

export function ScoreBreakdown({ p }: { p: Proposal }) {
  return (
    <div className="stack" style={{ gap: "0.5rem" }}>
      <div className="score-bars">
        {PARTS.map(([k, label, max]) => {
          const v = p.scores[k] ?? 0;
          return (
            <div key={k} style={{ display: "contents" }}>
              <span className="muted">{label}</span>
              <div className="bar" aria-hidden="true">
                <span style={{ width: `${(100 * v) / max}%` }} />
              </div>
              <span className="num">
                {v}/{max}
              </span>
            </div>
          );
        })}
      </div>
      {Object.entries(p.penalties).map(([k, v]) => (
        <span key={k} className="small" style={{ color: "var(--fail)" }}>
          −{v} {PENALTY[k] ?? k}
        </span>
      ))}
      <p className="small muted">
        {p.score_explanation} Bedömt av: {p.provider === "heuristic" ? "regelbaserad heuristik" : p.provider}.
      </p>
      {p.warnings.map((w) => (
        <span key={w} className="small" style={{ color: "var(--straw)" }}>
          {w}
        </span>
      ))}
    </div>
  );
}
