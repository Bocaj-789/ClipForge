import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Channel, OAuthInfo, PublishingStatus } from "../api/types";
import { ConfirmButton, Empty, Notice, Pill, useToast } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { t } from "../i18n/sv";
import { fmtTime } from "../lib/format";
import { goTo } from "../lib/nav";

const DAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"];

function ChannelForm({
  channel,
  onSaved,
}: {
  channel: Channel;
  onSaved: () => void;
}) {
  const [perDay, setPerDay] = useState(channel.max_uploads_per_day);
  const [spacing, setSpacing] = useState(channel.min_spacing_minutes);
  const [visibility, setVisibility] = useState(channel.default_visibility);
  const [days, setDays] = useState<number[]>(
    channel.schedule.days ?? [0, 1, 2, 3, 4, 5, 6],
  );
  const [from, setFrom] = useState(
    channel.schedule.windows?.[0]?.[0] ?? "09:00",
  );
  const [to, setTo] = useState(channel.schedule.windows?.[0]?.[1] ?? "21:00");
  const [releaseSpacing, setReleaseSpacing] = useState<string>(
    channel.release_spacing_minutes?.toString() ?? "",
  );
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api(`/channels/${channel.id}/settings`, {
        method: "PATCH",
        body: {
          release_spacing_minutes: releaseSpacing
            ? Number(releaseSpacing)
            : null,
          max_uploads_per_day: perDay,
          min_spacing_minutes: spacing,
          default_visibility: visibility,
          schedule: { days, windows: [[from, to]] },
        },
      });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    }
  }
  return (
    <form className="stack" onSubmit={submit}>
      {error && <Notice tone="fail">{error}</Notice>}
      <p className="small muted">{t.channels.limitsLead}</p>
      <div className="form-grid">
        <label>
          {t.channels.releaseSpacing}
          <input
            type="number"
            min={30}
            max={10080}
            placeholder={t.channels.releaseNow}
            value={releaseSpacing}
            onChange={(e) => setReleaseSpacing(e.target.value)}
          />
          <span className="help">{t.channels.releaseHelp}</span>
        </label>
        <label>
          {t.channels.perDay}
          <input
            type="number"
            min={0}
            max={20}
            value={perDay}
            onChange={(e) => setPerDay(Number(e.target.value))}
          />
        </label>
        <label>
          {t.channels.spacing}
          <input
            type="number"
            min={10}
            max={1440}
            value={spacing}
            onChange={(e) => setSpacing(Number(e.target.value))}
          />
        </label>
        <label>
          {t.channels.visibility}
          <select
            value={visibility}
            onChange={(e) => setVisibility(e.target.value)}
          >
            <option value="private">{t.clipPublish.vis.private}</option>
            <option value="unlisted">{t.clipPublish.vis.unlisted}</option>
            <option value="public" disabled={!channel.allow_public}>
              {t.clipPublish.vis.public}
              {channel.allow_public
                ? ""
                : " (kräver tillåten offentlig publicering)"}
            </option>
          </select>
        </label>
      </div>
      <fieldset className="row" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="small muted" style={{ marginBottom: "0.4rem" }}>
          {t.channels.windows}
        </legend>
        {DAYS.map((d, i) => (
          <label key={d} className="check small">
            <input
              type="checkbox"
              checked={days.includes(i)}
              onChange={(e) =>
                setDays(
                  e.target.checked
                    ? [...days, i].sort()
                    : days.filter((x) => x !== i),
                )
              }
            />
            {d}
          </label>
        ))}
        <label
          className="row"
          style={{ flexDirection: "row", alignItems: "center" }}
        >
          från{" "}
          <input
            type="time"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </label>
        <label
          className="row"
          style={{ flexDirection: "row", alignItems: "center" }}
        >
          till{" "}
          <input
            type="time"
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </label>
      </fieldset>
      <div>
        <button type="submit">{t.common.save}</button>
      </div>
    </form>
  );
}

function ConnectionPill({ c }: { c: Channel }) {
  if (c.connection_state === "connected")
    return <Pill tone="done">{t.channels.connected}</Pill>;
  if (c.connection_state === "needs_reauth")
    return <Pill tone="fail">{t.channels.needsReauth}</Pill>;
  return <Pill tone="muted">{t.channels.disconnected}</Pill>;
}

