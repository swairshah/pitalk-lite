# pitalk-lite (PiTalk Lite local workspace)

This directory is the **single source of truth** for your Linux PiTalk-lite setup.

## Main files
- `linux_tts_broker.py` — local broker (OpenAI TTS, queueing, mic-aware behavior)
- `pitalk_tray.py` — tray/menu app (sessions, speed, stop, quit-all)
- `voice_picker.py` — voice selection helper
- `extension-overrides/pi-talk-index.ts` — patched `@swairshah/pi-talk` extension source to sync into node_modules
- `scripts/setup_sync.sh` — apply sync + update systemd services to point here

## Apply changes after edits
Run:

```bash
bash /home/swair/Work/pitalk-lite/scripts/setup_sync.sh
```

What it does:
1. Syncs extension override into installed `@swairshah/pi-talk/index.ts`
2. Rewrites user systemd services so they execute Python files from this directory
3. Reloads systemd and restarts both services

## Service names
- `pitalk-lite-broker.service`
- `pitalk-lite-tray.service`
