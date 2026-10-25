import { useState, type FormEvent } from "react";
import { api, errorMessage, setCsrf } from "../api/client";
import { Notice } from "../components/ui";
import { t } from "../i18n/sv";

export default function Login({ setupRequired, onDone }: { setupRequired: boolean; onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ csrf_token: string }>(setupRequired ? "/auth/setup" : "/auth/login", {
        body: setupRequired ? { username, password, setup_token: token } : { username, password },
      });
      setCsrf(r.csrf_token);
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth" onSubmit={submit}>
        <div className="wordmark">
          Clip<span>Forge</span>
        </div>
        <h1>{setupRequired ? t.auth.setupTitle : t.auth.loginTitle}</h1>
        {setupRequired && <p className="help">{t.auth.setupHelp}</p>}
        {error && <Notice tone="fail">{error}</Notice>}
        {setupRequired && (
          <label>
            {t.auth.setupToken}
            <input value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" required />
          </label>
        )}
        <label>
          {t.auth.username}
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          {t.auth.password}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={setupRequired ? "new-password" : "current-password"}
            minLength={setupRequired ? 12 : 1}
            required
          />
          {setupRequired && <span className="help">{t.auth.passwordHint}</span>}
        </label>
        <button className="primary" type="submit" disabled={busy}>
          {setupRequired ? t.auth.create : t.auth.login}
        </button>
      </form>
    </div>
  );
}
