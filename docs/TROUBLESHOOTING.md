# Troubleshooting

**Dashboard says "Arbetarprocessen körs inte".** The worker isn't running or hasn't sent a heartbeat for 45 s.
Run `./scripts/start.sh` and check `data/logs/worker.out`.

**Video stuck as *Blockerad* with a model message.** No Whisper model downloaded. Inställningar →
Transkribering → enable *Tillåt nedladdning* (type JA), save, then *Kör igen* on the job in Kö.

**YouTube link blocked: "YouTube erbjuder inget officiellt API…".** Expected. Attach the original file on the
video page if the material is yours.

**"Rättigheterna är inte bekräftade".** Set the rights status on the video page. Discovery never sets it for you.

**URL rejected.** Only https on port 443 to public hostnames. Direct files only from hosts in
Inställningar → *Tillåtna mediavärdar*.

**Renders fail with "ffmpeg misslyckades".** The full ffmpeg error is in `data/logs/worker.log`. Check
`./scripts/doctor.sh` for ffmpeg. Fast draft uses `h264_videotoolbox`; switch profile if VideoToolbox misbehaves.

**Captions show boxes instead of å/ä/ö.** Set *Typsnittsfil för undertexter* to a TTF that covers Swedish, e.g.
`/System/Library/Fonts/Supplemental/Arial Bold.ttf`.

**Jobs pile up in *Nytt försök snart*.** Transient errors (network, quota). The next attempt time is shown;
*Kör igen* resets the attempt budget.

**"För många inloggningsförsök".** Wait a minute. Locked out: `python -m app.cli reset-password <user>`.

**Mac went to sleep mid-render.** The job's lease expires and it restarts from its last stage after wake.
Partial files in `data/tmp` are cleaned up.

**API won't start: "CLIPFORGE_HOST must be a loopback address".** Intentional. Use a tunnel for remote access.

**Database errors after an upgrade.** Stop, restore the latest backup (see OPERATIONS.md), report the error from
`data/logs/api.log`. ClipForge doesn't overwrite or "repair" a database automatically.
