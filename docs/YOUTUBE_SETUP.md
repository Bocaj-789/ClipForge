# YouTube setup

## Metadata only (works today)

- **oEmbed:** every YouTube link in the inbox gets title, channel and thumbnail from the public oEmbed endpoint.
  No key, no quota.
- **Channel feeds (RSS source):** `https://www.youtube.com/feeds/videos.xml?channel_id=UC…`. No key, no quota.
  Add a channel by its `youtube.com/channel/UC…` URL; handles (`@name`) need the API source.
- **YouTube Data API source (optional):** create a Google Cloud project, enable *YouTube Data API v3*, create an
  API key restricted to that API, and put it in `.env` as `CLIPFORGE_YOUTUBE_API_KEY`. Billing is not required
  and should not be enabled for this.

### Quota (official table, checked 2026-10-25; page last updated 2026-10-15)

- Default allocation: 10,000 units/day for most endpoints, plus separate buckets of 100 `search.list` calls and
  100 `videos.insert` calls per day. Quotas reset at midnight Pacific Time.
- `channels.list`, `playlistItems.list`, `videos.list`: 1 unit each.
- `videos.insert` (an upload) has its own bucket of 100 per day and project, 1 per call. Failed calls count too.
- ClipForge's API source uses `channels.list` once per channel (cached) and `playlistItems.list` per check:
  1–2 units. It never calls `search.list`.
- Quota errors pause that source for 6 hours (`api_quota_exceeded`) and show on the source.

Source: https://developers.google.com/youtube/v3/determine_quota_cost

## Video files

The YouTube Data API has no endpoint for downloading video files, and ClipForge will not scrape or use
third-party downloaders. For your own channel, download originals from YouTube Studio and attach them in the
dashboard (video page → *Bifoga fil*) or drop them in `data/import/`.

## Publishing to your channels

ClipForge uploads your rendered MP4s with the official YouTube Data API (`videos.insert`, resumable upload) after
you authorise each channel through Google's OAuth 2.0 consent screen. No browser automation, no scraping, no
paid services.

### 1. One-time Google Cloud setup (you do this, ~10 minutes)

