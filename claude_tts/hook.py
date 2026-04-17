#!/usr/bin/env python3
"""Claude Code hook entry point for TTS.

Dispatched by hook_event_name:
  - SessionStart       -> spawn transcript tailer, inject voice prompt
  - UserPromptSubmit   -> stop any in-flight speech for this session
  - SessionEnd         -> kill tailer, stop speech

Stop is intentionally a no-op — the tailer lives across turns.
"""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18081
PID_DIR = Path("/tmp")
SCRIPT_DIR = Path(__file__).resolve().parent
TAILER = SCRIPT_DIR / "tailer.py"
VOICE_PROMPT_FILE = SCRIPT_DIR / "voice_prompt.md"
STYLE_STATE_FILE = Path.home() / ".claude" / "pitalk_voice_style"
VALID_STYLES = ("succinct", "verbose", "chatty")
DEFAULT_STYLE = "succinct"


def read_active_style():
    try:
        value = STYLE_STATE_FILE.read_text().strip().lower()
    except OSError:
        return DEFAULT_STYLE
    return value if value in VALID_STYLES else DEFAULT_STYLE


def pidfile_path(session_id):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "session"
    return PID_DIR / f"claude-tts-{safe}.pid"


def kill_tailer(session_id):
    pf = pidfile_path(session_id)
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        pass
    try:
        pf.unlink()
    except FileNotFoundError:
        pass


def start_tailer(session_id, transcript_path):
    kill_tailer(session_id)
    pf = pidfile_path(session_id)
    log_path = PID_DIR / f"claude-tts-{session_id[:16]}.log"
    log_fh = open(log_path, "ab")
    proc = subprocess.Popen(
        [
            sys.executable, str(TAILER),
            "--transcript", transcript_path,
            "--session-id", session_id,
            "--pidfile", str(pf),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    pf.write_text(str(proc.pid))


def broker_send(payload, timeout=1.0):
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=timeout) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            try:
                s.recv(4096)
            except OSError:
                pass
    except OSError:
        pass


def handle_session_start(payload):
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    if session_id and transcript_path:
        try:
            start_tailer(session_id, transcript_path)
        except OSError:
            pass

    try:
        prompt = VOICE_PROMPT_FILE.read_text()
    except OSError:
        prompt = ""
    if prompt.strip():
        active = read_active_style()
        prompt = f"{prompt.rstrip()}\n\n**Active style: {active.upper()}**\n"
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": prompt,
            }
        }
        sys.stdout.write(json.dumps(out))


def handle_user_prompt_submit(payload):
    session_id = payload.get("session_id") or ""
    if session_id:
        broker_send({"type": "stop", "sourceApp": "claude", "sessionId": session_id})


def handle_session_end(payload):
    session_id = payload.get("session_id") or ""
    if session_id:
        kill_tailer(session_id)
        broker_send({"type": "stop", "sourceApp": "claude", "sessionId": session_id})


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    event = payload.get("hook_event_name") or ""
    if event == "SessionStart":
        handle_session_start(payload)
    elif event == "UserPromptSubmit":
        handle_user_prompt_submit(payload)
    elif event == "SessionEnd":
        handle_session_end(payload)


if __name__ == "__main__":
    main()
