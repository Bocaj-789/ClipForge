import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrf } from "../api/client";
import App from "../App";
import { fmtBytes, fmtDuration } from "../lib/format";

function mockFetch(routes: Record<string, unknown | ((init?: RequestInit) => Response)>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace("/api/v1", "").split("?")[0]!;
    const r = routes[url];
    if (typeof r === "function") return (r as (i?: RequestInit) => Response)(init);
    if (r === undefined)
      return new Response(JSON.stringify({ error: { code: "not_found", message: "x" } }), { status: 404 });
    return new Response(JSON.stringify(r), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf(null);
});

describe("api client", () => {
  it("sends CSRF header on mutations only", async () => {
    const f = mockFetch({ "/x": { ok: 1 } });
    setCsrf("tok");
    await api("/x");
    await api("/x", { method: "POST" });
    const headers = (i: number) => (f.mock.calls[i]![1] as RequestInit).headers as Record<string, string>;
    expect(headers(0)["X-CSRF-Token"]).toBeUndefined();
    expect(headers(1)["X-CSRF-Token"]).toBe("tok");
  });

  it("maps server errors to safe messages", async () => {
    mockFetch({
      "/bad": () =>
        new Response(JSON.stringify({ error: { code: "unsupported_url", message: "Endast https." } }), { status: 400 }),
    });
    await expect(api("/bad")).rejects.toMatchObject({ status: 400, code: "unsupported_url", message: "Endast https." });
  });

  it("reports offline when fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const err = await api("/x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("offline");
  });
});

describe("format", () => {
  it("formats bytes and durations", () => {
    expect(fmtBytes(1536)).toBe("1,5 kB");
    expect(fmtDuration(75)).toBe("1:15");
    expect(fmtDuration(3725)).toBe("1:02:05");
  });
});

describe("App", () => {
  it("shows account setup on first run", async () => {
    mockFetch({ "/auth/me": { authenticated: false, setup_required: true } });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Skapa ditt konto" })).toBeInTheDocument();
    expect(screen.getByLabelText("Installationskod")).toBeInTheDocument();
  });

  it("shows login errors from the server", async () => {
    mockFetch({
      "/auth/me": { authenticated: false, setup_required: false },
      "/auth/login": () =>
        new Response(JSON.stringify({ error: { code: "unauthorized", message: "Fel användarnamn eller lösenord." } }), {
          status: 401,
        }),
    });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("Användarnamn"), "a");
    await userEvent.type(screen.getByLabelText("Lösenord"), "b");
    await userEvent.click(screen.getByRole("button", { name: "Logga in" }));
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      "Fel användarnamn eller lösenord.",
    );
  });

  it("renders the agent state from real summary data", async () => {
    const summary = {
      agent: {
        state: "paused",
        desired_state: "paused",
        reason: "Pausad av användaren.",
        worker_alive: true,
        running_jobs: 0,
        degraded: {},
      },
      jobs: {},
      clips_awaiting_review: 2,
      rendered_last_24h: 1,
      candidates_total: 3,
      recent_errors: [],
      storage: {
        used_bytes: 1,
        budget_bytes: 10,
        used_percent: 10,
        disk_free_bytes: 100,
        level: "ok",
        reason: null,
        breakdown: {},
      },
      transcription: { available: true, reason: null, model: "base", downloaded: true },
      llm_provider: "heuristic",
      sources: [],
      channels: 0,
      recent_publications: [],
      auto_publish_global_enabled: false,
      youtube_api_key_configured: false,
    };
    mockFetch({
      "/auth/me": { authenticated: true, setup_required: false, username: "agaren", csrf_token: "c" },
      "/dashboard/summary": summary,
      "/jobs": { items: [], total: 0, counts: {} },
      "/settings": { settings: { timezone: "Europe/Stockholm" } },
    });
    render(<App />);
    await waitFor(() => expect(document.getElementById("agent-state")).toHaveTextContent("Pausad"));
    expect(screen.getByRole("button", { name: "Återuppta" })).toBeInTheDocument();
    expect(screen.getByText("Automatisk publicering: av")).toBeInTheDocument();
    expect(screen.getByLabelText("2 att granska")).toBeInTheDocument();
  });
});
