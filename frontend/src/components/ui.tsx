import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { t } from "../i18n/sv";

// ------------------------------------------------------------- status pills
const JOB_TONE: Record<string, string> = {
  pending: "wait",
  retry_wait: "wait",
  running: "work",
  succeeded: "done",
  failed: "fail",
  blocked: "fail",
  cancelled: "muted",
};
const CAND_TONE: Record<string, string> = {
  discovered: "wait",
  queued: "wait",
  analyzing: "work",
  ready: "done",
  skipped: "muted",
  blocked: "fail",
  failed: "fail",
};
const REVIEW_TONE: Record<string, string> = { needs_review: "review", approved: "done", rejected: "muted" };

export function Pill({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <span className="pill" data-tone={tone}>
      {children}
    </span>
  );
}
export const JobPill = ({ status }: { status: string }) => (
  <Pill tone={JOB_TONE[status] ?? "muted"}>{t.jobStatus[status] ?? status}</Pill>
);
export const CandPill = ({ status }: { status: string }) => (
  <Pill tone={CAND_TONE[status] ?? "muted"}>{t.candStatus[status] ?? status}</Pill>
);
export const ReviewPill = ({ status }: { status: string }) => (
  <Pill tone={REVIEW_TONE[status] ?? "muted"}>{t.review[status] ?? status}</Pill>
);
export const RightsPill = ({ status }: { status: string }) => (
  <Pill tone={status === "unknown" || status === "not_permitted" ? "fail" : "done"}>{t.rights[status] ?? status}</Pill>
);

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Notice({ tone = "warn", children }: { tone?: "warn" | "fail" | "info"; children: ReactNode }) {
  return (
    <div className="notice" data-tone={tone} role={tone === "fail" ? "alert" : undefined}>
      {children}
    </div>
  );
}

export function Progress({ value }: { value: number }) {
  return (
    <div
      className="progress"
      role="progressbar"
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <span style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
    </div>
  );
}

// ------------------------------------------------------------- toasts
type Toast = { id: number; text: string; tone: "info" | "fail" };
const ToastCtx = createContext<(text: string, tone?: "info" | "fail") => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: "info" | "fail" = "info") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, text, tone }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "fail" ? 8000 : 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((x) => (
          <div key={x.id} className="toast" data-tone={x.tone}>
            {x.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ------------------------------------------------------------- confirm dialog
export function ConfirmButton({
  children,
  message,
  onConfirm,
  className,
  confirmLabel,
  requirePhrase,
  disabled,
}: {
  children: ReactNode;
  message: string;
  onConfirm: (phrase?: string) => void | Promise<void>;
  className?: string;
  confirmLabel?: string;
  requirePhrase?: string;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const d = ref.current;
    const onClose = () => setPhrase("");
    d.addEventListener("close", onClose);
    return () => d.removeEventListener("close", onClose);
  }, []);
  const ok = !requirePhrase || phrase.trim().toUpperCase() === requirePhrase;
  return (
    <>
      <button type="button" className={className} onClick={() => ref.current?.showModal()} disabled={disabled}>
        {children}
      </button>
      <dialog ref={ref} aria-labelledby="confirm-title">
        <form
          method="dialog"
          className="stack"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!ok) return;
            setBusy(true);
            try {
              await onConfirm(requirePhrase ? phrase : undefined);
              ref.current?.close();
            } finally {
              setBusy(false);
            }
          }}
        >
          <h2 id="confirm-title">{t.common.confirm}</h2>
          <p>{message}</p>
          {requirePhrase && (
            <label>
              Skriv {requirePhrase}
              <input value={phrase} onChange={(e) => setPhrase(e.target.value)} autoFocus />
            </label>
          )}
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" onClick={() => ref.current?.close()}>
              {t.common.cancel}
            </button>
            <button type="submit" className="danger solid" disabled={!ok || busy}>
              {confirmLabel ?? t.common.confirm}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}
