#!/usr/bin/env python3
import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18081
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 18080

OPENAI_API_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1")
MIC_POLL_INTERVAL = 0.25
MIC_RELEASE_DELAY = 0.8
AGENT_STATUS_STALE_SECONDS = 5 * 60


VOICE_PRIORITY = ["fable", "nova", "shimmer", "alloy"]

VOICE_MAP = {
    # PiTalk local-style voices
    "alba": "fable",
    "fantine": "nova",
    "cosette": "shimmer",
    "marius": "onyx",
    "eponine": "echo",
    "azelma": "alloy",
    "javert": "onyx",
    # PiTalk cloud aliases
    "ally": "fable",
    "dorothy": "alloy",
    "lily": "echo",
    "alice": "nova",
    "dave": "onyx",
    "joseph": "onyx",
    # direct OpenAI voices
    "alloy": "alloy",
    "echo": "echo",
    "fable": "fable",
    "onyx": "onyx",
    "nova": "nova",
    "shimmer": "shimmer",
    "auto": "auto",
}


def load_openai_key() -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key.strip()

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


@dataclass
class Job:
    text: str
    voice: Optional[str]
    source_app: Optional[str]
    session_id: Optional[str]
    pid: Optional[int]
    queue_key: str


@dataclass
class AgentStatus:
    pid: int
    project: Optional[str]
    cwd: Optional[str]
    status: str
    detail: Optional[str]
    context_percent: Optional[int]
    updated_at: float


class BrokerState:
    def __init__(self):
        self.pending: list[Job] = []
        self.condition = asyncio.Condition()
        self.current_job: Optional[Job] = None
        self.current_proc: Optional[asyncio.subprocess.Process] = None
        self.stop_requested_current = False
        self.is_mic_active = False
        self.voice_by_queue: dict[str, str] = {}
        self.voice_cycle_idx = 0
        self.agent_statuses: dict[int, AgentStatus] = {}
        self.speech_speed = self._initial_speech_speed()

    def _initial_speech_speed(self) -> float:
        raw = os.getenv("PITALK_SPEECH_SPEED", "1.0")
        try:
            return self.clamp_speed(float(raw))
        except Exception:
            return 1.0

    @staticmethod
    def clamp_speed(value: float) -> float:
        return max(0.7, min(2.0, round(value, 2)))

    def queue_key(self, source_app: Optional[str], session_id: Optional[str]) -> str:
        app = (source_app or "unknown").strip() or "unknown"
        sid = (session_id or "__none__").strip() or "__none__"
        return f"{app}::{sid}"

    def resolve_voice(self, requested: Optional[str], queue_key: str) -> str:
        req = (requested or "").strip().lower()
        mapped = VOICE_MAP.get(req)
        if mapped and mapped != "auto":
            return mapped

        if queue_key in self.voice_by_queue:
            return self.voice_by_queue[queue_key]

        voice = VOICE_PRIORITY[self.voice_cycle_idx % len(VOICE_PRIORITY)]
        self.voice_cycle_idx += 1
        self.voice_by_queue[queue_key] = voice
        return voice

    def state(self) -> dict:
        return {
            "pending": len(self.pending) + (1 if self.current_job else 0),
            "playing": self.current_job is not None,
            "currentQueue": self.current_job.queue_key if self.current_job else None,
            "micActive": self.is_mic_active,
            "speechSpeed": self.speech_speed,
        }

    def queue_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self.pending:
            counts[job.queue_key] = counts.get(job.queue_key, 0) + 1
        if self.current_job:
            counts[self.current_job.queue_key] = counts.get(self.current_job.queue_key, 0) + 1
        return counts

    def upsert_status(self, req: dict) -> None:
        pid = req.get("pid")
        if not isinstance(pid, int):
            return

        status = str(req.get("status") or "unknown")
        if status == "remove":
            self.agent_statuses.pop(pid, None)
            return

        self.agent_statuses[pid] = AgentStatus(
            pid=pid,
            project=req.get("project"),
            cwd=req.get("cwd"),
            status=status,
            detail=req.get("detail"),
            context_percent=req.get("contextPercent") if isinstance(req.get("contextPercent"), int) else None,
            updated_at=time.time(),
        )

    def active_sessions(self) -> list[dict]:
        now = time.time()
        # cleanup stale statuses
        stale = [pid for pid, s in self.agent_statuses.items() if (now - s.updated_at) > AGENT_STATUS_STALE_SECONDS]
        for pid in stale:
            self.agent_statuses.pop(pid, None)

        sessions: list[dict] = []
        for s in self.agent_statuses.values():
            pending_for_pid = sum(1 for j in self.pending if j.pid == s.pid)
            speaking = bool(self.current_job and self.current_job.pid == s.pid)
            queued_count = pending_for_pid + (1 if speaking else 0)
            queue_key = self.current_job.queue_key if speaking and self.current_job else self.queue_key("pi", str(s.pid))

            sessions.append({
                "pid": s.pid,
                "project": s.project,
                "cwd": s.cwd,
                "status": s.status,
                "detail": s.detail,
                "contextPercent": s.context_percent,
                "updatedAt": int(s.updated_at * 1000),
                "queuedCount": queued_count,
                "speaking": speaking,
                "queueKey": queue_key,
            })

        sessions.sort(key=lambda x: (not x["speaking"], -(x["updatedAt"] or 0), x["pid"]))
        return sessions


