import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setCsrf } from "../api/client";
import type { Channel, Publication } from "../api/types";
import { ToastProvider } from "../components/ui";
import * as nav from "../lib/nav";
import Channels from "../pages/Channels";
import Publications from "../pages/Publications";

vi.mock("../lib/nav", () => ({ goTo: vi.fn() }));

type Handler = unknown | ((init?: RequestInit) => unknown);
function mockFetch(routes: Record<string, Handler>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace("/api/v1", "").split("?")[0]!;
    const r = routes[url];
    if (r === undefined)
      return new Response(
        JSON.stringify({ error: { code: "not_found", message: "x" } }),
        { status: 404 },
      );
    const body =
      typeof r === "function" ? (r as (i?: RequestInit) => unknown)(init) : r;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  setCsrf(null);
});

const channel = (over: Partial<Channel> = {}): Channel => ({
  id: "c1",
  display_name: "Min kanal",
  enabled: true,
  paused: false,
  auto_publish_enabled: true,
  max_uploads_per_day: 10,
  min_spacing_minutes: 10,
  timezone: "Europe/Stockholm",
  schedule: {},
  default_visibility: "private",
  render_profile: "shorts_standard",
  caption_style: "bold",
  oauth_connected: true,
  connection_state: "connected",
  connection_error: null,
  external_channel_id: "UC1",
  youtube_title: "Min kanal",
  youtube_handle: "@min",
  youtube_url: "https://www.youtube.com/channel/UC1",
  allow_public: false,
  category_id: "22",
  made_for_kids: false,
  notify_subscribers: false,
  auto_publish_min_score: 0,
  next_slot: null,
  last_upload_at: null,
  ...over,
});
const oauth = (over = {}) => ({
  client_configured: true,
  store: "keychain",
  store_available: true,
  store_reason: null,
  redirect_uri: "http://127.0.0.1:8000/api/v1/oauth/youtube/callback",
  ready: true,
  ...over,
});
const status = (auto = false) => ({
  client_configured: true,
  redirect_uri: "x",
  token_store: { backend: "keychain", available: true, reason: null },
  quota: {
    resets_at: "2026-10-26T07:00:00Z",
    insert: { used: 2, limit: 100, remaining: 98 },
    units: { used: 5, limit: 10000, remaining: 9995 },
  },
  auto_publish_global_enabled: auto,
  connected_channels: 1,
});

function renderChannels(url = "/kanaler") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <Channels />
      </ToastProvider>
    </MemoryRouter>,
  );
}

describe("Channels", () => {
  it("connects another channel with one button press", async () => {
    const f = mockFetch({
      "/channels": { items: [channel()], oauth: oauth() },
      "/publishing/status": status(),
      "/oauth/youtube/start": {
        auth_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1",
        expires_in: 600,
      },
    });
    setCsrf("tok");
    renderChannels();
    const btn = await screen.findByRole("button", {
      name: "Lägg till en kanal till",
    });
    await userEvent.click(btn);
    await waitFor(() =>
      expect(nav.goTo).toHaveBeenCalledWith(
        "https://accounts.google.com/o/oauth2/v2/auth?x=1",
      ),
    );
    const call = f.mock.calls.find((c) =>
      String(c[0]).endsWith("/oauth/youtube/start"),
    )!;
    expect((call[1] as RequestInit).method).toBe("POST");
    expect(JSON.parse(String((call[1] as RequestInit).body))).toEqual({});
  });

  it("offers the first connection when nothing is connected", async () => {
    mockFetch({
      "/channels": { items: [], oauth: oauth() },
      "/publishing/status": status(),
    });
    renderChannels();
    expect(
      await screen.findByRole("button", { name: "Lägg till YouTube-kanal" }),
    ).toBeEnabled();
    expect(
      await screen.findByText(/Inga kanaler anslutna ännu/),
    ).toBeInTheDocument();
  });

  it("explains the missing Google setup instead of failing silently", async () => {
    mockFetch({
      "/channels": {
        items: [],
        oauth: oauth({ client_configured: false, ready: false }),
      },
      "/publishing/status": status(),
    });
    renderChannels();
    expect(
      await screen.findByRole("button", { name: "Lägg till YouTube-kanal" }),
    ).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Google-anslutningen är inte konfigurerad ännu",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "http://127.0.0.1:8000/api/v1/oauth/youtube/callback",
    );
  });

  it("shows a clear message when Google sends the user back with an error", async () => {
    mockFetch({
      "/channels": { items: [], oauth: oauth() },
      "/publishing/status": status(),
    });
    renderChannels("/kanaler?oauth_error=scopes_missing");
    expect(
      await screen.findByText(/godkänna alla begärda behörigheter/),
    ).toBeInTheDocument();
  });

  it("shows connection state, private-by-default and a reconnect action when access expired", async () => {
    mockFetch({
      "/channels": {
        items: [
          channel({ id: "a", display_name: "Kanal A" }),
          channel({
            id: "b",
            display_name: "Kanal B",
            connection_state: "needs_reauth",
            oauth_connected: false,
            connection_error: "YouTube nekade åtkomst.",
          }),
        ],
        oauth: oauth(),
      },
      "/publishing/status": status(true),
    });
    renderChannels();
    const list = await screen.findByRole("list", { name: "Anslutna kanaler" });
    const items = within(list).getAllByRole("listitem");
    expect(within(items[0]!).getByText("Ansluten")).toBeInTheDocument();
    expect(
      within(items[0]!).getByText(/Av – allt laddas upp privat/),
    ).toBeInTheDocument();
    expect(
      within(items[1]!).getByText("YouTube nekade åtkomst."),
    ).toBeInTheDocument();
    expect(
      within(items[1]!).getAllByRole("button", { name: "Anslut igen" }).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("På")).toBeInTheDocument(); // global automatic publishing
  });
});

