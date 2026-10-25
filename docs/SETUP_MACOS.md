# Setup on macOS (Apple Silicon)

Verified on a MacBook Pro M1, 16 GB, macOS 27.0.1, 2026-10-25.

1. Native terminal: `uname -m` must print `arm64` (not `x86_64`, which means Rosetta).
2. Homebrew: `brew --version`. Python: `python3 --version` (3.12+; 3.14.5 tested).
3. FFmpeg: `brew install ffmpeg` (8.1.1 tested). ClipForge needs `libx264`, `overlay`, `loudnorm`, and uses
   `h264_videotoolbox` for fast drafts. libass is *not* required.
4. Python environment:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.lock
   ```
5. Dashboard: Node 20+ (`brew install node`), then `cd frontend && npm ci && npm run build`.
6. `./scripts/doctor.sh`, then `./scripts/start.sh`.

## Transcription model

faster-whisper runs on the CPU with int8 weights; it does not use the Apple GPU. Weights download from
Hugging Face into `data/models/faster-whisper/` only after you enable *Tillåt nedladdning* in Inställningar.

| Model | Download | RAM (approx.) | Note |
| --- | --- | --- | --- |
| tiny | ~75 MB | ~0.5 GB | fastest, weakest Swedish |
| base | ~145 MB | ~0.7 GB | default |
| small | ~485 MB | ~1.2 GB | noticeably better Swedish |
| medium | ~1.5 GB | ~2.5 GB | slow on CPU |
| large-v3-turbo / large-v3 | 1.6 / 3 GB | 3 / 4.5 GB | very slow |

Sizes are approximate; speeds have not been benchmarked on this machine yet (see CHECKLIST). The analytics page
shows measured seconds-per-second once real transcriptions run.

## Optional: local LLM (Ollama)

ClipForge works without it. Ollama is already installed on this Mac but was not running at setup time.

```bash
ollama serve            # or open the Ollama app
ollama pull llama3.2:3b # ~2 GB; pick a model that fits beside Whisper in 16 GB
```

Then set *Klippbedömning* to Ollama. If it stops responding, the worker falls back to the heuristic and shows
a degraded notice. Never expose Ollama's port beyond 127.0.0.1.

## Sleep and login items

Jobs only run while the Mac is awake. A sleeping Mac lets leases expire; jobs resume automatically after wake.
ClipForge installs no launch agent. If you want it at login, create a `launchd` plist that runs
`scripts/start.sh` yourself; nothing is installed silently.

## Disk

This Mac had about 17–21 GB free during setup. The default storage budget is 15 GB and new downloads/renders
pause when less than 5 GB of disk is free. Adjust both in Inställningar → Lagring.
