import { useRef, useState } from "react";
import { ApiError, getCsrf } from "../api/client";
import type { Candidate } from "../api/types";
import { t } from "../i18n/sv";
import { useToast } from "./ui";

/** Streams the file as the raw request body with real upload progress (XHR). */
export function uploadFile(
  file: File,
  params: Record<string, string>,
  onProgress: (pct: number) => void,
): Promise<{ candidate: Candidate; duplicate: boolean }> {
  return new Promise((resolve, reject) => {
    const qs = new URLSearchParams({ filename: file.name, ...params });
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/inbox/upload?${qs}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const csrf = getCsrf();
    if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress((100 * e.loaded) / e.total);
    xhr.onload = () => {
      let body: { error?: { code: string; message: string } } & Record<string, unknown> = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as never);
      else reject(new ApiError(xhr.status, body.error?.code ?? "error", body.error?.message ?? t.errors.generic));
    };
    xhr.onerror = () => reject(new ApiError(0, "offline", t.errors.offline));
    xhr.send(file);
  });
}

export function UploadButton({
  params,
  label,
  onDone,
}: {
  params: Record<string, string>;
  label: string;
  onDone: (r: { candidate: Candidate; duplicate: boolean }) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [pct, setPct] = useState<number | null>(null);
  const toast = useToast();
  return (
    <span className="row">
      <input
        ref={input}
        type="file"
        accept="video/*,.mkv"
        className="sr-only"
        aria-label={label}
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setPct(0);
          try {
            onDone(await uploadFile(f, params, setPct));
          } catch (err) {
            toast((err as Error).message, "fail");
          } finally {
            setPct(null);
            if (input.current) input.current.value = "";
          }
        }}
      />
      <button type="button" onClick={() => input.current?.click()} disabled={pct !== null}>
        {pct === null ? label : `${t.inbox.uploading} ${Math.round(pct)} %`}
      </button>
    </span>
  );
}
