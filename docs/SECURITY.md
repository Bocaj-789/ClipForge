# Security

ClipForge will eventually hold YouTube credentials and can publish publicly, so it is treated as
security-sensitive even though it is single-user and local.

## Controls in place (tested unless noted)

- **Bind address:** config rejects anything but loopback. Ollama URL must be loopback too.
- **Authentication:** required for every endpoint except `/api/v1/health`. First account needs a one-time setup
  code printed locally and stored at `data/secrets/setup-token.txt` (0600, deleted after use). No default password.
- **Passwords:** Argon2id (argon2-cffi defaults), minimum 12 characters, timing-equalised login.
- **Sessions:** random 256-bit token, only its SHA-256 is stored; HttpOnly, SameSite=Strict, Secure when served
  over HTTPS; 72 h expiry; logout, logout-everywhere, password change revokes all sessions.
- **CSRF:** per-session token in `X-CSRF-Token` for every non-GET request, plus Origin must match Host.
- **Rate limits:** login 5/min, setup 5/5 min per client.
- **Headers:** strict CSP (`script-src 'self'`, no inline scripts, no third-party origins except YouTube thumbnails),
  `frame-ancestors 'none'`, nosniff, no-referrer, `Cache-Control: no-store` on API responses. OpenAPI docs only
  with `CLIPFORGE_DEV_MODE=true`.
- **SSRF:** user URLs must be https, port 443, no credentials, no IP literals, no internal TLDs; hosts are
  allowlisted. Fetching resolves DNS itself, requires every address to be public, connects to that vetted IP
  (DNS-rebinding safe) and revalidates every redirect.
- **Files:** uploads streamed to `data/tmp` with a size cap, validated by ffprobe, stored under generated names.
  Every path read from the DB passes a containment check (no traversal, no symlink escape). Originals in
  `data/import` are never deleted.
- **Subprocesses:** ffmpeg/ffprobe always run with argument lists, never a shell.
- **Untrusted content:** titles, descriptions, transcripts and feeds are data. XML with DTDs is rejected. LLM
  prompts label transcripts as untrusted data; responses are Pydantic-validated and can only select existing
  sentence ranges. No model can change settings or publishing; policy is deterministic code.
- **Secrets:** `.env` and `data/` are gitignored; logs and audit details pass through a redaction filter; API
  responses never include keys or tokens; backups exclude `data/secrets` and session rows.

## YouTube OAuth and tokens
- Authorization-code flow with PKCE (S256). `state` is 256-bit, stored hashed, single use, valid 10 minutes and
  bound to the starting browser by a `Lax` cookie scoped to the callback path (the `Strict` session cookie is not
  sent on Google's cross-site redirect). The callback performs no action without a valid state.
- Refresh tokens are stored only in the macOS Keychain (via the `keyring` package, no secret on a command line).
  The DB keeps a reference. Access tokens live in process memory for under an hour. The client secret stays in
  `.env`/`data/secrets` (gitignored, excluded from backups) and is only sent to Google by the server.
- Tokens, codes and `state` are redacted from logs, audit events and the uvicorn access log; no API response
  contains them. Tests scan responses, the SQLite files and log files for token strings.
- Fails closed: without a usable Keychain no channel can be connected. `CLIPFORGE_TOKEN_STORE=file` is an explicit
  weaker option.
- Resumable-session URIs from Google are only used if they are https on `*.googleapis.com`.
- Public (or scheduled-to-be-public) uploads need a per-channel typed opt-in; everything defaults to private.

## Not yet implemented
- Remote access hardening beyond the above (2FA or an identity-aware proxy).

## Before enabling remote access

The custom domain and DNS provider are not confirmed yet, so nothing is configured. When they are:

- [ ] Use Cloudflare Tunnel (or similar) pointing at `http://127.0.0.1:8000`; never change the bind address.
- [ ] Put Cloudflare Access (or equivalent identity layer) in front, in addition to the ClipForge login.
- [ ] HTTPS only; confirm the session cookie gets `Secure` (the API honours `X-Forwarded-Proto` from loopback).
- [ ] Confirm `/api/docs` is 404, rate limiting works through the tunnel, and SQLite/Ollama/worker ports are
      not exposed.
- [ ] Test backup and restore; document the off switch (`cloudflared` service stop) and keep it one command.
- [ ] Set `remote_access_enabled` in Inställningar only after all of the above.

A sleeping or offline Mac makes both the dashboard and the agent unavailable; a tunnel doesn't change that.

## Reporting a lost password

Run locally: `cd backend && ../.venv/bin/python -m app.cli reset-password <username>`. It revokes all sessions.
