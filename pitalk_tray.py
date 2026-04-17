#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BROKER_HOST = os.getenv("PITALK_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.getenv("PITALK_BROKER_PORT", "18081"))
REFRESH_SECONDS = 1
SPEND_REFRESH_SECONDS = 300
THEME_CHECK_SECONDS = 3
SPEED_PRESETS = [0.8, 1.0, 1.2, 1.4, 1.6]


def _ensure_tinted_icon(source: Path, target: Path, rgb: str) -> Path:
    """
    Create a monochrome icon from source using source alpha, tinted with rgb color.
    rgb format: "R,G,B" (0-255)
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    try:
        # Keep alpha from source, force RGB to requested color.
        subprocess.run(
            [
                "convert", str(source),
                "-alpha", "on",
                "-fill", f"rgb({rgb})",
                "-colorize", "100",
                str(target),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return target
    except Exception:
        return source


def detect_dark_theme_preference() -> bool:
    """
    Best-effort theme detection.
    Returns True when dark theme is preferred, else False.
    """
    forced = (os.getenv("PITALK_TRAY_THEME") or "").strip().lower()
    if forced in {"dark", "light"}:
        return forced == "dark"

    try:
        color_scheme = subprocess.check_output(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip().lower()
        if "prefer-dark" in color_scheme:
            return True
        if "default" in color_scheme:
            return False
    except Exception:
        pass

    try:
        gtk_theme = subprocess.check_output(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip().strip("\"'").lower()
        if any(token in gtk_theme for token in ["dark", "night", "noir"]):
            return True
    except Exception:
        pass

    return False


def resolve_icon_sets() -> dict[str, tuple[str, str, str]]:
    """
    Returns icon sets for both theme modes:
      - key "dark": used when system theme is dark (icons should be light)
      - key "light": used when system theme is light (icons should be dark)
    """
    env_idle = os.getenv("PITALK_TRAY_ICON_IDLE") or os.getenv("PITALK_TRAY_ICON")
    env_speaking = os.getenv("PITALK_TRAY_ICON_SPEAKING") or env_idle
    env_offline = os.getenv("PITALK_TRAY_ICON_OFFLINE") or env_idle

    if env_idle and Path(env_idle).exists():
        idle = env_idle
        speaking = env_speaking if env_speaking and Path(env_speaking).exists() else env_idle
        offline = env_offline if env_offline and Path(env_offline).exists() else env_idle
        fixed = (idle, speaking, offline)
        return {"dark": fixed, "light": fixed}

    repo_icons = Path("/home/swair/Work/PiTalk/Resources/icons")
    run_icon = repo_icons / "menubar-running.png"
    stop_icon = repo_icons / "menubar-stopped.png"
    app_icon = repo_icons / "app-icon-no-border.png"

    if run_icon.exists() and stop_icon.exists():
        cache_dir = Path.home() / ".cache" / "pitalk-lite" / "icons"

        # For dark themes (dark bar): light icons.
        idle_dark_theme = _ensure_tinted_icon(stop_icon, cache_dir / "menubar-stopped-on-dark.png", "214,220,230")
        speaking_dark_theme = _ensure_tinted_icon(run_icon, cache_dir / "menubar-running-on-dark.png", "244,246,250")
        offline_dark_theme = _ensure_tinted_icon(stop_icon, cache_dir / "menubar-offline-on-dark.png", "160,170,185")

        # For light themes (light bar): dark icons.
        idle_light_theme = _ensure_tinted_icon(stop_icon, cache_dir / "menubar-stopped-on-light.png", "30,36,44")
        speaking_light_theme = _ensure_tinted_icon(run_icon, cache_dir / "menubar-running-on-light.png", "18,22,28")
        offline_light_theme = _ensure_tinted_icon(stop_icon, cache_dir / "menubar-offline-on-light.png", "90,100,115")

        return {
            "dark": (str(idle_dark_theme), str(speaking_dark_theme), str(offline_dark_theme)),
            "light": (str(idle_light_theme), str(speaking_light_theme), str(offline_light_theme)),
        }

    if app_icon.exists():
        fixed = (str(app_icon), str(app_icon), str(app_icon))
        return {"dark": fixed, "light": fixed}

    fixed = ("audio-speakers-symbolic", "audio-volume-high-symbolic", "audio-volume-muted-symbolic")
    return {"dark": fixed, "light": fixed}


def broker_cmd(payload: dict[str, Any], timeout: float = 1.5) -> dict[str, Any]:
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=timeout) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = s.recv(65535).decode("utf-8", errors="ignore").strip()
        return json.loads(data) if data else {"ok": False, "error": "empty response"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def jump_to_pid(pid: int) -> bool:
    if not shutil_which("tmux"):
        return False

    marker = f"πid{pid}"
    try:
        panes = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).splitlines()
    except Exception:
        return False

    for target in panes:
        try:
            captured = subprocess.check_output(
                ["tmux", "capture-pane", "-p", "-S", "-60", "-t", target],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            if marker in captured:
                subprocess.run(["tmux", "select-pane", "-t", target], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["tmux", "switch-client", "-t", target], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        except Exception:
            continue

    return False


def shutil_which(binary: str) -> bool:
    for p in os.getenv("PATH", "").split(os.pathsep):
        c = os.path.join(p, binary)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return True
    return False


def build_session_label(session: dict[str, Any]) -> str:
    pid = session.get("pid")
    project = session.get("project") or session.get("cwd") or f"pid-{pid}"
    status = session.get("status") or "idle"
    queued = int(session.get("queuedCount") or 0)
    speaking = bool(session.get("speaking"))
    icon = "▶" if speaking else "•"
    return f"{icon} {project} (PID {pid}) [{status}] q:{queued}"


def _read_openai_key() -> str | None:
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key.strip()

    env_file = Path.home() / ".env"
    if not env_file.exists():
        return None

    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != "OPENAI_API_KEY":
                continue
            value = value.strip().strip('"').strip("'")
            return value or None
    except Exception:
        return None

    return None


def _sum_cost_values(node: Any) -> float:
    total = 0.0
    if isinstance(node, dict):
        amount = node.get("amount")
        if isinstance(amount, dict):
            v = amount.get("value")
            if isinstance(v, (int, float)):
                total += float(v)
        for v in node.values():
            total += _sum_cost_values(v)
    elif isinstance(node, list):
        for item in node:
            total += _sum_cost_values(item)
    return total


def fetch_openai_spend_24h() -> str | None:
    key = _read_openai_key()
    if not key:
        return None

    now = int(time.time())
    start = now - 24 * 60 * 60
    qs = urllib.parse.urlencode({"start_time": start, "end_time": now, "limit": 31})
    url = f"https://api.openai.com/v1/organization/costs?{qs}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None

    total = _sum_cost_values(data)
    return f"${total:.2f}"


# GTK/AppIndicator imports
try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib  # type: ignore

    Indicator = None
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # type: ignore
        Indicator = AppIndicator
    except Exception:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator  # type: ignore
        Indicator = AppIndicator
except Exception as e:
    raise SystemExit(
        "PyGObject/AppIndicator not available. Install: python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1\n"
        f"Import error: {e}"
    )


class TrayApp:
    def __init__(self) -> None:
        self.icon_sets = resolve_icon_sets()
        self.theme_is_dark = detect_dark_theme_preference()
        self.idle_icon, self.speaking_icon, self.offline_icon = self.icon_sets["dark" if self.theme_is_dark else "light"]
        self.last_theme_check_ts = 0.0

        self.indicator = Indicator.Indicator.new(
            "pitalk-lite",
            self.idle_icon,
            Indicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("PiTalk Lite")

        self.menu = Gtk.Menu()
        self.updating_speed_ui = False
        self.updating_enabled_ui = False
        self.last_summary_label = ""
        self.last_icon = ""
        self.last_sessions_fingerprint = ""
        self.last_speed_value: float | None = None
        self.last_enabled_value: bool | None = None
        self.last_spend_value = "$--"
        self.last_spend_fetch_ts = 0.0
        self.summary_item = Gtk.MenuItem(label="PiTalk Lite")
        self.summary_item.set_sensitive(False)
        self.menu.append(self.summary_item)

        self.spend_item = Gtk.MenuItem(label="$-- (24h)")
        self.spend_item.set_sensitive(False)
        self.menu.append(self.spend_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.speed_item = Gtk.MenuItem(label="Speech speed")
        self.speed_submenu = Gtk.Menu()
        self.speed_radio_items: list[Gtk.RadioMenuItem] = []
        first_radio: Gtk.RadioMenuItem | None = None
        for speed in SPEED_PRESETS:
            if first_radio is None:
                item = Gtk.RadioMenuItem.new_with_label(None, f"{speed:.1f}x")
                first_radio = item
            else:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(first_radio, f"{speed:.1f}x")
            item.connect("activate", self.on_speed_selected, speed)
            self.speed_submenu.append(item)
            self.speed_radio_items.append(item)
        self.speed_item.set_submenu(self.speed_submenu)
        self.menu.append(self.speed_item)

        self.enabled_item = Gtk.CheckMenuItem(label="Voice output enabled")
        self.enabled_item.connect("toggled", self.on_enabled_toggled)
        self.menu.append(self.enabled_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.sessions_header = Gtk.MenuItem(label="Sessions")
        self.sessions_header.set_sensitive(False)
        self.menu.append(self.sessions_header)

        self.dynamic_session_items: list[Gtk.MenuItem] = []

        self.refresh_item = Gtk.MenuItem(label="Refresh now")
        self.refresh_item.connect("activate", self.on_refresh)
        self.menu.append(self.refresh_item)

        self.stop_item = Gtk.MenuItem(label="Stop speech")
        self.stop_item.connect("activate", self.on_stop)
        self.menu.append(self.stop_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.quit_item = Gtk.MenuItem(label="Quit (stop all)")
        self.quit_item.connect("activate", self.on_quit)
        self.menu.append(self.quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def _clear_session_items(self) -> None:
        for item in self.dynamic_session_items:
            self.menu.remove(item)
        self.dynamic_session_items.clear()

    def _insert_session_item(self, label: str, pid: int | None = None) -> None:
        item = Gtk.MenuItem(label=label)
        if pid is not None:
            item.connect("activate", self.on_jump, pid)
        else:
            item.set_sensitive(False)

        # insert before refresh item
        idx = self.menu.get_children().index(self.refresh_item)
        self.menu.insert(item, idx)
        item.show()
        self.dynamic_session_items.append(item)

    def _set_summary_once(self, label: str) -> None:
        if label != self.last_summary_label:
            self.summary_item.set_label(label)
            self.last_summary_label = label

    def _set_icon_once(self, icon: str, desc: str) -> None:
        if icon != self.last_icon:
            self.indicator.set_icon_full(icon, desc)
            self.last_icon = icon

    def _sessions_fingerprint(self, sessions: list[dict[str, Any]]) -> str:
        # Only fields that affect visible labels; ignore timestamps to avoid redraw flicker.
        slim = [
            {
                "pid": s.get("pid"),
                "project": s.get("project"),
                "cwd": s.get("cwd"),
                "status": s.get("status"),
                "queuedCount": s.get("queuedCount"),
                "speaking": s.get("speaking"),
            }
            for s in sessions
        ]
        return json.dumps(slim, sort_keys=True, separators=(",", ":"))

    def _refresh_spend(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_spend_fetch_ts) < SPEND_REFRESH_SECONDS:
            return

        value = fetch_openai_spend_24h()
        if value:
            self.last_spend_value = value

        self.spend_item.set_label(f"{self.last_spend_value} (24h)")
        self.last_spend_fetch_ts = now

    def _maybe_update_theme_icons(self) -> None:
        now = time.time()
        if (now - self.last_theme_check_ts) < THEME_CHECK_SECONDS:
            return
        self.last_theme_check_ts = now

        dark = detect_dark_theme_preference()
        if dark == self.theme_is_dark:
            return

        self.theme_is_dark = dark
        self.idle_icon, self.speaking_icon, self.offline_icon = self.icon_sets["dark" if dark else "light"]
        self.last_icon = ""  # force icon refresh on next status render

    def refresh(self, force_spend: bool = False) -> None:
        self._maybe_update_theme_icons()
        self._refresh_spend(force=force_spend)

        sessions_res = broker_cmd({"type": "sessions"})
        config_res = broker_cmd({"type": "config"})

        if not sessions_res.get("ok"):
            self._set_summary_once(f"PiTalk Lite: offline ({sessions_res.get('error', 'error')})")
            self._set_icon_once(self.offline_icon, "PiTalk offline")
            if self.last_sessions_fingerprint != "__offline__":
                self._clear_session_items()
                self._insert_session_item("No sessions")
                self.last_sessions_fingerprint = "__offline__"
            return

        sessions = sessions_res.get("sessions", [])
        speaking = int(sessions_res.get("summary", {}).get("speaking", 0))
        total = int(sessions_res.get("summary", {}).get("total", 0))
        mic_active = bool(sessions_res.get("micActive"))
        speed = float(config_res.get("speechSpeed", 1.0)) if config_res.get("ok") else 1.0
        enabled = bool(config_res.get("speechEnabled", True)) if config_res.get("ok") else True

        status = "speaking" if speaking > 0 else "idle"
        if not enabled:
            status = "voice off"
        elif mic_active:
            status = "mic active"

        self._set_summary_once(f"PiTalk Lite: {total} sessions, {status}, speed {speed:.1f}x")
        self._set_icon_once(
            self.speaking_icon if speaking > 0 else self.idle_icon,
            f"PiTalk {status}",
        )

        # update speed checks only when speed actually changes
        if self.last_speed_value is None or abs(self.last_speed_value - speed) > 0.01:
            self.updating_speed_ui = True
            for item, preset in zip(self.speed_radio_items, SPEED_PRESETS):
                item.set_active(abs(speed - preset) < 0.05)
            self.updating_speed_ui = False
            self.last_speed_value = speed

        if self.last_enabled_value is None or self.last_enabled_value != enabled:
            self.updating_enabled_ui = True
            self.enabled_item.set_active(enabled)
            self.updating_enabled_ui = False
            self.last_enabled_value = enabled

        fp = self._sessions_fingerprint(sessions)
        if fp != self.last_sessions_fingerprint:
            self._clear_session_items()
            if not sessions:
                self._insert_session_item("No active sessions")
            else:
                for s in sessions:
                    pid = s.get("pid")
                    self._insert_session_item(build_session_label(s), pid if isinstance(pid, int) else None)
            self.last_sessions_fingerprint = fp

    def _tick(self) -> bool:
        self.refresh()
        return True

    def on_refresh(self, _item: Gtk.MenuItem) -> None:
        self.refresh(force_spend=True)

    def on_stop(self, _item: Gtk.MenuItem) -> None:
        broker_cmd({"type": "stopCurrent"})
        self.refresh()

    def on_speed_selected(self, item: Gtk.RadioMenuItem, speed: float) -> None:
        if self.updating_speed_ui or not item.get_active():
            return
        broker_cmd({"type": "config", "speechSpeed": speed})
        self.refresh()

    def on_enabled_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if self.updating_enabled_ui:
            return
        broker_cmd({"type": "config", "speechEnabled": item.get_active()})
        self.refresh()

    def on_jump(self, _item: Gtk.MenuItem, pid: int) -> None:
        ok = jump_to_pid(pid)
        if not ok:
            print(f"[pitalk-tray] Could not jump to PID {pid} (tmux marker not found)")

    def on_quit(self, _item: Gtk.MenuItem) -> None:
        # "Quit" should mean fully off: stop speech and stop broker service.
        broker_cmd({"type": "stop"})

        # If running via systemd user services, stop both units.
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "pitalk-lite-broker.service", "pitalk-lite-tray.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        # Fallback for manual runs / non-systemd scenarios.
        try:
            subprocess.run(
                ["pkill", "-f", "/home/swair/Work/pi-talk-lite/linux_tts_broker.py"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        Gtk.main_quit()


def main() -> None:
    TrayApp()
    Gtk.main()


if __name__ == "__main__":
    main()