1. Open <https://console.cloud.google.com/> and create a project (e.g. *ClipForge*). Billing is not needed.
2. **APIs & Services → Library →** search *YouTube Data API v3* → **Enable**.
3. **APIs & Services → OAuth consent screen** (called *Google Auth Platform* in newer consoles):
   - User type **External**. App name *ClipForge*, your e-mail as support and developer contact.
   - **Data access / Scopes →** add exactly these two:
     - `https://www.googleapis.com/auth/youtube.upload` (upload videos)
     - `https://www.googleapis.com/auth/youtube.readonly` (identify which channel was connected and read back a
       video's status)
   - **Audience / Test users →** add every Google account that owns a channel you want to connect.
   - Publishing status stays **Testing**. This is fine for personal use, with one catch: while an external app is
     in *Testing*, Google expires refresh tokens after **7 days**. ClipForge then shows the channel as
     *Anslut igen* and you press that button. To avoid it you must move the app to *In production* (the
     `youtube.upload` scope is sensitive, so Google may ask you to verify the app, which can take weeks; unverified
     apps show a warning screen and are limited to 100 users).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, application type **Web application**:
   - **Authorised redirect URIs:** add exactly `http://127.0.0.1:8000/api/v1/oauth/youtube/callback`
     (change the port if you set `CLIPFORGE_PORT`; the host must be `127.0.0.1`, not `localhost`).
   - No JavaScript origins are needed.
5. Copy the client ID and secret into `.env` in the repository root (never commit it):

   ```
   CLIPFORGE_GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   CLIPFORGE_GOOGLE_CLIENT_SECRET=GOCSPX-...
   ```

   Alternatively download the client JSON and save it as `data/secrets/google_client_secret.json`.
6. Restart ClipForge (`./scripts/stop.sh && ./scripts/start.sh`). Check `./scripts/doctor.sh`: *OAuth-klient* should
   say it is configured.

### 2. Connecting channels (as many as you like)

1. Open ClipForge **at `http://127.0.0.1:8000`** (the same host as the redirect URI) and go to **Kanaler**.
2. Press **Lägg till YouTube-kanal**. Google opens; pick the account, and **on a Brand Account choose the channel**
   when Google asks which channel to use. Leave both permission boxes ticked and continue. (An unverified-app
   warning is expected in *Testing*: *Advanced → Go to ClipForge*.)
3. You land back on **Kanaler** with the channel connected. Press the button again for the next channel (another
   Google account, or another brand channel of the same account). Each channel has its own token and settings.
4. The first time, macOS may ask whether Python may use the Keychain item *ClipForge*. Choose **Always Allow**,
   because the background worker reads the token without a window to show the prompt.

Where things are stored: the refresh token lives in the macOS Keychain (service *ClipForge*, account
`youtube/<channel id>`). The database only holds a reference, the channel id and the display name. Tokens never
appear in API responses, logs, audit events or the frontend. **Koppla från** revokes the token at Google and deletes
the Keychain item. If the Keychain is unavailable ClipForge refuses to connect channels instead of storing
tokens in clear text (`CLIPFORGE_TOKEN_STORE=file` is an explicit weaker opt-in: a `0600` file in `data/secrets`).

### 3. Automatic publishing

On **Kanaler**, press **Slå på automatisk publicering** (confirmation required). From then on every clip that
finishes rendering is uploaded to **every connected channel that is ticked *Med i automatisk publicering***
(new channels are ticked by default). Rules that always apply:

- Uploads are **private** unless you explicitly press **Tillåt offentlig publicering** for that channel (typed
  confirmation) *and* set its default visibility to *Offentlig*. Public uploads outside the channel's publishing
  window are uploaded privately and released by YouTube at the next window (`publishAt`). Clips with render
  warnings are never auto-published publicly.
- Rights must be confirmed for the source video; rejected clips are never uploaded.
- Per-channel daily limit (default 10 for connected channels), minimum spacing, the global daily limit
  (*Inställningar → Publicering*, default 30, but an earlier saved value of 3 stays until you change it) and the
  API quota below are respected. When the quota is used up, uploads wait automatically for the reset.
- Manual publishing from a clip's page (*Publicera på YouTube*) is not throttled by the daily limits because you
  choose it yourself; rights, approval, privacy rules and quota still apply.

**Uploaded is not public.** *Uppladdad* means YouTube accepted the file. What viewers can see is shown separately
(*Privat*, *Olistad*, *Schemalagd*, *Offentlig*, *YouTube bearbetar*), read back from the API and refreshed every
10 minutes while a video is processing or scheduled. The upload page shows the YouTube link.

### 4. Quota and limits

- Default project quota: 10,000 units/day plus 100 `videos.insert` calls/day (resets midnight Pacific Time).
  So at most 100 uploads per day across **all** channels, e.g. 3 channels × 33 clips.
- ClipForge counts its own calls (*Publicering* page) and stops at the limits in *Inställningar → Publicering*
  (set them lower for headroom). A real `quotaExceeded` from Google is also honoured: the jobs wait for the reset
  without using retries.
- Each upload costs 1 insert call plus about 2 units (status check). Status refreshes cost 1 unit per channel.
- YouTube applies its own per-channel daily upload limit, which depends on the channel; ClipForge waits and
  retries later when it is hit.
- Google may restrict uploads from API projects that have not passed their audit (historically forcing them to
  private). If YouTube returns a different privacy than requested, the upload is flagged with an explanation.

### 5. Failure handling

Each channel has its own job. A failure on one never blocks the others, and *Försök igen* only retries the failed
channel. Network and 5xx errors resume the same resumable session; temporary errors retry with growing delays (5
attempts); a revoked or expired token blocks only that channel until you press *Anslut igen*; invalid metadata
fails without retrying. If YouTube may have received a whole file but the answer was lost and the session has
expired, ClipForge looks for the video among the channel's recent uploads and otherwise stops with *Behöver din
åtgärd* so that you can check YouTube Studio, rather than risk a duplicate.

### 6. Testing it for real (not covered by automated tests)

The automated tests use a simulated Google. To verify with the real service: add your own Google account as a
test user, connect a channel, open a rendered clip, tick that channel and press *Publicera* with *Privat*. Expect
the video in YouTube Studio as a private upload within a minute or two. Delete it there afterwards.

### Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Button disabled, "Google-anslutningen är inte konfigurerad" | Missing `CLIPFORGE_GOOGLE_CLIENT_ID/SECRET` or JSON file; restart after editing `.env`. |
| Google error `redirect_uri_mismatch` | The URI in Google Cloud must equal `http://127.0.0.1:<port>/api/v1/oauth/youtube/callback` exactly. Open ClipForge via `127.0.0.1`, not `localhost`. |
| Google error `access_denied` / "app not verified" | Add the account under *Test users*; in the warning screen choose *Advanced → Go to ClipForge*. |
| Returned with "Anslutningen kunde inte verifieras" | The link was used twice, expired (10 min) or was opened in another browser. Start again from the same browser. |
| "Google skickade ingen långlivad token" | Remove ClipForge at <https://myaccount.google.com/permissions> and connect again. |
| "Google-kontot har ingen YouTube-kanal" | Create a channel at youtube.com first. |
| "Du måste godkänna alla begärda behörigheter" | Keep both permission boxes ticked on Google's screen. |
| Channel shows *Anslut igen* about a week after connecting | Testing-mode refresh tokens expire after 7 days; see step 3 of the setup. Also happens if you revoke access in your Google account. |
| `accessNotConfigured` / "YouTube Data API has not been used" | Enable *YouTube Data API v3* in the same Google Cloud project as the OAuth client. |
| Uploads sit in *I kö* with "YouTube-kvoten är slut" | Quota reached; they continue after midnight Pacific (see *Publicering* for the time). |
| Upload is *Uppladdad* but *Privat* although you chose public | Public needs *Tillåt offentlig publicering* on the channel; YouTube may also force private (shown as a note). Change it in YouTube Studio. |
| Keychain prompt hangs the worker | Open *Keychain Access*, find *ClipForge*, *Access Control → Allow all applications* (or add the venv's Python). |
| Worker banner "Säker tokenlagring saknas" | The Keychain could not be reached from the worker; run the worker from your logged-in desktop session (`./scripts/start.sh`). |
