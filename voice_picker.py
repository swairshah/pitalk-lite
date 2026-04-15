#!/usr/bin/env python3
"""
Pick an OpenAI TTS voice by auditioning multiple voices.

Usage:
  python voice_picker.py
  python voice_picker.py --model tts-1-hd
  python voice_picker.py --auto
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

# OpenAI voices don't expose strict locale metadata, so we prioritize voices
# that tend to sound closer to UK/Irish style first, then US-leaning ones.
VOICE_PRIORITY = ["fable", "alloy", "echo", "nova", "onyx", "shimmer"]
DEFAULT_VOICES = VOICE_PRIORITY

TEST_TEXT = (
    "Speak in a natural British or Irish English accent. "
    "Hello, this is a voice preview for my Pi assistant on Linux. "
    "I can explain code, summarize changes, and read responses clearly."
)


def load_openai_key() -> str | None:
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()

    env_file = Path.home() / ".env"
    if not env_file.exists():
        return None

    for line in env_file.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == "OPENAI_API_KEY":
            return v.strip().strip('"').strip("'")
    return None


def synthesize(key: str, model: str, voice: str, text: str) -> bytes:
    payload = {"model": model, "voice": voice, "input": text}
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def play_file(path: Path) -> None:
    players = [
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)],
        ["mpg123", str(path)],
    ]
    for cmd in players:
        if not shutil_which(cmd[0]):
            continue
        subprocess.run(cmd, check=False)
        return
    print(f"No player found. Play manually: {path}")


def shutil_which(binary: str) -> bool:
    for p in os.getenv("PATH", "").split(os.pathsep):
        candidate = Path(p) / binary
        if candidate.exists() and os.access(candidate, os.X_OK):
            return True
    return False


def heuristic_voice_order(voices: list[str]) -> list[str]:
    # Very rough fallback order when running --auto without listening.
    rank = {v: i for i, v in enumerate(VOICE_PRIORITY)}
    return sorted(voices, key=lambda v: rank.get(v, 999))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tts-1", choices=["tts-1", "tts-1-hd"])
    parser.add_argument("--voices", default=",".join(DEFAULT_VOICES), help="comma-separated")
    parser.add_argument("--auto", action="store_true", help="pick by heuristic without scoring prompt")
    parser.add_argument("--text", default=TEST_TEXT)
    args = parser.parse_args()

    key = load_openai_key()
    if not key:
        print("OPENAI_API_KEY not found in environment or ~/.env", file=sys.stderr)
        return 1

    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    out_dir = Path("/tmp/openai-voice-test")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating previews with model={args.model} ...")
    generated: list[tuple[str, Path]] = []
    for voice in voices:
        try:
            audio = synthesize(key, args.model, voice, args.text)
            out_file = out_dir / f"{voice}.mp3"
            out_file.write_bytes(audio)
            generated.append((voice, out_file))
            print(f"  ✓ {voice} -> {out_file}")
        except Exception as e:
            print(f"  ✗ {voice} failed: {e}")

    if not generated:
        print("No voice previews generated.", file=sys.stderr)
        return 2

    if args.auto:
        pick = heuristic_voice_order([v for v, _ in generated])[0]
        print(f"\nHeuristic pick: {pick}")
        print(f"File: {out_dir / (pick + '.mp3')}")
        return 0

    print("\nScoring mode: listen and rate voice preference (1-5).")
    scores: dict[str, int] = {}
    for voice, file in generated:
        print(f"\n--- {voice} ---")
        play_file(file)
        while True:
            val = input(f"Rate '{voice}' [1-5, enter=3]: ").strip() or "3"
            if val in {"1", "2", "3", "4", "5"}:
                scores[voice] = int(val)
                break
            print("Please enter 1,2,3,4,5")

    best = max(scores.items(), key=lambda x: x[1])[0]
    print("\nScores:")
    for voice, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        print(f"  {voice:8} {score}")

    print(f"\nRecommended voice: {best}")
    print(f"Use this in your app config as the default voice.")
    print(f"Preview files in: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
