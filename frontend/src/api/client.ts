import { t } from "../i18n/sv";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let csrfToken: string | null = null;
export const setCsrf = (token: string | null) => {
  csrfToken = token;
};
export const getCsrf = () => csrfToken;

type Listener = (online: boolean) => void;
const listeners = new Set<Listener>();
let online = true;
export const onConnectionChange = (fn: Listener) => {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
};
const setOnline = (v: boolean) => {
  if (v !== online) {
    online = v;
    listeners.forEach((l) => l(v));
  }
};

let onUnauthorized: (() => void) | null = null;
export const setUnauthorizedHandler = (fn: () => void) => {
  onUnauthorized = fn;
};

export async function api<T>(
  path: string,
  opts: {
    method?: string;
    body?: unknown;
    raw?: BodyInit;
    headers?: Record<string, string>;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const method = opts.method ?? (opts.body !== undefined || opts.raw ? "POST" : "GET");
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      headers,
      body: opts.raw ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    setOnline(false);
    throw new ApiError(0, "offline", t.errors.offline);
  }
  setOnline(true);
  if (res.status === 204) return undefined as T;
  let data: unknown;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = (data as { error?: { code?: string; message?: string } } | null)?.error;
    if (res.status === 401 && onUnauthorized && !path.startsWith("/auth/")) onUnauthorized();
    const msg =
      err?.message ??
      (res.status === 429 ? t.errors.rateLimited : res.status >= 500 ? t.errors.generic : t.errors.generic);
    throw new ApiError(res.status, err?.code ?? "error", msg);
  }
  return data as T;
}

export const errorMessage = (e: unknown) =>
  e instanceof ApiError || e instanceof Error ? e.message : t.errors.generic;