state = BrokerState()
OPENAI_API_KEY = load_openai_key()


def json_line(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def detect_mic_active() -> bool:
    """
    Detect microphone activity using PulseAudio/PipeWire via pactl.
    Returns True when default input source is RUNNING.
    """
    if shutil.which("pactl") is None:
        return False

    default_source = None
    try:
        default_source = subprocess.check_output(
            ["pactl", "get-default-source"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip() or None
    except Exception:
        pass

    try:
        output = subprocess.check_output(
            ["pactl", "list", "short", "sources"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return False

    any_running = False
    for line in output.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        name = cols[1]
        state_col = cols[-1].strip().upper() if cols else ""
        is_running = state_col == "RUNNING"
        if is_running:
            any_running = True
        if default_source and name == default_source:
            return is_running

    return any_running


async def set_mic_active(active: bool) -> None:
    async with state.condition:
        if state.is_mic_active == active:
            return

        state.is_mic_active = active

        if active:
            # If we're currently speaking, interrupt immediately.
            if state.current_job is not None:
                state.stop_requested_current = True
            if state.current_proc and state.current_proc.returncode is None:
                interrupt_process(state.current_proc)
            print("[pitalk-lite] mic ACTIVE -> paused playback / interrupted current stream")
        else:
            print("[pitalk-lite] mic INACTIVE -> resuming queued playback")

        state.condition.notify_all()


async def microphone_monitor() -> None:
    keep_active_until = 0.0
    active = False

    while True:
        in_use = await asyncio.to_thread(detect_mic_active)
        now = time.monotonic()

        if in_use:
            keep_active_until = now + MIC_RELEASE_DELAY
            if not active:
                active = True
                await set_mic_active(True)
        else:
            if active and now >= keep_active_until:
                active = False
                await set_mic_active(False)

        await asyncio.sleep(MIC_POLL_INTERVAL)


def interrupt_process(proc: Optional[asyncio.subprocess.Process]) -> None:
    if not proc or proc.returncode is not None:
        return
    try:
        # start_new_session=True creates a new process group with leader PID.
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


async def stream_openai_to_ffplay(text: str, voice: str, speech_speed: float) -> asyncio.subprocess.Process:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not found (env or ~/.env)")

    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": voice,
        "input": text,
    }

    with tempfile.NamedTemporaryFile(prefix="pitalk-lite-body-", suffix=".json", delete=False) as body_file:
        body_file.write(json.dumps(payload).encode("utf-8"))
        body_path = body_file.name

    atempo = BrokerState.clamp_speed(speech_speed)

    # Use curl+ffplay pipeline for low-latency streaming playback.
    cmd = (
        f"curl -sS --fail-with-body -X POST {shlex.quote(OPENAI_API_URL)} "
        f"-H {shlex.quote('Authorization: Bearer ' + OPENAI_API_KEY)} "
        f"-H {shlex.quote('Content-Type: application/json')} "
        f"--data-binary @{shlex.quote(body_path)} "
        f"| ffplay -nodisp -autoexit -loglevel error -af atempo={atempo:.2f} -i pipe:0"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    async def _cleanup_body_file() -> None:
        # Give curl a moment to consume file before cleanup.
        await asyncio.sleep(3)
        try:
            Path(body_path).unlink(missing_ok=True)
        except Exception:
            pass

    asyncio.create_task(_cleanup_body_file())
    return proc


async def playback_worker() -> None:
    while True:
        async with state.condition:
            while not state.pending or state.is_mic_active:
                await state.condition.wait()
            job = state.pending.pop(0)
            state.current_job = job
            state.stop_requested_current = False

        try:
            voice = state.resolve_voice(job.voice, job.queue_key)

            if state.stop_requested_current:
                continue

            proc = await stream_openai_to_ffplay(job.text, voice, state.speech_speed)
            state.current_proc = proc
            await proc.wait()

            if proc.returncode not in (0, -signal.SIGTERM, 143):
                print(f"[pitalk-lite] streaming pipeline exited with code {proc.returncode}")
        except FileNotFoundError:
            print("[pitalk-lite] curl/ffplay not found. Install curl and ffmpeg.")
        except Exception as e:
            print(f"[pitalk-lite] playback error: {e}")
        finally:
            async with state.condition:
                state.current_proc = None
                state.current_job = None
                state.stop_requested_current = False
                state.condition.notify_all()


def job_matches(job: Job, source_app: Optional[str], session_id: Optional[str]) -> bool:
    if source_app is None:
        return True
    if (job.source_app or "unknown") != source_app:
        return False
    if session_id is None:
        return True
    return (job.session_id or "__none__") == session_id


async def handle_broker_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await reader.readline()
        if not raw:
            writer.write(json_line({"ok": False, "error": "Empty request"}))
            await writer.drain()
            return

        try:
            req = json.loads(raw.decode("utf-8", errors="ignore").strip())
        except Exception:
            writer.write(json_line({"ok": False, "error": "Invalid JSON request"}))
            await writer.drain()
            return

        cmd = req.get("type")

        if cmd == "health":
            res = {"ok": True, **state.state()}
            writer.write(json_line(res))
            await writer.drain()
            return

        if cmd == "status":
            async with state.condition:
                state.upsert_status(req)
            writer.write(json_line({"ok": True}))
            await writer.drain()
            return

        if cmd == "sessions":
            async with state.condition:
                sessions = state.active_sessions()
                summary = {
                    "total": len(sessions),
                    "speaking": sum(1 for s in sessions if s.get("speaking")),
                    "queued": sum(1 for s in sessions if (s.get("queuedCount") or 0) > 0),
                }
            writer.write(json_line({"ok": True, "sessions": sessions, "summary": summary, **state.state()}))
            await writer.drain()
            return

        if cmd == "config":
            async with state.condition:
                raw_speed = req.get("speechSpeed")
                if isinstance(raw_speed, (int, float)):
                    state.speech_speed = BrokerState.clamp_speed(float(raw_speed))
                payload = {"ok": True, "speechSpeed": state.speech_speed}
            writer.write(json_line(payload))
            await writer.drain()
            return

        if cmd == "speak":
            text = (req.get("text") or "").strip()
            if not text:
                writer.write(json_line({"ok": False, "error": "Missing text"}))
                await writer.drain()
                return

            source_app = req.get("sourceApp")
            session_id = req.get("sessionId")
            queue_key = state.queue_key(source_app, session_id)
            job = Job(
                text=text,
                voice=req.get("voice"),
                source_app=source_app,
                session_id=session_id,
                pid=req.get("pid"),
                queue_key=queue_key,
            )

            async with state.condition:
                state.pending.append(job)
                queued = len(state.pending) + (1 if state.current_job else 0)
                state.condition.notify()

            writer.write(json_line({"ok": True, "queued": queued}))
            await writer.drain()
            return

        if cmd == "stop":
            source_app = req.get("sourceApp")
            session_id = req.get("sessionId")

            async with state.condition:
                state.pending = [
                    j for j in state.pending
                    if not job_matches(j, source_app, session_id)
                ]

                if state.current_job and job_matches(state.current_job, source_app, session_id):
                    state.stop_requested_current = True
                    if state.current_proc and state.current_proc.returncode is None:
                        interrupt_process(state.current_proc)

                state.condition.notify_all()

            writer.write(json_line({"ok": True, **state.state()}))
            await writer.drain()
            return

        writer.write(json_line({"ok": False, "error": f"Unknown command: {cmd}"}))
        await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_health_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        req_line = await reader.readline()
        _ = req_line  # not needed for simple health endpoint
        # consume headers
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        body = json.dumps({"ok": True, "service": "PiTalk-Lite"}).encode("utf-8")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("utf-8")
            + b"Connection: close\r\n\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    if not OPENAI_API_KEY:
        print("[pitalk-lite] WARNING: OPENAI_API_KEY not found (env or ~/.env). speak will fail.")

    broker_server = await asyncio.start_server(handle_broker_client, BROKER_HOST, BROKER_PORT)
    health_server = await asyncio.start_server(handle_health_client, HEALTH_HOST, HEALTH_PORT)

    worker_task = asyncio.create_task(playback_worker())
    mic_task = asyncio.create_task(microphone_monitor())

    print(f"[pitalk-lite] broker listening on {BROKER_HOST}:{BROKER_PORT}")
    print(f"[pitalk-lite] health listening on {HEALTH_HOST}:{HEALTH_PORT}")
    print(f"[pitalk-lite] model={OPENAI_TTS_MODEL}")
    print("[pitalk-lite] mic monitor enabled (auto-pause + interrupt)")

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await stop_event.wait()

    broker_server.close()
    health_server.close()
    await broker_server.wait_closed()
    await health_server.wait_closed()

    for task in (worker_task, mic_task):
        task.cancel()
    for task in (worker_task, mic_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
