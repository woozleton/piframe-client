#!/usr/bin/env python3
"""
PiFrame client: WebSocket bridge that drives Chromium kiosk playback.

Keeps a single long-lived Chromium process alive under `cage` and updates a
local browser state file to switch between videos, video playlists, image
slideshows, and a looping idle placeholder. This provides one renderer for all
media types and keeps the display owned by the browser instead of bouncing
between specialized viewers.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import websocket
from browser_renderer_template import render_browser_html

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

# ---------------------------------------------------------------------------
# Configuration defaults (env overrides where noted)
# ---------------------------------------------------------------------------
STATUS_UPDATE_INTERVAL = 2  # seconds; periodic backstop in case events are lost
SERVER_DEFAULT = os.environ.get("PIFRAME_SERVER", "ws://192.168.100.100:8080/ws")
NAS_ROOT = os.environ.get("PIFRAME_NAS_ROOT", "/mnt/nas").rstrip("/") or "/mnt/nas"
CHROMIUM_BIN = os.environ.get("PIFRAME_CHROMIUM_BIN", "chromium").strip() or "chromium"
CAGE_BIN = os.environ.get("PIFRAME_CAGE_BIN", "cage").strip() or "cage"
WLRCTL_BIN = os.environ.get("PIFRAME_WLRCTL_BIN", "wlrctl").strip() or "wlrctl"
BROWSER_ROTATION_DEGREES = 270
BROWSER_STATE_FILE = Path("/tmp/piframe_browser_state.json")
BROWSER_HTML_FILE = Path("/tmp/piframe_browser.html")
BROWSER_PROFILE_DIR = Path("/tmp/piframe_chromium_profile")
BROWSER_CACHE_DIR = Path("/tmp/piframe_chromium_cache")
BROWSER_LOG_FILE = Path("/tmp/piframe_browser.log")
CLIENT_SETTINGS_FILE = Path(__file__).resolve().parent / "client_settings.json"
BROWSER_LOG_MAX_BYTES = int(
    float(os.environ.get("PIFRAME_BROWSER_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
)
BROWSER_LOG_BACKUPS = max(1, int(os.environ.get("PIFRAME_BROWSER_LOG_BACKUPS", "3")))
BROWSER_MIN_ITEM_DURATION = 1.0
BROWSER_STATE_POLL_MS = 250
# Loopback channel the browser POSTs to when it advances a slide. Status
# loop reads the latest reported index and forwards it to the manager.
BROWSER_EVENT_HOST = "127.0.0.1"
BROWSER_EVENT_PORT = int(os.environ.get("PIFRAME_BROWSER_EVENT_PORT", "18888"))
# Audio companion sidecar - separate mpv process so the audio device is
# decoupled from Chromium's <video>. Chromium on Pi keeps the audio
# device locked while a video plays (even muted), which prevents an
# in-page <audio> element from claiming HDMI. mpv opens its own audio
# stream via PulseAudio/PipeWire (or ALSA dmix), so the audio server
# mixes them at the OS level.
COMPANION_MPV_BIN = os.environ.get("PIFRAME_COMPANION_MPV_BIN", "mpv").strip() or "mpv"
COMPANION_MPV_SOCKET = Path(os.environ.get("PIFRAME_COMPANION_MPV_SOCKET", "/tmp/piframe_companion_mpv.sock"))
COMPANION_MPV_LOG_FILE = Path("/tmp/piframe_companion_mpv.log")
BROWSER_TRANSITION = os.environ.get("PIFRAME_BROWSER_TRANSITION", "fade").strip() or "fade"
BROWSER_TRANSITION_DURATION_MS = max(
    0,
    int(float(os.environ.get("PIFRAME_BROWSER_TRANSITION_DURATION_MS", "600"))),
)
BROWSER_VIDEO_FILL_MODE = (
    os.environ.get("PIFRAME_BROWSER_VIDEO_FILL_MODE", "contain").strip() or "contain"
)
BROWSER_SHOW_HUD = (
    os.environ.get("PIFRAME_BROWSER_SHOW_HUD", "").strip().lower()
    in {"1", "true", "t", "yes", "y", "on"}
)
IDLE_MEDIA_ENV = "PIFRAME_IDLE_MEDIA"
# Bundled locally so idle display doesn't depend on the NAS being reachable.
# Priority: HTML page > animated mp4 > still png > still jpg.
_IDLE_MEDIA_DIR = Path(__file__).resolve().parent
IDLE_MEDIA_DEFAULT_CANDIDATES = (
    str(_IDLE_MEDIA_DIR / "idle.html"),
    str(_IDLE_MEDIA_DIR / "idle.mp4"),
    str(_IDLE_MEDIA_DIR / "idle.png"),
    str(_IDLE_MEDIA_DIR / "idle.jpg"),
)

# Audio visualizer presets the bundle ships with. Heartbeat reports
# this list to the orchestrator so the audio-playlist inspector can
# offer a per-playlist visualizer dropdown sourced from each Pi's
# capabilities (vs hard-coding on the server). Source of truth here -
# browser_renderer_template.py uses this same list to filter the
# Butterchurn preset map at runtime.
AUDIO_VISUALIZER_PRESETS = [
    "martin - reflections on black tiles",
    "flexi + amandio c - organic12-3d-2",
    "Eo.S. + Zylot - skylight",
    "Flexi + amandio c - piercing 05",
    "flexi + geiss - pogo cubes vs. tokamak vs. game of life",
    "Flexi, martin + geiss - dedicated to the sherwin maxawow",
    "martin - disco mix 4",
    "martin - mandelbox explorer",
    "martin - mucus cervix",
    "suksma - heretical crosscut playpen",
]


def _default_idle_media() -> str:
    for candidate in IDLE_MEDIA_DEFAULT_CANDIDATES:
        try:
            if Path(candidate).exists():
                return candidate
        except OSError:
            continue
    # No bundled idle file present; return the jpg path as a last-resort
    # placeholder (the renderer treats a missing idle as "blank").
    return IDLE_MEDIA_DEFAULT_CANDIDATES[-1]


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".avif",
}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def _normalize_media_url(url: Optional[str]) -> Optional[str]:
    """Map UNC/IP-style paths to the configured NAS mount for Linux clients."""
    if not url:
        return url
    value = url.strip()
    if value.startswith(f"{NAS_ROOT}/") or value == NAS_ROOT:
        return value

    if value.startswith("//") or value.startswith("\\\\") or value.startswith("/192."):
        normalized = value.replace("\\", "/").lstrip("/ ")
        parts = [p for p in normalized.split("/") if p]
        if len(parts) >= 3:
            start_idx = 2  # drop host + share
            third = parts[2].lower().replace(" ", "")
            if third in {"piframemedia", "frametv"}:
                start_idx = 3  # also drop known container folder
            remaining = "/".join(parts[start_idx:])
            return f"{NAS_ROOT}/{remaining}" if remaining else NAS_ROOT
    return value


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Best-effort bool coercion for JSON-ish inputs (bool/int/str)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "f", "no", "n", "off", ""}:
            return False
    return default


def get_system_info() -> Dict[str, str]:
    """Gather basic system identification details."""
    info: Dict[str, str] = {}

    try:
        info["os"] = f"{os.uname().sysname} {os.uname().release}"
        info["platform"] = os.uname().version
    except Exception:
        info["os"] = "Unknown"
        info["platform"] = "Unknown"

    try:
        mac = ":".join(
            [f"{(uuid.getnode() >> shift) & 0xFF:02x}" for shift in range(0, 2 * 6, 2)][
                ::-1
            ]
        )
        info["mac_address"] = mac
    except Exception:
        info["mac_address"] = "Unknown"

    try:
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_socket.connect(("8.8.8.8", 80))
        info["ip_address"] = temp_socket.getsockname()[0]
        temp_socket.close()
    except Exception:
        info["ip_address"] = "Unknown"

    return info


def _collect_system_metrics() -> Dict[str, Any]:
    """Collect lightweight system metrics for status reporting (best-effort)."""
    metrics: Dict[str, Any] = {}

    if psutil:
        try:
            metrics["cpu_percent"] = psutil.cpu_percent(interval=None)
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            metrics.update(
                {
                    "memory_percent": vm.percent,
                    "memory_used_bytes": int(vm.used),
                    "memory_total_bytes": int(vm.total),
                }
            )
        except Exception:
            pass
        try:
            du = psutil.disk_usage("/")
            metrics.update(
                {
                    "storage_percent": du.percent,
                    "storage_used_bytes": int(du.used),
                    "storage_total_bytes": int(du.total),
                }
            )
        except Exception:
            pass

    if "storage_total_bytes" not in metrics:
        try:
            stat = os.statvfs("/")
            total = stat.f_frsize * stat.f_blocks
            available = stat.f_frsize * stat.f_bavail
            used = total - available
            metrics["storage_total_bytes"] = int(total)
            metrics["storage_used_bytes"] = int(used)
            if total:
                metrics["storage_percent"] = (used / total) * 100.0
        except Exception:
            pass

    if "cpu_percent" not in metrics:
        try:
            load1, _, _ = os.getloadavg()
            cpu_count = os.cpu_count() or 1
            metrics["cpu_percent"] = max(0.0, min(100.0, (load1 / cpu_count) * 100.0))
        except Exception:
            pass

    if "memory_total_bytes" not in metrics:
        try:
            meminfo: Dict[str, str] = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as mem_file:
                for line in mem_file:
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    meminfo[key.strip()] = value.strip()
            total_kb = float(meminfo.get("MemTotal", "0 kB").split()[0])
            available_kb = float(meminfo.get("MemAvailable", "0 kB").split()[0])
            used_kb = max(total_kb - available_kb, 0.0)
            metrics["memory_total_bytes"] = int(total_kb * 1024)
            metrics["memory_used_bytes"] = int(used_kb * 1024)
            if metrics["memory_total_bytes"]:
                metrics["memory_percent"] = (
                    metrics["memory_used_bytes"]
                    / metrics["memory_total_bytes"]
                    * 100.0
                )
        except Exception:
            pass

    if "temperature_c" not in metrics:
        try:
            for temp_path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
                try:
                    raw = temp_path.read_text(encoding="utf-8").strip()
                    temp = float(raw) / 1000.0
                    metrics["temperature_c"] = temp
                    metrics["temperature_f"] = temp * 9.0 / 5.0 + 32.0
                    break
                except Exception:
                    continue
        except Exception:
            pass

    return metrics


def _log(event: str, **fields: Any) -> None:
    """Emit compact structured logs that stay readable in journalctl."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{timestamp}]", event]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    print(" | ".join(parts), flush=True)


