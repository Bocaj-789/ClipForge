import { useCallback, useEffect, useState } from "react";
import {
  BrowserRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import {
  api,
  onConnectionChange,
  setCsrf,
  setUnauthorizedHandler,
} from "./api/client";
import type { Me, Summary } from "./api/types";
import { ToastProvider } from "./components/ui";
import { usePoll } from "./hooks/usePoll";
import { t } from "./i18n/sv";
import { setTimezone } from "./lib/format";
import Analytics from "./pages/Analytics";
import Channels from "./pages/Channels";
import ClipDetail from "./pages/ClipDetail";
import Clips from "./pages/Clips";
import Discovery from "./pages/Discovery";
import Inbox from "./pages/Inbox";
import Login from "./pages/Login";
import Logs from "./pages/Logs";
import Overview from "./pages/Overview";
import Publications from "./pages/Publications";
import Queue from "./pages/Queue";
import Settings from "./pages/Settings";
import VideoDetail from "./pages/VideoDetail";

function Shell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [offline, setOffline] = useState(false);
  useEffect(() => onConnectionChange((on) => setOffline(!on)), []);
  const { data } = usePoll<Summary>(
    (s) => api("/dashboard/summary", { signal: s }),
    15000,
  );
  useEffect(() => {
    api<{ settings: { timezone: string } }>("/settings")
      .then((r) => setTimezone(r.settings.timezone))
      .catch(() => {});
  }, []);
  const links: [string, string, number?][] = [
    ["/", t.nav.overview],
    ["/inkorg", t.nav.inbox],
    ["/ko", t.nav.queue],
    ["/klipp", t.nav.clips, data?.clips_awaiting_review],
    ["/upptackt", t.nav.discovery],
    ["/kanaler", t.nav.channels],
    ["/publicering", t.nav.publishing],
    ["/analys", t.nav.analytics],
    ["/installningar", t.nav.settings],
    ["/aktivitet", t.nav.logs],
  ];
  return (
    <>
      {offline && (
        <div className="banner" role="alert">
          {t.errors.offline}
        </div>
      )}
      <div className="shell">
        <aside className="rail">
          <div className="wordmark" aria-label="ClipForge">
            Clip<span>Forge</span>
          </div>
          <nav aria-label="Huvudmeny">
            {links.map(([to, label, badge]) => (
              <NavLink key={to} to={to} end={to === "/"}>
                {label}
                {badge ? (
                  <span className="badge" aria-label={`${badge} att granska`}>
                    {badge}
                  </span>
                ) : null}
              </NavLink>
            ))}
          </nav>
          <div className="foot small muted">
            <span>{me.username}</span>
            <button type="button" className="ghost small" onClick={onLogout}>
              {t.auth.logout}
            </button>
          </div>
        </aside>
        <main id="main">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/inkorg" element={<Inbox />} />
            <Route path="/ko" element={<Queue />} />
            <Route path="/klipp" element={<Clips />} />
            <Route path="/klipp/:id" element={<ClipDetail />} />
            <Route path="/video/:id" element={<VideoDetail />} />
            <Route path="/upptackt" element={<Discovery />} />
            <Route path="/kanaler" element={<Channels />} />
            <Route path="/publicering" element={<Publications />} />
            <Route path="/analys" element={<Analytics />} />
            <Route path="/installningar" element={<Settings />} />
            <Route path="/aktivitet" element={<Logs />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const m = await api<Me>("/auth/me");
      setCsrf(m.csrf_token ?? null);
      setMe(m);
      setBootError(null);
    } catch (e) {
      setBootError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    refresh();
    setUnauthorizedHandler(() => {
      setCsrf(null);
      setMe({ authenticated: false, setup_required: false });
    });
  }, [refresh]);

  if (!me) {
    return (
      <div className="auth-wrap">
        {bootError ? (
          <p role="alert">{bootError}</p>
        ) : (
          <p>{t.common.loading}</p>
        )}
      </div>
    );
  }
  return (
    <ToastProvider>
      <BrowserRouter>
        {me.authenticated ? (
          <Shell
            me={me}
            onLogout={async () => {
              await api("/auth/logout", { method: "POST" }).catch(() => {});
              setCsrf(null);
              setMe({ authenticated: false, setup_required: false });
            }}
          />
        ) : (
          <Login setupRequired={me.setup_required} onDone={refresh} />
        )}
      </BrowserRouter>
    </ToastProvider>
  );
}
