#!/usr/bin/env python3
"""Tail a Claude Code transcript JSONL, extract <voice> tags, forward to broker.

Claude Code writes one JSON line per agent-loop iteration (each model response),
which lets us speak chunks during tool-use loops — not only at turn end.
"""

import argparse
import json
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18081
IDLE_EXIT_SECONDS = 30 * 60
POLL_INTERVAL = 0.25

VOICE_RE = re.compile(r"<voice>(.*?)</voice>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def sanitize(text):
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def chunk_sentences(text):
    if not text.strip():
        return []
    parts = SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def send_broker(payload, timeout=2.0):
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=timeout) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            deadline = time.monotonic() + timeout
            while b"\n" not in buf and time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return


def extract_assistant_text(line_json):
    if line_json.get("type") != "assistant":
        return ""
    msg = line_json.get("message") or {}
    if msg.get("role") != "assistant":
        return ""
    parts = []
    for c in msg.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text")
            if t:
                parts.append(t)
    return "\n".join(parts)


def enqueue_voice_content(text, session_id):
    if not text:
        return
    for match in VOICE_RE.finditer(text):
        content = sanitize(match.group(1))
        if not content:
            continue
        for sentence in chunk_sentences(content):
            send_broker({
                "type": "speak",
                "text": sentence,
                "sourceApp": "claude",
                "sessionId": session_id,
                "pid": os.getpid(),
            })


def tail(path, session_id):
    while not path.exists():
        time.sleep(POLL_INTERVAL)

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        buffer = ""
        last_activity = time.monotonic()
        while True:
            chunk = f.read()
            if chunk:
                buffer += chunk
                last_activity = time.monotonic()
                while "\n" in buffer:
                    line, _, buffer = buffer.partition("\n")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = extract_assistant_text(data)
                    if text:
                        enqueue_voice_content(text, session_id)
            else:
                if time.monotonic() - last_activity > IDLE_EXIT_SECONDS:
                    return
                time.sleep(POLL_INTERVAL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--pidfile")
    args = ap.parse_args()

    def cleanup(*_):
        if args.pidfile:
            try:
                Path(args.pidfile).unlink()
            except FileNotFoundError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        tail(Path(args.transcript), args.session_id)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