LAST_UPDATE_FILE = Path(
    os.environ.get("PIFRAME_LAST_UPDATE_FILE", "/tmp/piframe_last_update.json")
)


def _read_and_consume_last_update() -> Optional[Dict[str, Any]]:
    """Read the marker file dropped by update.sh after a successful
    pull, then delete it so the same record isn't re-reported every
    boot. Returned dict is shipped home in the next status_update
    heartbeat so the server's System tab can confirm a restart took
    and show what changed.

    Returns None if the file is missing, malformed, or unreadable. We
    swallow errors aggressively here - this is a nice-to-have signal,
    not a correctness requirement."""
    try:
        if not LAST_UPDATE_FILE.exists():
            return None
        data = json.loads(LAST_UPDATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            if LAST_UPDATE_FILE.exists():
                LAST_UPDATE_FILE.unlink()
        except OSError:
            pass
    return data if isinstance(data, dict) else None


def _read_client_version() -> Optional[str]:
    """Best-effort: short git SHA of the running checkout. Surfaced in
    status_update heartbeats so the server's System tab can show the
    operator which commit each Pi is running. Returns None when git
    isn't available or the directory isn't a checkout (e.g. unzipped
    tarball install)."""
    repo_dir = Path(__file__).resolve().parent
    git_bin = shutil.which("git")
    if not git_bin:
        return None
    try:
        result = subprocess.run(
            [git_bin, "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (result.stdout or "").strip()
    return sha or None


def _detect_audio_server() -> str:
    """Return one of: 'pipewire', 'pulseaudio', 'alsa-only'.

    Bare ALSA is single-stream by default - whichever process opens
    the audio device first holds it exclusive. The audio companion
    (mpv sidecar) won't be audible while Chromium's <video> is
    playing through the same device. PA / PipeWire add a userspace
    mixer that splits one audio device across N processes.

    Detection order:
      1. pactl (PA CLI) - succeeds when either real PA or
         pipewire-pulse is running. Output mentions "PipeWire" when
         it's the latter.
      2. pgrep for the daemon names directly.
      3. /run/user/<uid>/{pulse,pipewire-0} sockets - lighter than
         spawning subprocesses.
    """
    pactl = shutil.which("pactl")
    if pactl:
        try:
            result = subprocess.run(
                [pactl, "info"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode == 0:
                if "PipeWire" in (result.stdout or ""):
                    return "pipewire"
                return "pulseaudio"
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ("pipewire", "pipewire-pulse", "wireplumber"):
        if shutil.which("pgrep"):
            try:
                if subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True, timeout=2, check=False,
                ).returncode == 0:
                    return "pipewire"
            except (OSError, subprocess.SubprocessError):
                pass
    if shutil.which("pgrep"):
        try:
            if subprocess.run(
                ["pgrep", "-x", "pulseaudio"],
                capture_output=True, timeout=2, check=False,
            ).returncode == 0:
                return "pulseaudio"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        uid = os.geteuid()
        runtime = Path(f"/run/user/{uid}")
        if (runtime / "pipewire-0").exists():
            return "pipewire"
        if (runtime / "pulse" / "native").exists():
            return "pulseaudio"
    except Exception:
        pass
    return "alsa-only"


def _set_chromium_sink_input_mute(mute: bool) -> None:
    """Mute (or unmute) Chromium's audio at the PipeWire/PulseAudio
    sink-input level so the companion mpv is audible underneath.

    Why: setting <video>.muted in the renderer only silences that
    element's contribution to Chromium's per-process audio node.
    Chromium's PipeWire node stays connected to the sink and can
    still emit decoded samples, which compete with mpv's stream in
    the mixer (companion audio gets drowned out). Muting at the
    sink-input level cuts Chromium out of the mix cleanly.

    Uses pactl (works against either real PulseAudio or
    pipewire-pulse). Best-effort - returns silently if pactl is
    missing or no Chromium sink-input is found (e.g. nothing is
    playing yet)."""
    pactl = shutil.which("pactl")
    if not pactl:
        return
    try:
        result = subprocess.run(
            [pactl, "list", "sink-inputs", "short"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    chromium_ids: list[str] = []
    for line in (result.stdout or "").splitlines():
        # `pactl list sink-inputs short` is whitespace-separated and
        # doesn't include the application.process.binary name. We
        # need the verbose listing to identify which input is
        # Chromium - fall through to the long form.
        pass
    try:
        long_result = subprocess.run(
            [pactl, "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if long_result.returncode != 0:
        return
    current_id: Optional[str] = None
    for raw in (long_result.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            current_id = line.split("#", 1)[1].strip()
            continue
        # Match either application.name = "Chromium" or
        # application.process.binary = "chromium" or node.name with
        # a Chromium prefix (covers chromium / chromium-browser /
        # chrome forks).
        lower = line.lower()
        if current_id is not None and (
            'application.name = "chromium' in lower
            or 'application.process.binary = "chromium' in lower
            or 'node.name = "chromium' in lower
        ):
            chromium_ids.append(current_id)
            current_id = None  # avoid double-matching the same block
    flag = "1" if mute else "0"
    for sid in chromium_ids:
        try:
            subprocess.run(
                [pactl, "set-sink-input-mute", sid, flag],
                capture_output=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
    _log("companion_chromium_mute", count=len(chromium_ids), muted=bool(mute))


def _load_client_settings(path: Path) -> Dict[str, Any]:
    """Load small persisted client settings from disk."""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_client_settings(path: Path, payload: Dict[str, Any]) -> None:
    """Persist small client settings atomically."""
    try:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:
        _log("settings_save_failed", path=path, error=exc)


def _rotate_log(path: Path, *, max_bytes: int, backups: int) -> None:
    """Rotate a log file in-place when it exceeds the configured size."""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except Exception:
        return

    oldest = path.with_name(f"{path.name}.{backups}")
    try:
        if oldest.exists():
            oldest.unlink()
    except Exception:
        pass

    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        try:
            if src.exists():
                src.replace(dst)
        except Exception:
            continue

    try:
        path.replace(path.with_name(f"{path.name}.1"))
    except Exception:
        pass


def _clear_stale_wayland_sockets(runtime_dir: str) -> None:
    """Remove leftover wayland-N / wayland-N.lock from a prior crashed cage.

    wlroots fails with "Unable to open Wayland socket: Invalid argument" when
    a stale lockfile is present, so cage exits 1 on every restart until the
    runtime dir is cleaned up.
    """
    try:
        entries = list(Path(runtime_dir).glob("wayland-*"))
    except OSError:
        return
    removed = []
    for entry in entries:
        try:
            entry.unlink()
            removed.append(entry.name)
        except OSError:
            continue
    if removed:
        _log("wayland_sockets_cleared", runtime_dir=runtime_dir, removed=",".join(removed))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class ClientConfig:
    server: str
    client_id: str
    name: str
    group: str = "default"


class BrowserController:
    """
    Drives a single Chromium kiosk session under cage and updates a local JSON
    state file that the browser polls to render images/videos/playlists.
    """

    def __init__(self, rotation_degrees: int = BROWSER_ROTATION_DEGREES) -> None:
        self.rotation_degrees = rotation_degrees
        self.process: Optional[subprocess.Popen[Any]] = None
        self.slideshow_active = False
        self.slideshow_images: List[str] = []
        self.last_command: str = ""
        self._settings_path = CLIENT_SETTINGS_FILE
        self._last_volume: float = 75.0
        self._last_mute: bool = False
        self._load_persisted_audio_state()
        self._control_token = 0
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "mode": "idle",
            "playlist_name": "",
            "items": [],
            "repeat": True,
            "loop": True,
            "interval": 5.0,
            "shuffle": False,
            "transition": BROWSER_TRANSITION,
            "transition_duration_ms": BROWSER_TRANSITION_DURATION_MS,
            "video_fill_mode": BROWSER_VIDEO_FILL_MODE,
            "control": None,
            "volume": self._last_volume,
            "muted": self._last_mute,
            "idle_item": None,
            "banner": None,
        }
        self._write_html()
        self._write_state()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def shutdown(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def _load_persisted_audio_state(self) -> None:
        settings = _load_client_settings(self._settings_path)
        volume = settings.get("volume")
        muted = settings.get("muted")
        try:
            if volume is not None:
                self._last_volume = self._clamp_volume(float(volume))
        except (TypeError, ValueError):
            self._last_volume = 75.0
        self._last_mute = _coerce_bool(muted, default=self._last_volume <= 0)
        _log(
            "settings_loaded",
            path=self._settings_path,
            volume=f"{self._last_volume:.1f}",
            muted=self._last_mute,
        )

    def _persist_audio_state(self) -> None:
        _save_client_settings(
            self._settings_path,
            {"volume": self._last_volume, "muted": self._last_mute},
        )

    @staticmethod
    def _clamp_volume(level: float) -> float:
        return max(0.0, min(100.0, float(level)))

    def _write_html(self) -> None:
        # Butterchurn (audio visualizer) ships as two minified UMD
        # bundles in vendor/butterchurn/. Copy them next to the HTML
        # in /tmp/ and reference via file:// - inlining tripped over
        # f-string brace escapes in the renderer template. Same-
        # origin file:// loads keep us out of CORS entirely. Missing
        # files just disable the visualizer at runtime.
        vendor_dir = Path(__file__).resolve().parent / "vendor" / "butterchurn"
        bc_lib = vendor_dir / "butterchurn.min.js"
        bc_presets = vendor_dir / "butterchurnPresets.min.js"
        bc_lib_uri = ""
        bc_presets_uri = ""
        try:
            if bc_lib.exists():
                dest = BROWSER_HTML_FILE.parent / "piframe_butterchurn.min.js"
                shutil.copyfile(bc_lib, dest)
                bc_lib_uri = dest.as_uri()
            if bc_presets.exists():
                dest = BROWSER_HTML_FILE.parent / "piframe_butterchurnPresets.min.js"
                shutil.copyfile(bc_presets, dest)
                bc_presets_uri = dest.as_uri()
        except OSError as exc:
            _log("butterchurn_vendor_copy_failed", error=str(exc))
        html = render_browser_html(
            rotation_degrees=self.rotation_degrees,
            show_hud=BROWSER_SHOW_HUD,
            transition_duration_ms=BROWSER_TRANSITION_DURATION_MS,
            state_file_uri=BROWSER_STATE_FILE.as_uri(),
            nas_root=NAS_ROOT,
            poll_ms=BROWSER_STATE_POLL_MS,
            event_endpoint=f"http://{BROWSER_EVENT_HOST}:{BROWSER_EVENT_PORT}/browser-event",
            butterchurn_lib_uri=bc_lib_uri,
            butterchurn_presets_uri=bc_presets_uri,
            visualizer_presets=AUDIO_VISUALIZER_PRESETS,
        )
        BROWSER_HTML_FILE.write_text(html, encoding="utf-8")

    def _start_browser(self) -> bool:
        _log(
            "browser_starting",
            cage=CAGE_BIN,
            chromium=CHROMIUM_BIN,
            html=BROWSER_HTML_FILE,
            state=BROWSER_STATE_FILE,
        )
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        _log("browser_runtime", xdg_runtime_dir=runtime_dir)
        _clear_stale_wayland_sockets(runtime_dir)
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log(
            BROWSER_LOG_FILE,
            max_bytes=BROWSER_LOG_MAX_BYTES,
            backups=BROWSER_LOG_BACKUPS,
        )
        chromium_args = [
            CHROMIUM_BIN,
            "--kiosk",
            "--ozone-platform=wayland",
            "--enable-features=UseOzonePlatform",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={BROWSER_PROFILE_DIR}",
            f"--disk-cache-dir={BROWSER_CACHE_DIR}",
            "--disk-cache-size=268435456",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--noerrdialogs",
            "--disable-background-networking",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-domain-reliability",
            "--disable-features=AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,OptimizationHints",
            "--disable-pings",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--metrics-recording-only",
            "--password-store=basic",
            "--allow-file-access-from-files",
            "--autoplay-policy=no-user-gesture-required",
            # WebGL flags: the second probe (after the first round of
            # diagnostics) confirmed Chromium can produce a hardware
            # WebGL context once we drop --use-gl=egl and let
            # Chromium's default ANGLE path negotiate. Keep
            # ignore-gpu-blocklist + gpu-rasterization since the Pi's
            # V3D driver shows up in the blocklist by default.
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
            # Drop both Chromium's frame-rate ceiling AND vsync. The
            # earlier vsync test found stormy / heavy presets reported
            # 58fps while visually running at ~10 - we've since
            # culled the worst offenders, so the remaining set runs
            # smoothly enough that the unbounded path gives a real
            # quality bump on the lighter presets.
            "--disable-frame-rate-limit",
            "--disable-gpu-vsync",
            BROWSER_HTML_FILE.as_uri(),
        ]
        wlrctl_path = shutil.which(WLRCTL_BIN)
        if wlrctl_path:
            park_cursor_cmd = shlex.join([wlrctl_path, "pointer", "move", "-100000", "100000"])
            launcher_script = "\n".join(
                [
                    "set -eu",
                    f"{shlex.join(chromium_args)} &",
                    "pid=$!",
                    "(",
                    "  sleep 1",
                    f"  {park_cursor_cmd} >/dev/null 2>&1 || true",
                    "  sleep 2",
                    f"  {park_cursor_cmd} >/dev/null 2>&1 || true",
                    ") &",
                    "wait \"$pid\"",
                ]
            )
            args = [CAGE_BIN, "-d", "--", "/bin/bash", "-lc", launcher_script]
        else:
            args = [CAGE_BIN, "-d", "--", *chromium_args]
        try:
            child_env = os.environ.copy()
            child_env["XDG_RUNTIME_DIR"] = runtime_dir
            child_env["WLR_NO_HARDWARE_CURSORS"] = "1"
            child_env["XCURSOR_THEME"] = "Adwaita"
            child_env["XCURSOR_SIZE"] = "1"
            browser_log = BROWSER_LOG_FILE.open("ab")
            self.process = subprocess.Popen(
                args,
                env=child_env,
                stdout=browser_log,
                stderr=browser_log,
            )
        except Exception as exc:
            _log("browser_start_failed", error=exc)
            self.process = None
            return False
        time.sleep(0.5)
        if self.process.poll() is not None:
            _log("browser_exited_early", code=self.process.returncode)
        return self.is_running

    def _ensure_running(self) -> bool:
        if self.is_running:
            return True
        return self._start_browser()

    @staticmethod
    def _pick_existing_idle_media(idle_url: str) -> str:
        idle_url = (idle_url or "").strip()
        if not idle_url:
            return ""
        candidate = idle_url.replace("\\", "/")
        candidates = [candidate]
        # The .jpg fallback only makes sense for image/video idle media;
        # an idle.html has no still-image twin to recover to.
        suffix = Path(candidate).suffix.lower()
        if suffix not in {".html", ".htm"}:
            try:
                candidates.append(str(Path(candidate).with_suffix(".jpg")))
            except Exception:
                pass
        for path in candidates:
            try:
                if path and Path(path).exists():
                    return path
            except Exception:
                continue
        return ""

    @staticmethod
    def _item_kind(path_str: str) -> str:
        # Audio files are tagged as "video" for the browser-side
        # renderer because Chromium's <video> element plays mp3/m4a/
        # flac/etc just fine - the audio routes through ALSA to HDMI
        # the same way embedded video audio does, and the existing
        # video-branch logic (onended -> advancePlaylist, repeat,
        # next/previous) works unchanged. The browser doesn't need to
        # know the file is audio-only; v1 ships with the default
        # black-frame visual and Phase 11 layers on a "now playing"
        # overlay. The orchestrator-side kind is still tracked
        # separately for status reporting.
        ext = Path(path_str.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        if ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
            return "video"
        if ext in {".html", ".htm"}:
            return "html"
        return "image"

    @staticmethod
    def _is_audio_only(path_str: str) -> bool:
        """True when the path is an audio-only file. Used by status
        reporting to distinguish 'audio_playing' from 'playing' (video)
        - the browser renderer treats them the same."""
        ext = Path(path_str.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        return ext in AUDIO_EXTENSIONS

    @staticmethod
    def _is_web_url(path_str: str) -> bool:
        lowered = (path_str or "").strip().lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @staticmethod
    def _make_banner(message: str, *, level: str = "warning") -> Dict[str, str]:
        return {"message": message, "level": level}

    def _classify_item_issue(self, path_str: str) -> Optional[Dict[str, str]]:
        if not path_str:
            return self._make_banner("Content unavailable")
        if self._is_web_url(path_str):
            return None
        normalized = _normalize_media_url(path_str) or path_str
        try:
            path = Path(normalized)
        except Exception:
            return self._make_banner("Content unavailable")

        try:
            if str(path).startswith(f"{NAS_ROOT}/") or str(path) == NAS_ROOT:
                if not os.path.ismount(NAS_ROOT):
                    return self._make_banner("NAS unavailable")
        except Exception:
            pass

        try:
            if not path.exists():
                return self._make_banner("Media file missing")
        except Exception:
            return self._make_banner("Content unavailable")
        return None

    def _make_item(self, path_str: str) -> Dict[str, Any]:
        ext = Path(path_str.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        return {
            "src": Path(path_str).as_uri(),
            "label": Path(path_str).name,
            "kind": self._item_kind(path_str),
            # is_audio is the renderer's hook for the audio visualization
            # overlay. The kind stays "video" so playback / next-prev /
            # repeat logic works unchanged through the existing
            # <video> path; this flag just tells the browser to paint
            # the audio overlay on top of the (otherwise blank) video.
            "is_audio": ext in AUDIO_EXTENSIONS,
        }

    def _write_state(self) -> None:
        tmp_path = BROWSER_STATE_FILE.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(self._state, handle)
        tmp_path.replace(BROWSER_STATE_FILE)

    def set_banner(self, message: Optional[str], *, level: str = "warning") -> None:
        with self._state_lock:
            self._state["banner"] = self._make_banner(message, level=level) if message else None
            self._write_state()

    def _set_control(self, action: str) -> None:
        self._control_token += 1
        self._state["control"] = {"action": action, "token": self._control_token}
        self._write_state()

    def _set_items_state(
        self,
        *,
        mode: str,
        playlist_name: str,
        items: List[str],
        interval: float,
        repeat: bool,
        loop: bool,
        shuffle: bool,
        idle_media: str,
    ) -> bool:
        if not self._ensure_running():
            _log(
                "renderer_apply_failed",
                mode=mode,
                playlist=playlist_name,
            )
            return False
        idle_item = self._make_item(idle_media) if idle_media else None
        banner = None
        for item in items:
            banner = self._classify_item_issue(item)
            if banner:
                break
        with self._state_lock:
            self._state.update(
                {
                    "mode": mode,
                    "playlist_name": playlist_name,
                    "items": [self._make_item(item) for item in items],
                    "repeat": repeat,
                    "loop": loop,
                    "interval": max(float(interval), BROWSER_MIN_ITEM_DURATION),
                    "shuffle": shuffle,
                    "transition": BROWSER_TRANSITION,
                    "transition_duration_ms": BROWSER_TRANSITION_DURATION_MS,
                    "video_fill_mode": BROWSER_VIDEO_FILL_MODE,
                    "control": None,
                    "volume": self._last_volume,
                    "muted": self._last_mute,
                    "idle_item": idle_item,
                    "banner": banner,
                }
            )
            self._write_state()
        _log(
            "browser_state_updated",
            mode=mode,
            playlist=playlist_name,
            items=len(items),
            repeat=repeat,
            loop=loop,
            interval=f"{float(interval):.3f}",
            video_fill_mode=BROWSER_VIDEO_FILL_MODE,
        )
        first_item = Path(items[0]).name if items else None
        _log(
            "renderer_transition",
            mode=mode,
            playlist=playlist_name,
            first_item=first_item,
            item_count=len(items),
            repeat=repeat,
            loop=loop,
            shuffle=shuffle,
            video_fill_mode=BROWSER_VIDEO_FILL_MODE,
        )
        return True

    def ensure_idle(self, idle_url: str) -> bool:
        if self.is_running and "(idle)" in self.last_command:
            return True
        return self.show_idle(idle_url)

    def show_idle(self, idle_url: str) -> bool:
        resolved = self._pick_existing_idle_media(idle_url)
        items = [resolved] if resolved else []
        if not self._set_items_state(
            mode="idle",
            playlist_name="Idle",
            items=items,
            interval=5.0,
            repeat=True,
            loop=True,
            shuffle=False,
            idle_media=resolved,
        ):
            return False
        self.slideshow_active = False
        self.slideshow_images = []
        self.last_command = f"browser idle {resolved or '(blank)'}"
        return True

    def play_single_video(self, url: str, *, loop: bool = True) -> bool:
        if not self._set_items_state(
            mode="single",
            playlist_name="Single Video",
            items=[url],
            interval=5.0,
            repeat=loop,
            loop=loop,
            shuffle=False,
            idle_media=self._pick_existing_idle_media(_default_idle_media()),
        ):
            return False
        self.slideshow_active = False
        self.slideshow_images = []
        self.last_command = f"browser single-video {Path(url).name} loop={loop}"
        return True

    def play_slideshow(
        self, items: List[str], interval: float, *, shuffle: bool = False
    ) -> bool:
        if not items:
            return False
        play_items = list(items)
        if shuffle:
            random.shuffle(play_items)
        if not self._set_items_state(
            mode="playlist",
            playlist_name="Mixed Playlist",
            items=play_items,
            interval=interval,
            repeat=True,
            loop=False,
            shuffle=shuffle,
            idle_media=self._pick_existing_idle_media(_default_idle_media()),
        ):
            return False
        self.slideshow_active = True
        self.slideshow_images = list(play_items)
        self.last_command = (
            f"browser mixed-playlist count={len(play_items)} interval={float(interval):.3f} shuffle={shuffle}"
        )
        return True

    def play_video_playlist(
        self,
        items: List[str],
        *,
        repeat: bool = True,
    ) -> bool:
        if not items:
            return False
        if not self._set_items_state(
            mode="playlist",
            playlist_name="Video Playlist",
            items=items,
            interval=5.0,
            repeat=repeat,
            loop=False,
            shuffle=False,
            idle_media=self._pick_existing_idle_media(_default_idle_media()),
        ):
            return False
        self.slideshow_active = True
        self.slideshow_images = list(items)
        self.last_command = f"browser video-playlist count={len(items)} repeat={repeat}"
        return True

    def playlist_next(self) -> None:
        if self.is_running:
            self._set_control("next")

    def playlist_previous(self) -> None:
        if self.is_running:
            self._set_control("previous")

    def toggle_pause(self) -> None:
        if self.is_running:
            self._set_control("pause")

    def set_volume(self, level: float) -> None:
        self._last_volume = self._clamp_volume(level)
        self._last_mute = self._last_volume <= 0
        with self._state_lock:
            self._state["volume"] = self._last_volume
            self._state["muted"] = self._last_mute
            self._write_state()
        self._persist_audio_state()

    def set_video_mute_override(self, mute: bool) -> None:
        """Surface the override-mute flag for the renderer's <video>
        element AND mute Chromium's PipeWire/PulseAudio stream
        directly so the companion mpv is audible underneath.

        Why both: setting <video>.muted in JS only stops Chromium
        from sending samples FROM that element, but Chromium's
        per-process audio node stays connected to the PA sink and
        whatever it's playing (including the muted-but-still-decoded
        audio track) competes with mpv's stream. Muting at the
        sink-input level cuts Chromium out of the mix cleanly so the
        companion is audible.

        state.muted is the user mute toggle and shouldn't be touched
        here. video_mute_override is its own flag, ORed onto state.
        muted in startFromState + applyLiveAudioState."""
        if not self._ensure_running():
            return
        with self._state_lock:
            self._state["video_mute_override"] = bool(mute)
            self._write_state()
        try:
            _set_chromium_sink_input_mute(bool(mute))
        except Exception as exc:
            _log("companion_chromium_mute_failed", error=str(exc))

    def set_visualizer(self, choice: str) -> None:
        """Per-playlist visualizer pick: 'none' / 'random' / preset name.

        Stamps the choice onto the browser state file so the renderer's
        audioVis module reads it on the next play. Default ('random')
        preserves the existing cycle-every-5s behavior. 'none' skips
        the overlay entirely. Anything else is treated as a literal
        preset name to lock onto.
        """
        if not self._ensure_running():
            return
        normalized = (choice or "random").strip() or "random"
        with self._state_lock:
            self._state["visualizer"] = normalized
            self._write_state()


class MpvCompanion:
    """Sidecar mpv process for companion audio playback.

    Why a separate process: Chromium on Pi keeps the audio device
    locked while a <video> is playing, even with muted+volume=0. An
    in-page <audio> element can't claim HDMI in that state, so the
    companion never produces sound when override-mute is on. Spinning
    up a separate mpv process opens its own audio stream through
    PulseAudio/PipeWire (or ALSA dmix), which the audio server mixes
    with the Chromium output.

    Lifecycle: spawned on first load(), kept warm afterward, IPC over
    a Unix socket. Shutdown() during PiFrameClient teardown.

    Mirrors vwm/audio_companion.py from the woozlescape repo - same
    one-mpv-per-pipe pattern, just on Linux instead of Windows."""

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._last_volume: float = 75.0
        self._last_muted: bool = False
        # Tracked for the toggle path - the WS 'pause' command is a
        # toggle (browser flips its own state on each press), so the
        # companion mirrors by flipping this flag too.
        self._paused: bool = False

    def _is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def _ensure_running(self) -> bool:
        with self._lock:
            if self._is_running():
                return True
            # Sweep any orphan companion mpv that might still hold the
            # socket. Without this, a previously-crashed mpv keeps the
            # socket file alive and the new mpv exits with "Couldn't
            # bind socket" - mirrors the orphan-sweep pattern in
            # vwm/audio_companion.py.
            try:
                if COMPANION_MPV_SOCKET.exists():
                    COMPANION_MPV_SOCKET.unlink()
            except Exception:
                pass
            mpv_path = shutil.which(COMPANION_MPV_BIN)
            if not mpv_path:
                _log("companion_mpv_missing", binary=COMPANION_MPV_BIN)
                return False
            args = [
                mpv_path,
                "--no-config",
                "--idle=yes",
                "--no-video",
                "--no-osc",
                "--no-osd-bar",
                "--no-input-default-bindings",
                "--ytdl=no",
                "--cache=no",
                "--keep-open=yes",
                # Force the PulseAudio/PipeWire output and an
                # explicit default-sink routing. Without this,
                # PipeWire's module-stream-restore remembers the
                # first sink the stream connected to (which on a
                # cold-boot Pi is sometimes no sink at all) and
                # pins all future mpv companion runs to that
                # broken state - the stream connects, plays
                # decoded audio, but never reaches the HDMI sink.
                "--ao=pulse",
                "--audio-device=pulse",
                # Clear any role-based stream-restore memory so the
                # companion always uses the current default sink.
                "--audio-stream-silence=no",
                f"--input-ipc-server={COMPANION_MPV_SOCKET}",
                "--msg-level=all=info",
                f"--log-file={COMPANION_MPV_LOG_FILE}",
            ]
            try:
                self.process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as exc:
                _log("companion_mpv_spawn_failed", error=str(exc))
                return False
            # Wait briefly for the socket to come up so the next
            # send_command() doesn't race the spawn.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if COMPANION_MPV_SOCKET.exists():
                    _log("companion_mpv_started", pid=self.process.pid)
                    return True
                if self.process.poll() is not None:
                    _log("companion_mpv_exited_during_spawn", code=self.process.returncode)
                    self.process = None
                    return False
                time.sleep(0.05)
            _log("companion_mpv_socket_timeout")
            return False

    def _send_command(self, cmd: List[Any]) -> bool:
        """One-shot JSON command send via Unix socket. Best-effort -
        callers shouldn't depend on the return value beyond logging."""
        if not self._ensure_running():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(str(COMPANION_MPV_SOCKET))
            payload = json.dumps({"command": cmd}).encode("utf-8") + b"\n"
            sock.sendall(payload)
            try:
                sock.recv(4096)  # drain reply, ignore content
            except (socket.timeout, OSError):
                pass
            sock.close()
            return True
        except Exception as exc:
            _log("companion_mpv_ipc_failed", cmd=str(cmd[:1]), error=str(exc))
            return False

    def load(self, items: List[str], *, repeat: bool = True) -> bool:
        """Replace the queue with `items` and start playback. Mirrors
        the loadfile-replace + N-1 appends pattern."""
        items = [s for s in items if s]
        if not items:
            self.stop()
            return False
        if not self._ensure_running():
            return False
        # Prime mpv: set loop + reset state, then load the queue.
        self._send_command(["set_property", "loop-file", "no"])
        self._send_command(["set_property", "loop-playlist", "inf" if repeat else "no"])
        self._send_command(["set_property", "volume", float(self._last_volume)])
        self._send_command(["set_property", "mute", bool(self._last_muted)])
        # Fresh content always starts unpaused. Without this, a stale
        # _paused=True from an earlier pause-toggle would silence the
        # newly-loaded queue and the operator would think nothing was
        # playing. The pause command also wouldn't fix it because the
        # toggle logic flips it to True again.
        self._paused = False
        self._send_command(["set_property", "pause", False])
        self._send_command(["loadfile", items[0], "replace"])
        for path in items[1:]:
            self._send_command(["loadfile", path, "append"])
        _log("companion_mpv_loaded", count=len(items), repeat=repeat)
        return True

    def stop(self) -> None:
        """Drop the queue + leave mpv idle. Keeps the process warm
        for the next load (avoids spawn cost)."""
        if not self._is_running():
            return
        self._send_command(["stop"])
        _log("companion_mpv_stopped")

    def set_volume(self, level: float) -> None:
        """Forward a volume change to the running mpv. mpv expects
        0-100. The companion follows the surface volume so the
        operator's slider always controls what they hear."""
        try:
            v = max(0.0, min(100.0, float(level)))
        except (TypeError, ValueError):
            return
        self._last_volume = v
        if self._is_running():
            self._send_command(["set_property", "volume", v])

    def set_muted(self, muted: bool) -> None:
        self._last_muted = bool(muted)
        if self._is_running():
            self._send_command(["set_property", "mute", bool(muted)])

    def toggle_pause(self) -> None:
        """Flip mpv's `pause` property. Mirrors the browser's pause
        toggle so the companion stops/resumes alongside the visual.
        No-op when mpv isn't running (companion not started yet)."""
        if not self._is_running():
            return
        self._paused = not self._paused
        self._send_command(["set_property", "pause", bool(self._paused)])

    def set_paused(self, paused: bool) -> None:
        """Explicit-state variant of toggle_pause for callers that
        already know which state to land in (e.g. recovering from a
        drift between browser-toggle count and companion state)."""
        self._paused = bool(paused)
        if self._is_running():
            self._send_command(["set_property", "pause", bool(paused)])

    def shutdown(self) -> None:
        """Kill the mpv process entirely. Used during PiFrameClient
        teardown so we don't leave orphan companions running."""
        with self._lock:
            if not self._is_running():
                self.process = None
                return
            self._send_command(["quit"])
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            self.process = None
            try:
                if COMPANION_MPV_SOCKET.exists():
                    COMPANION_MPV_SOCKET.unlink()
            except Exception:
                pass


class _BrowserEventState:
    """Thread-safe holder for the latest events the browser pushes back to
    Python. Tracks slideshow index + paused flag so the status payload to
    the manager reflects the kiosk's actual on-screen state. Owns a
    wakeup Event the status loop waits on so state changes propagate to
    the manager within ~one round-trip instead of waiting for the next
    periodic tick."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slideshow_index: Optional[int] = None
        self._slideshow_index_at: float = 0.0
        self._paused: bool = False
        self.wakeup = threading.Event()

    def set_slideshow_index(self, index: int) -> None:
        with self._lock:
            changed = self._slideshow_index != int(index)
            self._slideshow_index = int(index)
            self._slideshow_index_at = time.time()
        if changed:
            self.wakeup.set()

    def clear_slideshow_index(self) -> None:
        with self._lock:
            self._slideshow_index = None
            self._slideshow_index_at = 0.0
        self.wakeup.set()

    def slideshow_index(self) -> Optional[int]:
        with self._lock:
            return self._slideshow_index

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            changed = self._paused != bool(paused)
            self._paused = bool(paused)
        if changed:
            self.wakeup.set()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused


# Module-level singleton so the HTTP handler (which has no app context)
# can stash events for the PiFrameClient instance to read.
BROWSER_EVENT_STATE = _BrowserEventState()


class _BrowserEventHandler(BaseHTTPRequestHandler):
    """Tiny POST endpoint the kiosk fetch()es on every slide change."""

    # Quiet the default access log; chatty at 1+ POST/sec per slide.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        return

    def _send(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        # CORS preflight - browser fetch() to a different origin (file:// page
        # to http://127.0.0.1) treats this as cross-origin.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/browser-event":
            self._send(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        kind = (payload.get("type") or "").strip()
        if kind == "slideshow_index":
            try:
                idx = int(payload.get("index"))
            except (TypeError, ValueError):
                idx = -1
            if idx >= 0:
                BROWSER_EVENT_STATE.set_slideshow_index(idx)
        elif kind == "pause_state":
            BROWSER_EVENT_STATE.set_paused(bool(payload.get("paused")))
        elif kind == "audio_visualizer_status":
            # Forward to the regular log so init failures / runtime
            # warnings show up in journalctl instead of being swallowed
            # by the kiosk Chromium (which has no console output by
            # default).
            _log(
                "audio_visualizer_status",
                stage=str(payload.get("stage") or ""),
                detail=str(payload.get("detail") or "")[:200],
                has_butterchurn=bool(payload.get("has_butterchurn")),
                has_presets=bool(payload.get("has_presets")),
            )
        # Always return 204; CORS header lets the browser stop spamming
        # console errors when it crosses the file:// -> http:// boundary.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()


class PiFrameClient:
    """Minimal WebSocket-to-browser bridge with basic status/volume updates."""

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.ws_connection: Optional[websocket.WebSocketApp] = None
        self.renderer = BrowserController()
        self.companion_mpv = MpvCompanion()
        self.current_playlist_name: str = ""
        self.current_playlist_id: str = ""
        self.current_video: str = ""
        self.current_slideshow: List[str] = []
        self.current_interval: float = 0.0
        self.current_shuffle: bool = False
        # Wall-clock timestamp (epoch seconds, float) when the current
        # slideshow started rotating. Manager UI uses this + current_interval
        # to project which slide is on screen right now without needing the
        # browser to push per-rotation events.
        self.slideshow_started_at: Optional[float] = None
        self.playback_state: str = "stopped"
        self.status_thread: Optional[threading.Thread] = None
        self.status_running = False
        # Loopback HTTP server the kiosk POSTs slide-change events to.
        # Started alongside the status loop in _start_status_updates().
        self.browser_event_server: Optional[HTTPServer] = None
        self.browser_event_thread: Optional[threading.Thread] = None
        self.system_info = get_system_info()
        # Computed once at boot; surfaced in the status heartbeat so the
        # server's System tab can show per-device git SHA. After an
        # update_self -> systemctl restart, the new SHA shows up
        # automatically on the next reconnect.
        self.client_version: Optional[str] = _read_client_version()
        # Marker file written by update.sh after a successful pull.
        # Read once and consumed (deleted) so we don't keep reporting
        # the same update across every restart - it should only land
        # in the heartbeat once, on the boot that immediately follows
        # the update.
        self.last_update: Optional[Dict[str, Any]] = _read_and_consume_last_update()

    def run(self) -> None:
        mpv_found = shutil.which(COMPANION_MPV_BIN)
        audio_server = _detect_audio_server()
        _log(
            "client_starting",
            client_id=self.config.client_id,
            name=self.config.name,
            server=self.config.server,
            group=self.config.group,
            companion_mpv=mpv_found or "missing",
            audio_server=audio_server,
        )
        if audio_server == "alsa-only":
            _log(
                "audio_mixer_warning",
                msg=(
                    "Neither PulseAudio nor PipeWire detected. Bare ALSA "
                    "is single-stream: the audio companion will not be "
                    "audible while a Chromium <video> is playing through "
                    "the same device. Install pipewire+pipewire-pulse OR "
                    "configure ~/.asoundrc with a dmix plugin."
                ),
            )
        while True:
            try:
                app = websocket.WebSocketApp(
                    self.config.server,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                app.on_open = self.on_open
                self.ws_connection = app
                app.run_forever()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                _log("connection_error", error=exc)
            self.ws_connection = None
            _log("reconnecting", delay_seconds=5)
            time.sleep(5)

    def on_open(self, ws) -> None:  # pylint: disable=unused-argument
        payload = {
            "type": "register",
            "cmd": "register",
            "client_id": self.config.client_id,
            "name": self.config.name,
            "group": self.config.group,
            "os": self.system_info.get("os", "Unknown"),
            "mac_address": self.system_info.get("mac_address", "Unknown"),
            "ip_address": self.system_info.get("ip_address", "Unknown"),
        }
        try:
            metrics = _collect_system_metrics()
            if metrics:
                payload["system_metrics"] = metrics
                payload["hardware"] = metrics
            ws.send(json.dumps(payload))
            _log("registered", name=self.config.name)
        except Exception as exc:
            _log("register_failed", error=exc)
        self._start_status_updates()
        self.renderer.set_banner(None)
        # Ensure the screen is always owned by the browser renderer.
        self.renderer.ensure_idle(self._get_idle_media())

    def on_message(self, ws, message: str) -> None:  # pylint: disable=unused-argument
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            _log("invalid_json", payload=message)
            return

        params = data.get("params") or {}
        cmd = (data.get("cmd") or data.get("type") or "").strip()
        handler = {
            "play": self._handle_play,
            "video_playlist": self._handle_video_playlist,
            "audio_playlist": self._handle_audio_playlist,
            "pause": self._handle_pause,
            "next": self._handle_next,
            "previous": self._handle_previous,
            "stop": self._handle_stop,
            "slideshow": self._handle_slideshow,
            "volume": self._handle_volume,
            "update_self": self._handle_update_self,
        }.get(cmd)
        if handler:
            handler(data, params)

    @staticmethod
    def _playlist_context(
        data: Dict[str, Any], params: Dict[str, Any]
    ) -> tuple[str, str]:
        return (
            params.get("playlist_name") or data.get("playlist_name", ""),
            params.get("playlist_id") or data.get("playlist_id", ""),
        )

    @staticmethod
    def _normalized_items(*sources: Any) -> List[str]:
        for source in sources:
            if source:
                return [_normalize_media_url(item) for item in source if item]
        return []

    def _handle_play(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        url = _normalize_media_url(data.get("url") or params.get("url"))
        loop_flag = _coerce_bool(params.get("loop", data.get("loop", True)), default=True)
        self.current_playlist_name = playlist_name
        self.current_playlist_id = playlist_id
        if not url:
            return
        _log(
            "play_command",
            item=Path(url).name,
            loop=loop_flag,
            playlist=playlist_name or "",
            playlist_id=playlist_id or "",
        )
        self.current_video = url
        self.current_slideshow = []
        self.playback_state = "playing"
        if self.renderer.play_single_video(url, loop=loop_flag):
            self._send_render_command()

    def _handle_video_playlist(
        self, data: Dict[str, Any], params: Dict[str, Any]
    ) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        items = self._normalized_items(
            params.get("items"),
            params.get("videos"),
            data.get("items"),
            data.get("videos"),
        )
        repeat = _coerce_bool(params.get("repeat", data.get("repeat", True)), default=True)
        self.current_playlist_name = playlist_name
        self.current_playlist_id = playlist_id
        _log(
            "video_playlist_command",
            items=len(items),
            repeat=repeat,
            playlist=playlist_name or "",
            playlist_id=playlist_id or "",
        )
        handled = False
        if len(items) == 1:
            handled = self.renderer.play_single_video(items[0], loop=repeat)
        elif items:
            handled = self.renderer.play_video_playlist(items, repeat=repeat)
        if handled:
            self.current_slideshow = list(items)
            self.current_video = items[0] if len(items) == 1 else ""
            self.playback_state = "playing" if len(items) == 1 else "slideshow"
            # Video playlists rotate too, but the manager doesn't render
            # per-video tile previews from the playlist - leave the
            # slideshow timer null for video playback.
            self.slideshow_started_at = None
            BROWSER_EVENT_STATE.clear_slideshow_index()
            BROWSER_EVENT_STATE.set_paused(False)
            self._send_render_command()

    def _handle_audio_playlist(
        self, data: Dict[str, Any], params: Dict[str, Any]
    ) -> None:
        """Audio playlist dispatch. Two distinct cases keyed by the
        `is_companion` flag:

        - is_companion=False (default): direct audio playback.
          Replaces the visual content with the audio output.
          Treats audio files the same way the video-playlist path
          treats videos - the renderer's play_video_playlist accepts
          any media URL Chromium can play through its <video> element,
          which includes the common audio formats.

        - is_companion=True: background audio that plays in parallel
          with whatever visual content is on screen. Routed to the
          renderer's separate <audio> element so the visual content
          (image or silent video) keeps playing. Empty items signals
          "stop the companion."

        The orchestrator-side `target` field (default "hdmi") is
        reserved for future per-target audio routing; v1 always plays
        through the browser's default ALSA -> HDMI audio path.
        """
        playlist_name, playlist_id = self._playlist_context(data, params)
        items = self._normalized_items(
            params.get("items"),
            data.get("items"),
        )
        repeat = _coerce_bool(params.get("repeat", data.get("repeat", True)), default=True)
        target = (params.get("target") or data.get("target") or "hdmi")
        visualizer = (
            params.get("visualizer")
            or data.get("visualizer")
            or "random"
        )
        if not isinstance(visualizer, str):
            visualizer = "random"
        is_companion = _coerce_bool(
            params.get("is_companion", data.get("is_companion", False)),
            default=False,
        )
        mute_visual = _coerce_bool(
            params.get("mute_visual", data.get("mute_visual", False)),
            default=False,
        )

        if is_companion:
            self._set_companion_state(items, repeat=repeat, mute_visual=mute_visual)
            _log(
                "audio_companion_command",
                items=len(items),
                repeat=repeat,
                mute_visual=mute_visual,
                target=target,
                playlist=playlist_name or "",
                playlist_id=playlist_id or "",
            )
            return

        self.current_playlist_name = playlist_name
        self.current_playlist_id = playlist_id
        _log(
            "audio_playlist_command",
            items=len(items),
            repeat=repeat,
            target=target,
            visualizer=visualizer,
            playlist=playlist_name or "",
            playlist_id=playlist_id or "",
        )
        # Apply the visualizer pick BEFORE starting playback so the
        # renderer's state file already carries the right value when
        # the new audio item activates and the visualizer hooks fire.
        self.renderer.set_visualizer(visualizer)
        handled = False
        if len(items) == 1:
            handled = self.renderer.play_single_video(items[0], loop=repeat)
        elif items:
            handled = self.renderer.play_video_playlist(items, repeat=repeat)
        if handled:
            self.current_slideshow = list(items)
            self.current_video = items[0] if len(items) == 1 else ""
            # Distinct playback_state value so the orchestrator can
            # distinguish "currently playing audio" from "currently
            # playing video" without sniffing the file extension. The
            # browser renderer doesn't need to know the difference.
            self.playback_state = "audio_playing"
            self.slideshow_started_at = None
            BROWSER_EVENT_STATE.clear_slideshow_index()
            BROWSER_EVENT_STATE.set_paused(False)
            # Starting direct audio playback supersedes any companion
            # that was running - the surface is now an audio device.
            self._set_companion_state([], repeat=False, mute_visual=False)
            self._send_render_command()

    def _set_companion_state(
        self, items: List[str], *, repeat: bool, mute_visual: bool
    ) -> None:
        """Drive the audio companion. Audio plays through the
        MpvCompanion sidecar (separate process - Chromium on Pi
        locks the audio device while a video plays, so an in-page
        <audio> element can't claim HDMI). The renderer's
        video_mute_override flag handles the visual side of the
        override-mute path."""
        if items:
            self.companion_mpv.load(items, repeat=repeat)
        else:
            self.companion_mpv.stop()
        # Visual side: video_mute_override applies only while companion
        # is actually playing AND mute_visual is on. Off otherwise so
        # the video resumes audio cleanly when override flips off or
        # the companion is cleared.
        self.renderer.set_video_mute_override(bool(items) and bool(mute_visual))

    def _handle_pause(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        _log("pause_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
        # Toggle is browser-side; the real paused/playing state comes back
        # via the /browser-event channel and overrides playback_state in
        # the next status_update. self.playback_state stays as the
        # underlying media kind (slideshow / playing / stopped).
        self.renderer.toggle_pause()
        self._send_render_command(action="pause")
        # Companion plays alongside visual content, so it pauses with
        # the visual. Toggle mirrors the browser's toggle - both flip
        # on each press, so they stay in lockstep without us needing
        # to know the resulting state.
        try:
            self.companion_mpv.toggle_pause()
        except Exception as exc:
            _log("companion_pause_error", error=str(exc))

    def _handle_next(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        _log("next_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
        self.renderer.playlist_next()

    def _handle_previous(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        _log(
            "previous_command", playlist=playlist_name or "", playlist_id=playlist_id or ""
        )
        self.renderer.playlist_previous()

    def _handle_stop(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        is_companion = _coerce_bool(
            params.get("is_companion", data.get("is_companion", False)),
            default=False,
        )
        _log(
            "stop_command",
            playlist=playlist_name or "",
            playlist_id=playlist_id or "",
            is_companion=is_companion,
        )
        if is_companion:
            # Companion-only stop: halt the background audio without
            # disturbing the visual. Used when the routing rule flips
            # from "companion plays" to "companion off" mid-content
            # (e.g. transitioning from image to video-with-audio).
            self._set_companion_state([], repeat=False, mute_visual=False)
            return
        if self.renderer.last_command:
            self._send_render_command(action="stop")
        self.renderer.show_idle(self._get_idle_media())
        self.current_video = ""
        self.current_slideshow = []
        self.playback_state = "stopped"
        self.slideshow_started_at = None
        BROWSER_EVENT_STATE.clear_slideshow_index()
        BROWSER_EVENT_STATE.set_paused(False)
        # Full stop also halts any running companion - the surface is
        # idle now.
        self._set_companion_state([], repeat=False, mute_visual=False)

    def _handle_slideshow(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        playlist_name, playlist_id = self._playlist_context(data, params)
        images = self._normalized_items(params.get("images"), data.get("images"))
        interval_raw = params.get("interval", data.get("interval", 5))
        shuffle_flag = _coerce_bool(
            params.get("shuffle", data.get("shuffle", False)), default=False
        )
        _log(
            "slideshow_command",
            images=len(images),
            interval=params.get("interval", data.get("interval", "?")),
            shuffle=shuffle_flag,
            playlist_id=playlist_id or "",
        )
        try:
            interval = float(interval_raw)
        except (TypeError, ValueError):
            interval = 5.0
        self.current_playlist_name = playlist_name
        self.current_playlist_id = playlist_id
        if (
            not shuffle_flag
            and not self.current_shuffle
            and self.playback_state == "slideshow"
            and self.renderer.slideshow_active
            and images == self.current_slideshow
            and float(interval) == float(self.current_interval)
            and playlist_id == self.current_playlist_id
        ):
            return
        if images and self.renderer.play_slideshow(images, interval, shuffle=shuffle_flag):
            self.current_slideshow = list(self.renderer.slideshow_images) or images
            self.current_video = ""
            self.playback_state = "slideshow"
            self.current_interval = float(interval)
            self.current_shuffle = shuffle_flag
            self.slideshow_started_at = time.time()
            # Reset to None - browser will POST the actual index as soon as
            # it shows the first slide, so the manager doesn't lock onto
            # the previous slideshow's last-known index during the gap.
            BROWSER_EVENT_STATE.clear_slideshow_index()
            BROWSER_EVENT_STATE.set_paused(False)
            self._send_render_command()

    def _handle_volume(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:
        level = params.get("level", data.get("level", 75))
        try:
            level = float(level)
        except (TypeError, ValueError):
            level = 75
        _log("volume_command", level=level)
        self.renderer.set_volume(level)
        # Companion mpv tracks the same volume so the operator's slider
        # controls what they hear regardless of which output is active.
        self.companion_mpv.set_volume(level)
        self.companion_mpv.set_muted(level <= 0)
        # Immediately wake the heartbeat loop so the orchestrator sees
        # the new volume in ~50ms (network round-trip) instead of
        # waiting up to STATUS_UPDATE_INTERVAL seconds. Same channel
        # the heartbeat already uses; just ticks it now.
        try:
            BROWSER_EVENT_STATE.wakeup.set()
        except Exception:
            pass

    def _handle_update_self(self, data: Dict[str, Any], params: Dict[str, Any]) -> None:  # pylint: disable=unused-argument
        """Server-pushed in-place update. Spawns `update.sh` from the
        repo root (which does git fetch + reset --hard origin/main, then
        execs `sudo systemctl restart piframe-client`). The current
        process gets killed by the restart and systemd respawns us with
        the new code. If `update.sh` is missing or git is dirty the
        script exits non-zero; we log it and stay running on the old
        version."""
        repo_dir = Path(__file__).resolve().parent
        script = repo_dir / "update.sh"
        if not script.exists():
            _log("update_self_missing_script", path=str(script))
            return
        _log("update_self_starting", repo=str(repo_dir))
        try:
            # Detached so we don't block the WS reader thread, and so
            # the systemctl restart doesn't have to kill our parent
            # before it can exec.
            subprocess.Popen(
                ["/bin/bash", str(script)],
                cwd=str(repo_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            _log("update_self_spawn_failed", error=exc)

    def on_close(self, ws, close_status_code, close_msg) -> None:  # pylint: disable=unused-argument
        _log("websocket_closed", code=close_status_code, message=close_msg)
        self.ws_connection = None
        self._stop_status_updates()
        self.renderer.show_idle(self._get_idle_media())
        self.renderer.set_banner("Server disconnected", level="error")

    def on_error(self, ws, error: Exception) -> None:  # pylint: disable=unused-argument
        _log("websocket_error", error=error)

    def shutdown(self) -> None:
        self.renderer.shutdown()
        try:
            self.companion_mpv.shutdown()
        except Exception:
            pass
        self._stop_status_updates()
        self._stop_browser_event_server()

    @staticmethod
    def _get_idle_media() -> str:
        idle_media = os.environ.get(IDLE_MEDIA_ENV) or _default_idle_media()
        return _normalize_media_url(idle_media) or idle_media

    def _send_render_command(self, action: Optional[str] = None) -> None:
        """Notify manager of the exact render command used for playback."""
        if not self.ws_connection or not self.renderer.last_command:
            return
        try:
            payload = {
                "type": "render_command",
                "cmd": "render_command",
                "command": self.renderer.last_command,
                "action": action or "update",
            }
            self.ws_connection.send(json.dumps(payload))
        except Exception:
            pass

    def _start_status_updates(self) -> None:
        if self.status_thread and self.status_thread.is_alive():
            return
        self._start_browser_event_server()
        self.status_running = True
        self.status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self.status_thread.start()

    def _stop_status_updates(self) -> None:
        self.status_running = False
        # Kick the loop out of its wakeup.wait() instead of letting it
        # idle for up to STATUS_UPDATE_INTERVAL seconds.
        BROWSER_EVENT_STATE.wakeup.set()
        if self.status_thread and self.status_thread.is_alive():
            self.status_thread.join(timeout=2.0)
        self.status_thread = None

    def _start_browser_event_server(self) -> None:
        if getattr(self, "browser_event_server", None) is not None:
            return
        try:
            server = HTTPServer((BROWSER_EVENT_HOST, BROWSER_EVENT_PORT), _BrowserEventHandler)
        except OSError as exc:
            _log("browser_event_server_bind_failed", error=str(exc), port=BROWSER_EVENT_PORT)
            self.browser_event_server = None
            self.browser_event_thread = None
            return
        self.browser_event_server = server
        self.browser_event_thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="browser-event-server"
        )
        self.browser_event_thread.start()
        _log("browser_event_server_started", host=BROWSER_EVENT_HOST, port=BROWSER_EVENT_PORT)

    def _stop_browser_event_server(self) -> None:
        server = getattr(self, "browser_event_server", None)
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self.browser_event_server = None
        thread = getattr(self, "browser_event_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self.browser_event_thread = None

    def _status_loop(self) -> None:
        while self.status_running:
            # Wake on either the periodic tick OR an explicit event from
            # the browser (pause toggle, slide change). Lets state changes
            # land on the manager within ~one network round-trip instead
            # of waiting up to STATUS_UPDATE_INTERVAL seconds.
            BROWSER_EVENT_STATE.wakeup.wait(timeout=STATUS_UPDATE_INTERVAL)
            BROWSER_EVENT_STATE.wakeup.clear()
            if not self.status_running or not self.ws_connection:
                continue
            # Pick up an update marker dropped by update.sh while we're
            # running. The boot path already reads + consumes the file
            # in __init__, but the noop case (before==after) doesn't
            # restart the service, so the only way the marker reaches
            # the server is for the running process to notice it.
            if self.last_update is None:
                marker = _read_and_consume_last_update()
                if marker is not None:
                    self.last_update = marker
            # If the browser reports paused, surface that to the manager
            # while leaving the underlying media kind in self.playback_state
            # untouched. Stopped state is never overridden (a paused flag
            # left over from a prior slideshow shouldn't mask "stopped").
            # audio_playing belongs in the pause-eligible set too -
            # without it, the orchestrator's tile flips back to
            # "playing" on the next heartbeat tick (~2-3s) even when
            # the browser is genuinely paused.
            reported_paused = BROWSER_EVENT_STATE.is_paused()
            effective_state = (
                "paused"
                if reported_paused and self.playback_state in ("playing", "slideshow", "audio_playing")
                else self.playback_state
            )
            # Pull current audio level from the renderer. The Pi tracks
            # _last_volume / _last_mute on every set_volume / set_muted
            # call; surfacing them in the heartbeat lets the
            # orchestrator persist + display real values across page
            # refreshes (without these the UI shows default 100% / 50%
            # for the 0-2s window after a refresh).
            try:
                renderer_volume = float(self.renderer._last_volume)
            except Exception:
                renderer_volume = None
            try:
                renderer_muted = bool(self.renderer._last_mute)
            except Exception:
                renderer_muted = None
            status: Dict[str, Any] = {
                "current_video": self.current_video,
                "current_slideshow": self.current_slideshow,
                "current_playlist": self.current_playlist_name,
                "current_playlist_id": self.current_playlist_id,
                "playback_state": effective_state,
                "shuffle": self.current_shuffle,
                "volume": renderer_volume,
                "muted": renderer_muted,
                "slideshow_active": self.renderer.slideshow_active,
                "last_render_cmd": self.renderer.last_command or None,
                # Manager UI projects the current slide locally from these
                # two fields: index = floor((now - started_at) / interval) % length
                "slideshow_started_at": (
                    int(self.slideshow_started_at * 1000)
                    if self.slideshow_started_at is not None
                    else None
                ),
                "current_interval": self.current_interval if self.current_interval > 0 else None,
                # Authoritative index reported by the browser via the local
                # event server; manager UI prefers this over its own
                # time-based projection. None when browser hasn't pushed
                # yet (older kiosk template, or no slideshow active).
                "slideshow_index": BROWSER_EVENT_STATE.slideshow_index(),
                "client_version": self.client_version,
                "last_update": self.last_update,
                # Bundle-curated visualizer preset names so the
                # orchestrator can populate a per-playlist picker.
                # Static across the lifetime of this client; cheap to
                # ship every heartbeat (small list).
                "visualizer_presets": list(AUDIO_VISUALIZER_PRESETS),
            }
            metrics = _collect_system_metrics()
            if metrics:
                status["system_metrics"] = metrics
                status.setdefault("hardware", metrics)
            snapshot = {"type": "status_update", "status": status}
            try:
                self.ws_connection.send(json.dumps(snapshot))
            except Exception:
                pass
            else:
                # One-shot delivery: drop the marker after a successful
                # send so a second update click doesn't see this stale
                # record and resolve the new attempt against an old SHA.
                # The server stamps update_result + last_updated_at on
                # receipt, so we don't need to keep echoing it.
                if self.last_update is not None:
                    self.last_update = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> ClientConfig:
    parser = argparse.ArgumentParser(description="Minimal PiFrame Client")
    parser.add_argument("--server", default=SERVER_DEFAULT, help="WebSocket server URL")
    parser.add_argument(
        "--id",
        dest="client_id",
        default=socket.gethostname(),
        help="Client ID (default: hostname)",
    )
    parser.add_argument(
        "--name", default=None, help="Client display name (default: client ID)"
    )
    parser.add_argument("--group", default="default", help="Client group name")
    args = parser.parse_args()
    return ClientConfig(
        server=args.server,
        client_id=args.client_id,
        name=args.name or args.client_id,
        group=args.group,
    )


def main() -> None:
    config = parse_args()
    client = PiFrameClient(config)
    try:
        client.run()
    except KeyboardInterrupt:
        _log("client_stopping")
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
