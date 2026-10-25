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
- ClipForge's API source uses `channels.list` once per channel (cached) and `playlistItems.list` per check:
  1–2 units. It never calls `search.list`.
- Quota errors pause that source for 6 hours (`api_quota_exceeded`) and show on the source.

Source: https://developers.google.com/youtube/v3/determine_quota_cost

## Video files

The YouTube Data API has no endpoint for downloading video files, and ClipForge will not scrape or use
third-party downloaders. For your own channel, download originals from YouTube Studio and attach them in the
dashboard (video page → *Bifoga fil*) or drop them in `data/import/`.

## Publishing (phase 6, not implemented)

What that phase will need, to be re-verified against the docs at implementation time:

- An OAuth client (Desktop app type) with the `youtube.upload` scope (and `youtube.readonly` to verify the channel),
  loopback redirect, `state` + PKCE. Tokens in macOS Keychain.
- Google's OAuth consent screen: while the app is in *Testing*, only listed test users can authorise and refresh
  tokens expire after 7 days. Unverified API projects may have uploads restricted to private visibility until
  they pass an audit. ClipForge will default every channel to private uploads.
- `videos.insert` is limited to 100 calls/day per project under the current quota model.
- Resumable uploads with stored session URIs and reconciliation before any retry, so a timeout never produces
  a duplicate public video.