const pub = (over: Partial<Publication> = {}): Publication => ({
  id: "p1",
  rendered_clip_id: "r1",
  channel_id: "c1",
  channel_name: "Min kanal",
  clip_title: "Ett klipp",
  status: "uploaded",
  upload_succeeded: true,
  availability: "private",
  is_public: false,
  visibility_requested: "private",
  privacy_actual: "private",
  scheduled_for: null,
  uploaded_at: "2026-10-25T10:00:00Z",
  external_video_id: "abc",
  external_url: "https://www.youtube.com/watch?v=abc",
  rejection_reason: null,
  title: "Ett klipp",
  progress_percent: null,
  error_code: null,
  safe_error_message: null,
  notice: null,
  automatic: true,
  last_checked_at: null,
  created_at: "2026-10-25T10:00:00Z",
  job: null,
  ...over,
});

describe("Publications", () => {
  function renderPubs(items: Publication[]) {
    mockFetch({
      "/publications": { items, total: items.length, counts: {} },
      "/publishing/status": status(true),
    });
    return render(
      <MemoryRouter>
        <ToastProvider>
          <Publications />
        </ToastProvider>
      </MemoryRouter>,
    );
  }

  it("never presents an upload as public until YouTube says so", async () => {
    renderPubs([pub()]);
    const row = (await screen.findByText("Ett klipp")).closest("tr")!;
    expect(within(row).getByText("Uppladdad")).toBeInTheDocument();
    expect(within(row).getByText("Privat")).toBeInTheDocument();
    expect(
      within(row).getByText("Uppladdad – inte offentlig ännu"),
    ).toBeInTheDocument();
    const link = within(row).getByRole("link", { name: "Öppna på YouTube" });
    expect(link).toHaveAttribute("href", "https://www.youtube.com/watch?v=abc");
  });

  it("shows public videos as public", async () => {
    renderPubs([
      pub({
        availability: "public",
        is_public: true,
        privacy_actual: "public",
      }),
    ]);
    const row = (await screen.findByText("Ett klipp")).closest("tr")!;
    expect(within(row).getByText("Offentlig")).toBeInTheDocument();
    expect(
      within(row).queryByText("Uppladdad – inte offentlig ännu"),
    ).not.toBeInTheDocument();
  });

  it("shows failures with their reason, a retry button and no YouTube link", async () => {
    renderPubs([
      pub({
        id: "p2",
        status: "failed",
        upload_succeeded: false,
        availability: "unknown",
        external_url: null,
        external_video_id: null,
        safe_error_message: "YouTube avvisade uppgifterna (invalidTags)",
      }),
    ]);
    const row = (await screen.findByText("Ett klipp")).closest("tr")!;
    expect(within(row).getByText("Misslyckades")).toBeInTheDocument();
    expect(within(row).getByText(/invalidTags/)).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Försök igen" }),
    ).toBeInTheDocument();
    expect(
      within(row).queryByRole("link", { name: "Öppna på YouTube" }),
    ).not.toBeInTheDocument();
  });

  it("requires a deliberate choice when the upload outcome is unknown", async () => {
    renderPubs([
      pub({
        status: "blocked",
        upload_succeeded: false,
        external_url: null,
        external_video_id: null,
        error_code: "upload_outcome_unknown",
        safe_error_message:
          "ClipForge vet inte om förra uppladdningen gick igenom",
      }),
    ]);
    const row = (await screen.findByText("Ett klipp")).closest("tr")!;
    expect(
      within(row).queryByRole("button", { name: "Försök igen" }),
    ).not.toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Ladda upp på nytt" }),
    ).toBeInTheDocument();
  });
});
