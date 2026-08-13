#!/usr/bin/env python3
"""
Vector Status Widget — floating stats when Vector is online.

Shows a compact, draggable gauge (like PC Speedometer) only while Vector is
reachable via wire-pod-reported IP + engine stats (HTTP :8888).

  • Appears when Vector is connected / stats reachable
  • Disappears when Vector goes offline
  • Left-click  = cycle meter: BAT → TMP → VOLT
  • Drag        = move
  • Right-click = menu
  • Tray icon always present (green online / grey offline)
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("GtkLayerShell", "0.1")

from gi.repository import Gtk, GLib, Gdk, GdkPixbuf  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import GtkLayerShell  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POLL_ONLINE_MS = 2000
POLL_OFFLINE_MS = 4000
CONNECT_TIMEOUT = 1.2

# Wider HUD: Vector face on the left + horizontal bar on the right
GAUGE_H = 64
GAUGE_W = 148
ICON_SIZE = 28
DRAG_THRESHOLD = 4

ICON_DIR = os.path.join(tempfile.gettempdir(), "vector-status-icons")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "vector-status")
POSITION_FILE = os.path.join(CONFIG_DIR, "position.txt")
MODE_FILE = os.path.join(CONFIG_DIR, "mode.txt")
IP_OVERRIDE_FILE = os.path.join(CONFIG_DIR, "ip.txt")
FACE_OVERRIDE_FILE = os.path.join(CONFIG_DIR, "face.png")  # optional user override
APP_ID = "vector-status"
TITLE = "Vector Status"

# Real Vector photo assets (shipped next to this script)
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FACE_ONLINE = os.path.join(_ASSET_DIR, "vector-face.png")
FACE_OFFLINE = os.path.join(_ASSET_DIR, "vector-face-offline.png")
FACE_EYES = os.path.join(_ASSET_DIR, "vector-eyes.png")  # alt: wire-pod face

BOT_SDK_INFO = "/etc/wire-pod/wire-pod/jdocs/botSdkInfo.json"
SDK_CONFIG = os.path.join(os.path.expanduser("~"), ".anki_vector", "sdk_config.ini")

MODES = ("BAT", "TMP", "VOLT", "CPU", "LOAD")

# Engine stats mask — same as Vector /root/vs
ENG_MASK = "11111100000000000000000000000000000000000000"

# SSH into Vector for CPU / load (optional). Key is never shipped — users
# supply their own unlock key via env or a standard path.
SSH_USER = "root"
SSH_TIMEOUT = 2.0
SSH_KEY_ENV = "VECTOR_SSH_KEY"


def resolve_ssh_key() -> str | None:
    """Locate a Vector root SSH key without hardcoding a personal path."""
    env = (os.environ.get(SSH_KEY_ENV) or "").strip()
    if env and os.path.isfile(env):
        return env
    for candidate in (
        os.path.join(CONFIG_DIR, "ssh_root_key"),
        os.path.expanduser("~/.anki_vector/ssh_root_key"),
        os.path.expanduser("~/.ssh/id_rsa_vector"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None

# One remote script: BusyBox-safe, key=value lines
SSH_REMOTE = (
    "grep '^cpu ' /proc/stat; "
    "awk '{print \"LOAD\",$1}' /proc/loadavg; "
    "echo CORES $(grep -c ^processor /proc/cpuinfo); "
    "F=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq 2>/dev/null || echo 0); "
    "echo MHZ $((F/1000)); "
    "free -m | awk '/^Mem:/ {print \"MEM\",$3,$2}'"
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_position():
    try:
        with open(POSITION_FILE, "r", encoding="utf-8") as f:
            parts = f.read().strip().split(",")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except (OSError, ValueError):
        pass
    return None


def save_position(x, y):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(POSITION_FILE, "w", encoding="utf-8") as f:
            f.write(f"{int(x)},{int(y)}\n")
    except OSError as exc:
        print(f"Could not save position: {exc}", file=sys.stderr)


def load_mode():
    try:
        with open(MODE_FILE, "r", encoding="utf-8") as f:
            mode = f.read().strip().upper()
            if mode in MODES:
                return mode
    except OSError:
        pass
    return "BAT"


def save_mode(mode):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(MODE_FILE, "w", encoding="utf-8") as f:
            f.write(f"{mode}\n")
    except OSError as exc:
        print(f"Could not save mode: {exc}", file=sys.stderr)


def default_position():
    display = Gdk.Display.get_default()
    if display is None or display.get_n_monitors() < 1:
        return 100, 100
    mon = display.get_monitor(0)
    geo = mon.get_geometry()
    # Sit near PC speedometer (left of bottom-right)
    x = geo.x + geo.width - GAUGE_W - 470
    y = geo.y + geo.height - GAUGE_H - 4
    return max(geo.x, x), max(geo.y, y)


def clamp_position(x, y):
    display = Gdk.Display.get_default()
    if display is None or display.get_n_monitors() < 1:
        return max(0, x), max(0, y)
    min_x = min_y = 10**9
    max_x = max_y = -10**9
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        min_x = min(min_x, g.x)
        min_y = min(min_y, g.y)
        max_x = max(max_x, g.x + g.width)
        max_y = max(max_y, g.y + g.height)
    x = max(min_x, min(x, max_x - GAUGE_W))
    y = max(min_y, min(y, max_y - GAUGE_H))
    return int(x), int(y)


# ---------------------------------------------------------------------------
# Vector discovery + stats
# ---------------------------------------------------------------------------
def discover_ip() -> str | None:
    """Prefer override, then wire-pod botSdkInfo, then SDK config."""
    try:
        with open(IP_OVERRIDE_FILE, "r", encoding="utf-8") as f:
            ip = f.read().strip()
            if ip:
                return ip
    except OSError:
        pass

    try:
        with open(BOT_SDK_INFO, "r", encoding="utf-8") as f:
            data = json.load(f)
        robots = data.get("robots") or []
        if robots:
            ip = (robots[0].get("ip_address") or "").strip()
            if ip:
                return ip
    except (OSError, json.JSONDecodeError, IndexError, TypeError):
        pass

    try:
        import configparser

        cfg = configparser.ConfigParser()
        cfg.read(SDK_CONFIG)
        for section in cfg.sections():
            ip = cfg.get(section, "ip", fallback="").strip()
            if ip:
                return ip
    except Exception:  # noqa: BLE001
        pass
    return None


def tcp_open(ip: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# After reboot Vector often reports 0.000 V while level is still Full/Low.
# Remember last good voltage so the widget doesn't jump to 0%.
_last_good_volts: float | None = None

# Map engine "level" string / SDK enum names → approx %
LEVEL_PERCENT = {
    "full": 100,
    "nominal": 70,
    "low": 25,
    "critical": 8,
    "unknown": 50,
}


def voltage_to_percent(v: float) -> int | None:
    """Match /root/vs and wire-pod-style curve. None if voltage unusable."""
    if v is None or v < 2.5:
        # Real Vector pack is ~3.5–4.2 V; 0.0 after reboot is not real
        return None
    maxv, midv, minv = 4.1, 3.85, 3.5
    if v >= maxv:
        return 100
    if v >= midv:
        s = (v - midv) / (maxv - midv)
        p = 80 + 20 * (math.log(1 + s * 9) / math.log(10))
        return int(max(0, min(100, p + 0.5)))
    if v >= minv:
        s = (v - minv) / (midv - minv)
        p = 80 * (math.log(1 + s * 9) / math.log(10))
        return int(max(0, min(100, p + 0.5)))
    # Below 3.5 but above 2.5 — nearly empty
    return max(0, int((v - 2.5) / (minv - 2.5) * 15))


def level_to_percent(level: str) -> int:
    key = (level or "").strip().lower()
    return LEVEL_PERCENT.get(key, 50)


def fetch_engine_stats(ip: str) -> dict | None:
    """
    Pull battery/temp from vic-engine webviz.
    Lines (with mask): filtered V, raw V, charger V, level, temp C, charging, on_charger, ...

    Note: after a Vector reboot, volts often stay 0.000 for a while even when
    level says Full — SDK reports the same. We fall back to level / last good V.
    """
    global _last_good_volts

    url = f"http://{ip}:8888/getenginestats?{ENG_MASK}"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    lines = [ln.strip() for ln in body.splitlines() if ln.strip() != ""]
    if len(lines) < 6:
        return None

    def fnum(i, default=0.0):
        try:
            return float(lines[i])
        except (IndexError, ValueError):
            return default

    batt_v = fnum(0)
    raw_v = fnum(1)
    # Prefer filtered V; if zero try raw
    if batt_v < 2.5 and raw_v >= 2.5:
        batt_v = raw_v

    charger_v = fnum(2)
    level = lines[3] if len(lines) > 3 else "?"
    temp_c = fnum(4)
    charging = (lines[5].lower() == "true") if len(lines) > 5 else False
    on_charger = (lines[6].lower() == "true") if len(lines) > 6 else False

    volts_ok = batt_v >= 2.5
    if volts_ok:
        _last_good_volts = batt_v
        batt_pct = voltage_to_percent(batt_v)
        display_v = batt_v
        volts_source = "live"
    else:
        # Unusable 0.0 V (common right after reboot)
        if _last_good_volts is not None and _last_good_volts >= 2.5:
            display_v = _last_good_volts
            batt_pct = voltage_to_percent(display_v)
            volts_source = "last"
        else:
            display_v = 0.0
            batt_pct = level_to_percent(level)
            volts_source = "level"

    if batt_pct is None:
        batt_pct = level_to_percent(level)

    return {
        "ip": ip,
        "batt_v": display_v,
        "batt_v_raw": batt_v,
        "raw_v": raw_v,
        "charger_v": charger_v,
        "level": level,
        "temp_c": temp_c,
        "charging": charging,
        "on_charger": on_charger,
        "batt_pct": int(batt_pct),
        "volts_source": volts_source,  # live | last | level
        "volts_ok": volts_ok,
    }


def fetch_sys_stats_ssh(ip: str) -> dict | None:
    """
    CPU / load / mem via SSH root (needs unlock key).
    Returns raw /proc/stat counters + loadavg so the app can compute %.
    """
    ssh_key = resolve_ssh_key()
    if not ip or not ssh_key:
        return None
    cmd = [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o",
        "HostKeyAlgorithms=+ssh-rsa",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"ConnectTimeout={max(1, int(SSH_TIMEOUT))}",
        f"{SSH_USER}@{ip}",
        SSH_REMOTE,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT + 1.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    out: dict = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu" and len(parts) >= 5:
            # user nice system idle iowait irq softirq ...
            vals = [int(x) for x in parts[1:8] if x.isdigit() or (x.lstrip("-").isdigit())]
            try:
                vals = [int(x) for x in parts[1:8]]
            except ValueError:
                continue
            while len(vals) < 7:
                vals.append(0)
            idle = vals[3] + vals[4]  # idle + iowait
            total = sum(vals[:7])
            out["cpu_idle"] = idle
            out["cpu_total"] = total
        elif parts[0] == "LOAD" and len(parts) >= 2:
            try:
                out["load"] = float(parts[1])
            except ValueError:
                pass
        elif parts[0] == "CORES" and len(parts) >= 2:
            try:
                out["cores"] = max(1, int(parts[1]))
            except ValueError:
                pass
        elif parts[0] == "MHZ" and len(parts) >= 2:
            try:
                out["cpu_mhz"] = int(parts[1])
            except ValueError:
                pass
        elif parts[0] == "MEM" and len(parts) >= 3:
            try:
                out["mem_used"] = int(parts[1])
                out["mem_total"] = int(parts[2])
            except ValueError:
                pass
    return out if out else None


class VectorCpuSampler:
    """Delta /proc/stat samples from Vector over SSH."""

    def __init__(self):
        self._prev_idle: int | None = None
        self._prev_total: int | None = None
        self.cpu_percent: float | None = None
        self.load: float | None = None
        self.cores: int = 1
        self.cpu_mhz: int | None = None
        self.mem_used: int | None = None
        self.mem_total: int | None = None

    def update(self, sys_stats: dict | None) -> None:
        if not sys_stats:
            return
        if "load" in sys_stats:
            self.load = float(sys_stats["load"])
        if "cores" in sys_stats:
            self.cores = max(1, int(sys_stats["cores"]))
        if "cpu_mhz" in sys_stats:
            self.cpu_mhz = int(sys_stats["cpu_mhz"])
        if "mem_used" in sys_stats:
            self.mem_used = int(sys_stats["mem_used"])
        if "mem_total" in sys_stats:
            self.mem_total = int(sys_stats["mem_total"])

        idle = sys_stats.get("cpu_idle")
        total = sys_stats.get("cpu_total")
        if idle is None or total is None:
            return
        if self._prev_idle is not None and self._prev_total is not None:
            didle = idle - self._prev_idle
            dtotal = total - self._prev_total
            if dtotal > 0:
                busy = max(0.0, 1.0 - (didle / dtotal))
                self.cpu_percent = max(0.0, min(100.0, busy * 100.0))
        self._prev_idle = idle
        self._prev_total = total

    def reset(self) -> None:
        self._prev_idle = None
        self._prev_total = None
        self.cpu_percent = None
        self.load = None


def probe_vector(
    cpu_sampler: VectorCpuSampler | None = None,
    *,
    want_sys: bool = False,
) -> tuple[bool, dict | None, str]:
    """
    Returns (online, stats_or_None, status_text).

    Online = engine stats on :8888 (cheap HTTP — always used).

    CPU/LOAD need SSH. Pass want_sys=True only when the user is viewing
    the CPU or LOAD meter so we don't hammer SSH (battery) every poll.
    """
    ip = discover_ip()
    if not ip:
        return False, None, "No Vector IP (wire-pod / SDK config)"

    # Fast fail if engine port closed
    if not tcp_open(ip, 8888):
        # Still try 443 as "half online" for status text, but no stats → offline for widget
        if tcp_open(ip, 443):
            return False, None, f"{ip}: SDK port up, no engine stats"
        return False, None, f"{ip}: offline"

    stats = fetch_engine_stats(ip)
    if not stats:
        return False, None, f"{ip}: stats failed"

    # Light port checks (no SSH) for tooltip
    ssh_port = tcp_open(ip, 22, timeout=0.5)
    sdk = tcp_open(ip, 443, timeout=0.5)
    stats["ssh"] = ssh_port
    stats["sdk"] = sdk
    stats["sys_polled"] = False

    # Attach last known CPU/LOAD from sampler (may be stale if not on those screens)
    if cpu_sampler is not None:
        stats["cpu_percent"] = cpu_sampler.cpu_percent
        stats["load"] = cpu_sampler.load
        stats["cores"] = cpu_sampler.cores
        stats["cpu_mhz"] = cpu_sampler.cpu_mhz
        stats["mem_used"] = cpu_sampler.mem_used
        stats["mem_total"] = cpu_sampler.mem_total

    # Heavy path: SSH only when user is on CPU/LOAD
    if want_sys and ssh_port:
        sys_stats = fetch_sys_stats_ssh(ip)
        stats["sys_polled"] = True
        if sys_stats and cpu_sampler is not None:
            cpu_sampler.update(sys_stats)
            stats["sys"] = sys_stats
            stats["cpu_percent"] = cpu_sampler.cpu_percent
            stats["load"] = cpu_sampler.load
            stats["cores"] = cpu_sampler.cores
            stats["cpu_mhz"] = cpu_sampler.cpu_mhz
            stats["mem_used"] = cpu_sampler.mem_used
            stats["mem_total"] = cpu_sampler.mem_total
    elif want_sys and not ssh_port:
        stats["sys_polled"] = False

    bits = []
    if stats["charging"] or stats["on_charger"]:
        bits.append("charging" if stats["charging"] else "on charger")
    if want_sys and stats.get("cpu_percent") is not None:
        bits.append(f"CPU {stats['cpu_percent']:.0f}%")
    if want_sys and stats.get("load") is not None:
        bits.append(f"load {stats['load']:.2f}")
    if want_sys:
        bits.append("sys-SSH")
    bits.append(f"SSH{'✓' if ssh_port else '✗'}")
    bits.append(f"SDK{'✓' if sdk else '✗'}")
    return True, stats, f"{ip} · " + " · ".join(bits)


def reading_for_mode(mode: str, stats: dict) -> tuple[float, str, str]:
    """(needle 0-100, value_text, label)."""
    mode = (mode or "BAT").upper()
    if mode == "TMP":
        t = float(stats.get("temp_c") or 0)
        # Map ~25–70 C → needle
        needle = max(0.0, min(100.0, (t - 25.0) / 45.0 * 100.0))
        return needle, f"{t:.0f}", "TMP"
    if mode == "VOLT":
        v = float(stats.get("batt_v") or 0)
        src = stats.get("volts_source") or "live"
        if not stats.get("volts_ok") and src == "level":
            # No real voltage yet — show level name instead of lying with 0.00
            lvl = str(stats.get("level") or "?")
            return float(stats.get("batt_pct") or 50), lvl[:6], "VOLT"
        needle = max(0.0, min(100.0, (v - 3.5) / 0.6 * 100.0)) if v >= 2.5 else float(
            stats.get("batt_pct") or 0
        )
        # Mark cached voltage lightly
        text = f"{v:.2f}" if v >= 2.5 else "—"
        if src == "last" and v >= 2.5:
            text = f"{v:.2f}*"  # * = last good (sensor still 0)
        return needle, text, "VOLT"
    if mode == "CPU":
        pct = stats.get("cpu_percent")
        mhz = stats.get("cpu_mhz")
        if pct is None:
            # First sample needs two ticks; show clock if we have it
            if mhz:
                return 0.0, f"{mhz}", "MHz"
            return 0.0, "—", "CPU"
        return float(pct), f"{pct:.0f}", "CPU"
    if mode == "LOAD":
        load = stats.get("load")
        cores = max(1, int(stats.get("cores") or 1))
        if load is None:
            return 0.0, "—", "LOAD"
        # 100% needle when load == number of cores (same idea as PC speedometer)
        needle = max(0.0, min(100.0, 100.0 * float(load) / cores))
        return needle, f"{load:.2f}", "LOAD"
    # BAT
    pct = float(stats.get("batt_pct") or 0)
    return pct, f"{pct:.0f}", "BAT"


# ---------------------------------------------------------------------------
# Drawing — Vector face + horizontal bar gauge
# ---------------------------------------------------------------------------
def _zone_color(pct):
    """Green → yellow → red by fill percent (battery-friendly)."""
    if pct < 25:
        t = pct / 25.0
        return (0.90, 0.20 + 0.15 * t, 0.15)
    if pct < 50:
        t = (pct - 25) / 25.0
        return (0.90, 0.55 + 0.15 * t, 0.12)
    if pct < 80:
        t = (pct - 50) / 30.0
        return (0.55 - 0.25 * t, 0.75, 0.20)
    return (0.20, 0.82, 0.35)


def _rounded_rect(ctx, x, y, w, h, radius):
    r = min(radius, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    ctx.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    ctx.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
    ctx.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
    ctx.close_path()


def _resolve_face_path(online: bool) -> str | None:
    """Pick face image: user override → assets → None (fallback draw)."""
    if os.path.isfile(FACE_OVERRIDE_FILE):
        return FACE_OVERRIDE_FILE
    if online and os.path.isfile(FACE_ONLINE):
        return FACE_ONLINE
    if not online and os.path.isfile(FACE_OFFLINE):
        return FACE_OFFLINE
    if os.path.isfile(FACE_ONLINE):
        return FACE_ONLINE
    if os.path.isfile(FACE_EYES):
        return FACE_EYES
    return None


# Cache cairo surfaces (path, size) → ImageSurface
_face_surface_cache: dict[tuple[str, int], cairo.ImageSurface] = {}


def _load_face_surface(path: str, size: int) -> cairo.ImageSurface | None:
    """Load image and scale to size×size via temporary pixbuf → PNG → cairo."""
    key = (path, int(size))
    if key in _face_surface_cache:
        return _face_surface_cache[key]
    try:
        # Scale with GdkPixbuf, then round-trip through a small PNG buffer
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, False)
        # Write to a memory file via temp path in ICON_DIR
        os.makedirs(ICON_DIR, exist_ok=True)
        tmp = os.path.join(ICON_DIR, f"face_{abs(hash(path))}_{size}.png")
        pb.savev(tmp, "png", [], [])
        surface = cairo.ImageSurface.create_from_png(tmp)
        _face_surface_cache[key] = surface
        return surface
    except Exception as exc:  # noqa: BLE001
        print(f"face load failed ({path}): {exc}", file=sys.stderr)
        return None


def draw_vector_face(ctx, x, y, size, online=True, mood="happy"):
    """
    Paint a Vector face image (rounded), falling back to a simple drawn face.
    Default: assets/vector-face.png next to this script.
    Override: ~/.config/vector-status/face.png
    """
    size_i = max(8, int(round(size)))
    path = _resolve_face_path(online)
    surface = _load_face_surface(path, size_i) if path else None

    if surface is not None:
        ctx.save()
        _rounded_rect(ctx, x, y, size, size, size * 0.18)
        ctx.clip()
        ctx.set_source_surface(surface, x, y)
        ctx.paint()
        if not online:
            # Dim overlay when offline
            ctx.set_source_rgba(0.0, 0.0, 0.0, 0.35)
            ctx.paint()
        ctx.restore()

        # Soft rim
        _rounded_rect(ctx, x, y, size, size, size * 0.18)
        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.40)
        ctx.set_line_width(max(1.0, size * 0.04))
        ctx.stroke()

        # Online indicator dot
        ctx.arc(x + size - size * 0.14, y + size - size * 0.14, size * 0.08, 0, 2 * math.pi)
        if online:
            ctx.set_source_rgb(0.25, 0.90, 0.45)
        else:
            ctx.set_source_rgb(0.50, 0.50, 0.52)
        ctx.fill()
        return

    # --- Fallback drawn face (only if assets missing) ---
    _rounded_rect(ctx, x, y, size, size, size * 0.22)
    if online:
        ctx.set_source_rgb(0.82, 0.84, 0.86)
    else:
        ctx.set_source_rgb(0.55, 0.56, 0.58)
    ctx.fill()
    pad = size * 0.14
    sx, sy = x + pad, y + pad * 0.95
    sw, sh = size - 2 * pad, size * 0.52
    _rounded_rect(ctx, sx, sy, sw, sh, size * 0.08)
    ctx.set_source_rgb(0.05, 0.05, 0.07)
    ctx.fill()
    eye_h = sh * 0.42
    eye_w = sw * 0.28
    eye_y = sy + (sh - eye_h) * 0.45
    gap = sw * 0.12
    left_x = sx + (sw - 2 * eye_w - gap) * 0.5
    right_x = left_x + eye_w + gap
    eye_col = (0.55, 0.95, 1.0) if online else (0.35, 0.38, 0.42)
    for ex in (left_x, right_x):
        _rounded_rect(ctx, ex, eye_y, eye_w, eye_h, eye_h * 0.25)
        ctx.set_source_rgb(*eye_col)
        ctx.fill()


def draw_horizontal_bar(ctx, x, y, w, h, percent, online=True):
    """Horizontal progress bar with zone track + fill."""
    percent = max(0.0, min(100.0, percent))

    # Track background
    _rounded_rect(ctx, x, y, w, h, h * 0.45)
    ctx.set_source_rgb(0.18, 0.20, 0.24)
    ctx.fill()

    # Zone underlay (green / yellow / red segments)
    zones = [
        (0, 25, (0.55, 0.18, 0.15)),
        (25, 50, (0.55, 0.40, 0.12)),
        (50, 80, (0.40, 0.50, 0.15)),
        (80, 100, (0.15, 0.50, 0.28)),
    ]
    for z0, z1, color in zones:
        zx = x + w * (z0 / 100.0)
        zw = w * ((z1 - z0) / 100.0)
        ctx.rectangle(zx, y, zw, h)
        ctx.set_source_rgba(color[0], color[1], color[2], 0.35)
        ctx.fill()

    # Fill
    if percent > 0.5:
        fw = max(h * 0.5, w * (percent / 100.0))
        _rounded_rect(ctx, x, y, fw, h, h * 0.45)
        r, g, b = _zone_color(percent) if online else (0.45, 0.45, 0.48)
        # Gradient-ish: slightly brighter leading edge
        ctx.set_source_rgb(r, g, b)
        ctx.fill()

        # Shine line
        ctx.rectangle(x + 2, y + 1, max(0, fw - 4), max(1, h * 0.28))
        ctx.set_source_rgba(1, 1, 1, 0.18)
        ctx.fill()

    # Border
    _rounded_rect(ctx, x, y, w, h, h * 0.45)
    ctx.set_source_rgba(1, 1, 1, 0.12)
    ctx.set_line_width(1.0)
    ctx.stroke()


def draw_vector_gauge(ctx, width, height, percent, label, value_text, online=True):
    percent = max(0.0, min(100.0, percent))

    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    # Background card
    _rounded_rect(ctx, 0, 0, width, height, 12)
    if online:
        ctx.set_source_rgba(0.09, 0.12, 0.16, 0.96)
    else:
        ctx.set_source_rgba(0.14, 0.14, 0.15, 0.92)
    ctx.fill()

    # Face on the left
    face_size = height - 12
    face_x, face_y = 6, 6
    mood = "happy" if online else "sleepy"
    draw_vector_face(ctx, face_x, face_y, face_size, online=online, mood=mood)

    # Right-side content
    text_x = face_x + face_size + 10
    content_w = width - text_x - 10

    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

    # Title row
    ctx.set_font_size(9)
    ctx.set_source_rgb(0.55, 0.85, 1.0)
    ctx.move_to(text_x, 14)
    ctx.show_text("VECTOR")

    # Online LED
    ctx.arc(width - 11, 11, 3.5, 0, 2 * math.pi)
    ctx.set_source_rgb(0.2, 0.9, 0.4) if online else ctx.set_source_rgb(0.5, 0.5, 0.52)
    ctx.fill()

    # Value (+ unit; skip V when showing level name after reboot zero-volts)
    if label == "BAT" or label == "CPU":
        unit = "%"
    elif label == "TMP":
        unit = "°C"
    elif label == "VOLT":
        try:
            float(str(value_text).replace("*", ""))
            unit = "V"
        except ValueError:
            unit = ""
    elif label == "MHz":
        unit = ""
    elif label == "LOAD":
        unit = ""
    else:
        unit = ""
    # First CPU sample shows MHz without % 
    if label == "MHz":
        main = f"{value_text}MHz"
    else:
        main = f"{value_text}{unit}"
    ctx.set_font_size(16)
    ctx.set_source_rgb(0.96, 0.96, 0.98)
    ctx.move_to(text_x, 34)
    ctx.show_text(main)

    # Mode label
    ctx.set_font_size(9)
    ctx.set_source_rgb(0.65, 0.70, 0.75)
    ctx.move_to(text_x + content_w - 28, 34)
    ctx.show_text(label)

    # Horizontal bar
    bar_x = text_x
    bar_y = height - 18
    bar_h = 10
    bar_w = content_w
    draw_horizontal_bar(ctx, bar_x, bar_y, bar_w, bar_h, percent, online=online)


def render_gauge_png(path, width, height, percent, label, value_text, online=True):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    draw_vector_gauge(ctx, width, height, percent, label, value_text, online=online)
    surface.write_to_png(path)


def render_tray_png(path, size, online: bool, percent: float = 0.0):
    """Tray: Vector face only (no needle)."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    # slight padding
    pad = 1
    draw_vector_face(ctx, pad, pad, size - 2 * pad, online=online)
    # Tiny bar under face for battery when online
    if online and percent > 0:
        bh = max(2, int(size * 0.10))
        by = size - bh - 1
        draw_horizontal_bar(ctx, 2, by, size - 4, bh, percent, online=True)
    surface.write_to_png(path)


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
def acquire_lock():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lock_path = os.path.join(CONFIG_DIR, "vector-status.lock")
    fp = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Vector Status is already running.", file=sys.stderr)
        sys.exit(0)
    fp.write(str(os.getpid()))
    fp.flush()
    return fp


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class VectorStatusApp:
    def __init__(self):
        os.makedirs(ICON_DIR, exist_ok=True)
        self._lock = acquire_lock()
        self._cpu_sampler = VectorCpuSampler()
        self._online = False
        self._stats: dict | None = None
        self._status_text = "Starting…"
        self._mode = load_mode()
        self._percent = 0.0
        self._value_text = "—"
        self._label = "BAT"
        self._icon_toggle = 0

        self._tray_paths = [
            os.path.join(ICON_DIR, "tray_a.png"),
            os.path.join(ICON_DIR, "tray_b.png"),
        ]
        self._gauge_paths = [
            os.path.join(ICON_DIR, "gauge_a.png"),
            os.path.join(ICON_DIR, "gauge_b.png"),
        ]

        self._use_layer = False
        self._pos_x = 0
        self._pos_y = 0
        self._drag_origin_x = 0
        self._drag_origin_y = 0
        self._press_root_x = 0.0
        self._press_root_y = 0.0
        self._dragging = False
        self._drag_moved = False
        self._grab_seat = None
        self._poll_id = 0
        self._timer_id = 0

        # Seed images
        render_tray_png(self._tray_paths[0], ICON_SIZE, False)
        render_tray_png(self._tray_paths[1], ICON_SIZE, False)
        render_gauge_png(
            self._gauge_paths[0], GAUGE_W, GAUGE_H, 0, "BAT", "—", online=False
        )
        render_gauge_png(
            self._gauge_paths[1], GAUGE_W, GAUGE_H, 0, "BAT", "—", online=False
        )

        self._indicator = AppIndicator3.Indicator.new(
            APP_ID,
            self._tray_paths[0],
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._indicator.set_title(TITLE)
        self._indicator.set_menu(self._build_menu())

        self._gauge_window = None
        self._gauge_image = None
        self._build_gauge_widget()
        # Start hidden until online
        self._gauge_window.hide()

        self._tick()
        # First interval chosen after tick

    def _build_menu(self):
        menu = Gtk.Menu()

        item_refresh = Gtk.MenuItem(label="Refresh now")
        item_refresh.connect("activate", lambda *_: self._tick())
        menu.append(item_refresh)

        item_cycle = Gtk.MenuItem(label="Next meter (BAT/TMP/VOLT/CPU/LOAD)")
        item_cycle.connect("activate", lambda *_: self._cycle_mode())
        menu.append(item_cycle)

        item_reset = Gtk.MenuItem(label="Reset position")
        item_reset.connect("activate", self._reset_position)
        menu.append(item_reset)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._quit)
        menu.append(item_quit)

        menu.show_all()
        return menu

    def _reset_position(self, *_args):
        x, y = default_position()
        self._set_position(x, y, persist=True)

    def _mode_needs_ssh(self) -> bool:
        """CPU/LOAD need SSH; everything else is cheap HTTP only."""
        return (self._mode or "").upper() in ("CPU", "LOAD")

    def _cycle_mode(self, *_args):
        idx = MODES.index(self._mode) if self._mode in MODES else 0
        self._mode = MODES[(idx + 1) % len(MODES)]
        save_mode(self._mode)
        # Immediate refresh so CPU/LOAD start sampling as soon as you open them
        self._tick()

    def _build_gauge_widget(self):
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(TITLE)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_app_paintable(True)
        win.set_size_request(GAUGE_W, GAUGE_H)
        win.set_default_size(GAUGE_W, GAUGE_H)

        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)

        saved = load_position()
        if saved is None:
            self._pos_x, self._pos_y = default_position()
        else:
            self._pos_x, self._pos_y = clamp_position(*saved)

        self._use_layer = GtkLayerShell.is_supported()
        if self._use_layer:
            GtkLayerShell.init_for_window(win)
            GtkLayerShell.set_namespace(win, APP_ID)
            GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.RIGHT, False)
            GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.BOTTOM, False)
            GtkLayerShell.set_exclusive_zone(win, 0)
            GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
            GtkLayerShell.set_margin(win, GtkLayerShell.Edge.LEFT, self._pos_x)
            GtkLayerShell.set_margin(win, GtkLayerShell.Edge.TOP, self._pos_y)
        else:
            win.set_keep_above(True)
            win.set_type_hint(Gdk.WindowTypeHint.DOCK)
            win.move(self._pos_x, self._pos_y)

        win.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.STRUCTURE_MASK
        )
        win.connect("button-press-event", self._on_press)
        win.connect("button-release-event", self._on_release)
        win.connect("motion-notify-event", self._on_motion)
        win.connect(
            "realize",
            lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "grab")
            ),
        )

        self._gauge_image = Gtk.Image()
        self._gauge_image.set_events(Gdk.EventMask.STRUCTURE_MASK)
        self._load_gauge_image(self._gauge_paths[0])
        win.add(self._gauge_image)

        win.set_has_tooltip(True)
        win.connect("query-tooltip", self._on_query_tooltip)

        self._gauge_window = win
        win.show_all()

    def _on_query_tooltip(self, _widget, _x, _y, _keyboard, tooltip):
        if self._dragging:
            return False
        if not self._online or not self._stats:
            tooltip.set_text(f"Vector offline\n{self._status_text}")
            return True
        s = self._stats
        chg = "yes" if s.get("charging") else ("on pad" if s.get("on_charger") else "no")
        src = s.get("volts_source") or "live"
        raw = s.get("batt_v_raw")
        raw_s = f"{raw:.3f}" if isinstance(raw, (int, float)) else "?"
        note = {
            "live": "live volts",
            "last": "last good volts (sensor currently 0 — common after reboot)",
            "level": "from level name (volts not ready yet)",
        }.get(src, src)
        cpu = s.get("cpu_percent")
        load = s.get("load")
        mhz = s.get("cpu_mhz")
        cores = s.get("cores") or "?"
        mem_u, mem_t = s.get("mem_used"), s.get("mem_total")
        cpu_s = f"{cpu:.0f}%" if cpu is not None else "—"
        load_s = f"{load:.2f}" if load is not None else "—"
        mhz_s = f"{mhz} MHz" if mhz else "—"
        mem_s = f"{mem_u}/{mem_t} MB" if mem_u is not None and mem_t else "—"
        sys_note = (
            "live SSH"
            if s.get("sys_polled")
            else "CPU/LOAD only polled on those screens (eco)"
        )
        tip = (
            f"Vector online\n"
            f"{self._status_text}\n"
            f"Battery: {s.get('batt_pct')}% · {s.get('batt_v'):.2f} V (raw {raw_s}) · {s.get('level')} [{note}]\n"
            f"Temp: {s.get('temp_c'):.0f}°C · Charging: {chg}\n"
            f"CPU: {cpu_s} · {mhz_s} · Load: {load_s} / {cores} cores · RAM: {mem_s}\n"
            f"Sys: {sys_note}\n"
            f"Click = BAT/TMP/VOLT/CPU/LOAD  ·  Drag to move"
        )
        tooltip.set_text(tip)
        return True

    def _set_position(self, x, y, persist=False):
        x, y = clamp_position(x, y)
        self._pos_x, self._pos_y = x, y
        if self._gauge_window is None:
            return
        if self._use_layer:
            GtkLayerShell.set_margin(self._gauge_window, GtkLayerShell.Edge.LEFT, x)
            GtkLayerShell.set_margin(self._gauge_window, GtkLayerShell.Edge.TOP, y)
        else:
            self._gauge_window.move(x, y)
        if persist:
            save_position(x, y)

    def _load_gauge_image(self, path):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            self._gauge_image.set_from_pixbuf(pixbuf)
        except Exception as exc:  # noqa: BLE001
            print(f"gauge image load failed: {exc}", file=sys.stderr)

    def _set_visible(self, visible: bool):
        if self._gauge_window is None:
            return
        if visible:
            if not self._gauge_window.get_visible():
                self._gauge_window.show_all()
        else:
            if self._gauge_window.get_visible():
                self._gauge_window.hide()

    def _redraw(self):
        self._icon_toggle = 1 - self._icon_toggle
        gpath = self._gauge_paths[self._icon_toggle]
        render_gauge_png(
            gpath,
            GAUGE_W,
            GAUGE_H,
            self._percent,
            self._label,
            self._value_text,
            online=self._online,
        )
        if self._online:
            self._load_gauge_image(gpath)

        tpath = self._tray_paths[self._icon_toggle]
        render_tray_png(tpath, ICON_SIZE, self._online, self._percent)
        self._indicator.set_icon_full(
            tpath,
            f"Vector {'online' if self._online else 'offline'}",
        )
        if self._online:
            self._indicator.set_label(
                f"{self._label} {self._value_text}",
                "VECTOR",
            )
        else:
            self._indicator.set_label("", "")

    def _schedule_next(self):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        ms = POLL_ONLINE_MS if self._online else POLL_OFFLINE_MS
        self._timer_id = GLib.timeout_add(ms, self._tick)

    def _tick(self, *_args):
        want_sys = self._mode_needs_ssh()
        online, stats, status = probe_vector(
            self._cpu_sampler, want_sys=want_sys
        )
        was_online = self._online
        self._online = online
        self._stats = stats
        self._status_text = status

        if online and stats:
            self._percent, self._value_text, self._label = reading_for_mode(
                self._mode, stats
            )
            self._set_visible(True)
            if not was_online:
                print(f"Vector online: {status}", flush=True)
        else:
            self._cpu_sampler.reset()
            self._percent = 0.0
            self._value_text = "—"
            self._label = "OFF"
            self._set_visible(False)
            if was_online:
                print(f"Vector offline: {status}", flush=True)

        self._redraw()
        self._schedule_next()
        return False  # we reschedule ourselves

    # ----- drag (same pattern as pc-speedometer) -----
    def _master_pointer_pos(self):
        if self._gauge_window is None:
            return None
        seat = self._gauge_window.get_display().get_default_seat()
        if seat is None:
            return None
        device = seat.get_pointer()
        if device is None:
            return None
        try:
            _screen, px, py = device.get_position()
            return int(px), int(py)
        except Exception:  # noqa: BLE001
            return None

    def _stop_poll(self):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def _ungrab_pointer(self):
        self._stop_poll()
        if self._grab_seat is not None:
            try:
                self._grab_seat.ungrab()
            except Exception:  # noqa: BLE001
                pass
            self._grab_seat = None

    def _apply_drag_from_root(self, root_x, root_y):
        dx = root_x - self._press_root_x
        dy = root_y - self._press_root_y
        if not self._drag_moved:
            if abs(dx) < DRAG_THRESHOLD and abs(dy) < DRAG_THRESHOLD:
                return
            self._drag_moved = True
        self._set_position(
            int(self._drag_origin_x + dx),
            int(self._drag_origin_y + dy),
            persist=False,
        )

    def _poll_drag(self):
        if not self._dragging:
            self._poll_id = 0
            return False
        pos = self._master_pointer_pos()
        if pos is not None:
            self._apply_drag_from_root(pos[0], pos[1])
        return True

    def _on_press(self, widget, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 3:
            menu = self._indicator.get_menu()
            if menu is not None:
                menu.popup_at_pointer(event)
            return True
        if event.button == 1:
            self._dragging = True
            self._drag_moved = False
            self._drag_origin_x = self._pos_x
            self._drag_origin_y = self._pos_y
            self._press_root_x = float(event.x_root)
            self._press_root_y = float(event.y_root)
            if self._press_root_x == 0 and self._press_root_y == 0:
                pos = self._master_pointer_pos()
                if pos is not None:
                    self._press_root_x, self._press_root_y = float(pos[0]), float(pos[1])

            gdk_win = widget.get_window()
            display = widget.get_display()
            cursor = Gdk.Cursor.new_from_name(display, "grabbing")
            if gdk_win is not None:
                gdk_win.set_cursor(cursor)
            seat = display.get_default_seat()
            if seat is not None and gdk_win is not None:
                status = seat.grab(
                    gdk_win,
                    Gdk.SeatCapabilities.POINTER,
                    True,
                    cursor,
                    event,
                    None,
                    None,
                )
                if status == Gdk.GrabStatus.SUCCESS:
                    self._grab_seat = seat
            self._stop_poll()
            self._poll_id = GLib.timeout_add(16, self._poll_drag)
            return True
        return False

    def _on_motion(self, _widget, event):
        if not self._dragging:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        rx, ry = float(event.x_root), float(event.y_root)
        if rx == 0 and ry == 0:
            pos = self._master_pointer_pos()
            if pos is None:
                return True
            rx, ry = float(pos[0]), float(pos[1])
        self._apply_drag_from_root(rx, ry)
        return True

    def _on_release(self, widget, event):
        if event.button != 1:
            return False
        self._ungrab_pointer()
        gdk_win = widget.get_window()
        if gdk_win is not None:
            gdk_win.set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), "grab")
            )
        was_dragging = self._dragging
        moved = self._drag_moved
        self._dragging = False
        self._drag_moved = False
        if was_dragging and moved:
            self._set_position(self._pos_x, self._pos_y, persist=True)
        elif was_dragging and not moved:
            self._cycle_mode()
        return True

    def _quit(self, *_args):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._ungrab_pointer()
        Gtk.main_quit()


def main():
    # Need a display
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("No display (DISPLAY/WAYLAND_DISPLAY). Run under your desktop session.", file=sys.stderr)
        return 1
    VectorStatusApp()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