export default function Channels() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const q = usePoll<{ items: Channel[]; oauth: OAuthInfo }>(
    (s) => api("/channels", { signal: s }),
    15000,
  );
  const st = usePoll<PublishingStatus>(
    (s) => api("/publishing/status", { signal: s }),
    15000,
  );
  const [editing, setEditing] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  // Google sends the browser back here with ?connected=<id> or ?oauth_error=<code>.
  useEffect(() => {
    const ok = params.get("connected");
    const err = params.get("oauth_error");
    if (!ok && !err) return;
    if (ok) toast(t.channels.connectedToast);
    if (err) toast(t.channels.errors[err] ?? t.errors.generic, "fail");
    setParams({}, { replace: true });
    q.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  async function connect(channelId?: string) {
    setConnecting(true);
    try {
      const r = await api<{ auth_url: string }>("/oauth/youtube/start", {
        method: "POST",
        body: channelId ? { channel_id: channelId } : {},
      });
      goTo(r.auth_url);
    } catch (e) {
      toast(errorMessage(e), "fail");
      setConnecting(false);
    }
  }
  async function post(path: string, body?: unknown, okText?: string) {
    try {
      await api(path, { method: "POST", body: body ?? {} });
      if (okText) toast(okText);
      q.reload();
      st.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  }

  async function deleteChannel(cid: string) {
    try {
      await api(`/channels/${cid}`, { method: "DELETE" });
      q.reload();
      st.reload();
    } catch (e) {
      toast(errorMessage(e), "fail");
    }
  }

  const oauth = q.data?.oauth;
  const items = q.data?.items ?? [];
  const globalAuto = st.data?.auto_publish_global_enabled ?? false;
  return (
    <div className="page">
      <header className="page-head">
        <h1>{t.channels.title}</h1>
        <p className="muted">{t.channels.lead}</p>
      </header>

      {oauth && !oauth.client_configured && (
        <Notice tone="fail">
          <strong>{t.channels.setupNeeded}.</strong> {t.channels.setupHelp}{" "}
          <code>{oauth.redirect_uri}</code>
        </Notice>
      )}
      {oauth && oauth.client_configured && !oauth.store_available && (
        <Notice tone="fail">
          <strong>{t.channels.storeMissing}.</strong> {oauth.store_reason}
        </Notice>
      )}

      <section className="panel stack">
        <div className="row">
          <button
            className="primary"
            onClick={() => connect()}
            disabled={connecting || !oauth?.ready}
          >
            {connecting
              ? t.channels.connecting
              : items.length
                ? t.channels.connectAnother
                : t.channels.connect}
          </button>
        </div>
        <p className="small muted">{t.channels.connectHelp}</p>
      </section>

      <section className="panel stack" aria-labelledby="auto-h">
        <div className="spread">
          <h2 id="auto-h">{t.publishing.autoTitle}</h2>
          <Pill tone={globalAuto ? "done" : "muted"}>
            {globalAuto ? "På" : "Av"}
          </Pill>
        </div>
        <p className="small">
          {globalAuto ? t.publishing.autoOn : t.publishing.autoOff}
        </p>
        <div>
          {globalAuto ? (
            <button
              className="small"
              onClick={async () => {
                try {
                  await api("/settings", {
                    method: "PATCH",
                    body: { changes: { auto_publish_global_enabled: false } },
                  });
                  st.reload();
                } catch (e) {
                  toast(errorMessage(e), "fail");
                }
              }}
            >
              {t.publishing.disableAuto}
            </button>
          ) : (
            <ConfirmButton
              className="primary small"
              message={t.publishing.enableMessage}
              confirmLabel={t.publishing.enableAuto}
              onConfirm={async () => {
                try {
                  await api("/settings", {
                    method: "PATCH",
                    body: {
                      changes: { auto_publish_global_enabled: true },
                      confirm: "JA",
                    },
                  });
                  st.reload();
                } catch (e) {
                  toast(errorMessage(e), "fail");
                }
              }}
            >
              {t.publishing.enableAuto}
            </ConfirmButton>
          )}
        </div>
      </section>

      {q.error && <Notice tone="fail">{q.error}</Notice>}
      {q.data && items.length === 0 ? (
        <Empty>{t.channels.empty}</Empty>
      ) : (
        <ul className="list" aria-label="Anslutna kanaler">
          {items.map((c) => (
            <li
              key={c.id}
              style={{ flexDirection: "column", alignItems: "stretch" }}
            >
              <div className="spread">
                <div className="stack" style={{ gap: "0.3rem" }}>
                  <span className="title-line">
                    {c.youtube_url ? (
                      <a
                        href={c.youtube_url}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        {c.display_name}
                      </a>
                    ) : (
                      c.display_name
                    )}
                    {c.youtube_handle && (
                      <span className="muted"> {c.youtube_handle}</span>
                    )}
                  </span>
                  <div className="row small muted">
                    <ConnectionPill c={c} />
                    <Pill tone={c.allow_public ? "review" : "muted"}>
                      {t.channels.publicLabel}:{" "}
                      {c.allow_public
                        ? t.channels.publicOn
                        : t.channels.publicOff}
                    </Pill>
                    <span>
                      {c.release_spacing_minutes
                        ? t.channels.releaseEvery.replace(
                            "{minutes}",
                            String(c.release_spacing_minutes),
                          )
                        : t.channels.releaseNow}
                    </span>
                    <span>max {c.max_uploads_per_day}/dag</span>
                    <span>
                      {t.channels.lastUpload}: {fmtTime(c.last_upload_at)}
                    </span>
                  </div>
                  {c.connection_error && c.connection_state !== "connected" && (
                    <span className="small" style={{ color: "var(--straw)" }}>
                      {c.connection_error}
                    </span>
                  )}
                </div>
                <div className="row">
                  <button
                    className="small"
                    onClick={() => setEditing(editing === c.id ? null : c.id)}
                    aria-expanded={editing === c.id}
                  >
                    {editing === c.id ? t.common.close : t.channels.edit}
                  </button>
                  {c.connection_state !== "disconnected" ? (
                    <>
                      {c.connection_state === "needs_reauth" && (
                        <button
                          className="small primary"
                          onClick={() => connect(c.id)}
                          disabled={connecting}
                        >
                          {t.channels.reconnect}
                        </button>
                      )}
                      <ConfirmButton
                        className="small ghost"
                        message={t.channels.disconnectConfirm}
                        onConfirm={() => post(`/channels/${c.id}/disconnect`)}
                      >
                        {t.channels.disconnect}
                      </ConfirmButton>
                    </>
                  ) : (
                    <>
                      <button
                        className="small primary"
                        onClick={() => connect(c.id)}
                        disabled={connecting}
                      >
                        {t.channels.reconnect}
                      </button>
                      <ConfirmButton
                        className="small ghost"
                        message="Ta bort kanalen från ClipForge?"
                        onConfirm={() => deleteChannel(c.id)}
                      >
                        {t.common.delete}
                      </ConfirmButton>
                    </>
                  )}
                </div>
              </div>
              <div className="row">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={c.auto_publish_enabled}
                    disabled={c.connection_state !== "connected"}
                    onChange={(e) =>
                      post(`/channels/${c.id}/auto-publish`, {
                        enabled: e.target.checked,
                      })
                    }
                  />
                  {t.channels.inAuto}
                </label>
                <span className="small muted">{t.channels.autoNote}</span>
              </div>
              <div className="row">
                {c.allow_public ? (
                  <button
                    className="small"
                    onClick={() =>
                      post(`/channels/${c.id}/allow-public`, { enabled: false })
                    }
                  >
                    {t.channels.revokePublic}
                  </button>
                ) : (
                  <ConfirmButton
                    className="small"
                    message={t.channels.allowPublicMessage}
                    requirePhrase="OFFENTLIG PUBLICERING"
                    onConfirm={(phrase) =>
                      post(`/channels/${c.id}/allow-public`, {
                        enabled: true,
                        confirm_phrase: phrase,
                      })
                    }
                  >
                    {t.channels.allowPublic}
                  </ConfirmButton>
                )}
              </div>
              {editing === c.id && (
                <ChannelForm
                  channel={c}
                  onSaved={() => {
                    toast("Kanalen sparades.");
                    q.reload();
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {items.length > 0 && (
        <p className="small">
          <Link to="/publicering">{t.publishing.title}</Link>
        </p>
      )}
    </div>
  );
}
