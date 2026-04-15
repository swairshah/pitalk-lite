# PiTalk Lite Linux Broker (OpenAI TTS)

This broker is compatible with the `@swairshah/pi-talk` extension protocol:

- Health HTTP: `127.0.0.1:18080/health`
- Broker TCP (NDJSON): `127.0.0.1:18081`
- Commands: `speak`, `stop`, `health`, `status` (status is accepted as no-op)

## Files

- `linux_tts_broker.py`
- `pitalk_tray.py` (Linux tray / menu bar prototype)

## Requirements

- Python 3.10+
- `ffplay` installed (`ffmpeg` package)
- `OPENAI_API_KEY` in env or `~/.env`

## Run

```bash
python ~/Work/pi-talk-lite/linux_tts_broker.py
```

## Run in background

```bash
nohup python ~/Work/pi-talk-lite/linux_tts_broker.py >/tmp/pitalk-lite.log 2>&1 &
```

## Stop

```bash
pkill -f linux_tts_broker.py
```

## Quick checks

```bash
curl http://127.0.0.1:18080/health
```

```bash
python - <<'PY'
import socket
s=socket.create_connection(('127.0.0.1',18081),timeout=5)
s.sendall(b'{"type":"speak","text":"Hello from PiTalk lite"}\n')
print(s.recv(4096).decode())
s.close()
PY
```

## Tray app (prototype)

Requires GTK/AppIndicator bindings:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
```

Run:

```bash
python ~/Work/pi-talk-lite/pitalk_tray.py
```

Features:

- Menu bar icon with live summary
- Active session list (from `status` events)
- Click a session to jump to tmux pane by `πid<PID>` marker
- Speech speed presets (0.8x to 1.6x)
- Stop speech action

## Voice behavior

Voice priority defaults to UK/Irish-leaning first (`fable`, `alloy`, `echo`), then others.
Unknown voice names are mapped/fallbacked automatically.

## Extra broker commands (for tray/UI)

- `{"type":"sessions"}` -> returns active session list + summary
- `{"type":"config"}` -> returns current config (`speechSpeed`)
- `{"type":"config","speechSpeed":1.2}` -> updates playback speed

## Microphone-aware behavior

- If the mic becomes active while speech is playing, current playback is interrupted.
- While mic stays active, new speech stays queued (not spoken yet).
- When mic activity ends, queued playback resumes automatically.

Mic activity is detected via `pactl` source state (`RUNNING`) with a short release delay to avoid choppy toggling.
