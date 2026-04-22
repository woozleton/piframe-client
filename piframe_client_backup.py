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
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import websocket

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

# ---------------------------------------------------------------------------
# Configuration defaults (env overrides where noted)
# ---------------------------------------------------------------------------
STATUS_UPDATE_INTERVAL = 5  # seconds
SERVER_DEFAULT = os.environ.get("PIFRAME_SERVER", "ws://192.168.100.100:8080/ws")
NAS_ROOT = os.environ.get("PIFRAME_NAS_ROOT", "/mnt/nas").rstrip("/") or "/mnt/nas"
CHROMIUM_BIN = os.environ.get("PIFRAME_CHROMIUM_BIN", "chromium").strip() or "chromium"
CAGE_BIN = os.environ.get("PIFRAME_CAGE_BIN", "cage").strip() or "cage"
BROWSER_ROTATION_DEGREES = 270
BROWSER_STATE_FILE = Path("/tmp/piframe_browser_state.json")
BROWSER_HTML_FILE = Path("/tmp/piframe_browser.html")
BROWSER_PROFILE_DIR = Path("/tmp/piframe_chromium_profile")
BROWSER_CACHE_DIR = Path("/tmp/piframe_chromium_cache")
BROWSER_LOG_FILE = Path("/tmp/piframe_browser.log")
BROWSER_LOG_MAX_BYTES = int(
    float(os.environ.get("PIFRAME_BROWSER_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
)
BROWSER_LOG_BACKUPS = max(1, int(os.environ.get("PIFRAME_BROWSER_LOG_BACKUPS", "3")))
BROWSER_MIN_ITEM_DURATION = 1.0
BROWSER_STATE_POLL_MS = 250
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
IDLE_MEDIA_DEFAULT = str(Path(__file__).resolve().parent / "idle.jpg")
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

    def __init__(
        self,
        on_track_end: Optional[Callable[[], None]],
        rotation_degrees: int = BROWSER_ROTATION_DEGREES,
    ) -> None:
        self.on_track_end = on_track_end
        self.rotation_degrees = rotation_degrees
        self.process: Optional[subprocess.Popen[Any]] = None
        self.slideshow_active = False
        self.slideshow_images: List[str] = []
        self.last_command: str = ""
        self._last_volume: float = 75.0
        self._last_mute: bool = False
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

    def _write_html(self) -> None:
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PiFrame Browser Renderer</title>
  <style>
    :root {{
      --bg: #050505;
      --fg: #f3f0e8;
      --muted: rgba(243, 240, 232, 0.72);
      --accent: #e0b35b;
    }}
    * {{ box-sizing: border-box; }}
    * {{ cursor: none !important; }}
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      background: var(--bg);
      overflow: hidden;
      color: var(--fg);
      cursor: none;
      font-family: Georgia, "Times New Roman", serif;
    }}
    body {{
      display: grid;
      place-items: center;
    }}
    .frame {{
      position: fixed;
      inset: 0;
      overflow: hidden;
      background: #050505;
    }}
    .stage {{
      position: absolute;
      inset: 0;
      opacity: 0;
      transition: opacity {BROWSER_TRANSITION_DURATION_MS}ms ease-in-out;
      pointer-events: none;
    }}
    .stage.active {{
      opacity: 1;
    }}
    .backdrop {{
      position: absolute;
      inset: -8%;
      opacity: 0;
      transform: scale(1.06);
      transition: opacity 180ms ease-in-out;
      filter: blur(34px) brightness(0.62) saturate(1.08);
      background: transparent;
    }}
    .backdrop.ready {{
      opacity: 1;
    }}
    .media {{
      position: absolute;
      left: 50%;
      top: 50%;
      width: auto;
      height: auto;
      object-fit: contain;
      transform: translate(-50%, -50%) rotate({self.rotation_degrees}deg);
      transform-origin: center center;
      filter: drop-shadow(0 0 24px rgba(0,0,0,0.45));
      opacity: 0;
      transition: opacity 180ms ease-in-out;
      background: transparent;
    }}
    .media.ready {{
      opacity: 1;
    }}
    .video {{
      display: none;
    }}
    .hud {{
      display: {"block" if BROWSER_SHOW_HUD else "none"};
      position: fixed;
      left: 20px;
      bottom: 18px;
      padding: 10px 14px;
      background: rgba(0, 0, 0, 0.42);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 12px;
      backdrop-filter: blur(8px);
      letter-spacing: 0.03em;
    }}
    .hud strong {{
      color: var(--accent);
      display: block;
      font-size: 14px;
      margin-bottom: 3px;
    }}
    .hud span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
  </style>
</head>
<body>
  <div class="frame">
    <div id="stage0" class="stage active">
      <img id="bgImage0" class="backdrop" alt="">
      <video id="bgVideo0" class="backdrop" muted playsinline preload="auto"></video>
      <img id="image0" class="media image" alt="">
      <video id="video0" class="media video" muted playsinline preload="auto"></video>
    </div>
    <div id="stage1" class="stage">
      <img id="bgImage1" class="backdrop" alt="">
      <video id="bgVideo1" class="backdrop" muted playsinline preload="auto"></video>
      <img id="image1" class="media image" alt="">
      <video id="video1" class="media video" muted playsinline preload="auto"></video>
    </div>
  </div>
  <div class="hud">
    <strong id="playlistName">Starting…</strong>
    <span id="fileName"></span>
    <span id="timing"></span>
  </div>
  <script>
    const stateUrl = {json.dumps(BROWSER_STATE_FILE.as_uri())};
    const hiddenCursor = 'url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9s0qK1wAAAAASUVORK5CYII=") 0 0, none';
    const stages = [
      {{
        root: document.getElementById("stage0"),
        bgImage: document.getElementById("bgImage0"),
        bgVideo: document.getElementById("bgVideo0"),
        image: document.getElementById("image0"),
        video: document.getElementById("video0"),
      }},
      {{
        root: document.getElementById("stage1"),
        bgImage: document.getElementById("bgImage1"),
        bgVideo: document.getElementById("bgVideo1"),
        image: document.getElementById("image1"),
        video: document.getElementById("video1"),
      }},
    ];
    const playlistEl = document.getElementById("playlistName");
    const fileEl = document.getElementById("fileName");
    const timingEl = document.getElementById("timing");
    let currentSignature = "";
    let currentControlToken = -1;
    let activeState = null;
    let activeIndex = 0;
    let intervalHandle = null;
    let activeStageIndex = 0;
    let pendingAdvanceToken = 0;

    function hideCursor() {{
      document.documentElement.style.cursor = hiddenCursor;
      document.body.style.cursor = hiddenCursor;
      for (const stage of stages) {{
        stage.root.style.cursor = hiddenCursor;
        stage.bgImage.style.cursor = hiddenCursor;
        stage.bgVideo.style.cursor = hiddenCursor;
        stage.image.style.cursor = hiddenCursor;
        stage.video.style.cursor = hiddenCursor;
      }}
    }}

    function fitMedia(element, naturalWidth, naturalHeight, fitMode = "contain") {{
      const viewportWidth = window.innerWidth || 1;
      const viewportHeight = window.innerHeight || 1;
      const usableWidth = viewportHeight;
      const usableHeight = viewportWidth;
      const fitFn = fitMode === "cover" ? Math.max : Math.min;
      const scale = fitFn(usableWidth / naturalWidth, usableHeight / naturalHeight);
      element.style.width = `${{Math.max(1, naturalWidth * scale)}}px`;
      element.style.height = `${{Math.max(1, naturalHeight * scale)}}px`;
    }}

    function setHud(item, state, perItemSeconds) {{
      if ({str(BROWSER_SHOW_HUD).lower()} !== true) {{
        return;
      }}
      playlistEl.textContent = state.playlist_name || "PiFrame";
      fileEl.textContent = item ? item.label : "";
      timingEl.textContent = `mode ${{state.mode}} | per-item ${{perItemSeconds.toFixed(3)}}s | rotate {self.rotation_degrees}deg`;
    }}

    function preloadNextImage() {{
      if (!activeState || !activeState.items || activeState.items.length < 2) {{
        return;
      }}
      const itemCount = activeState.items.length;
      let nextIndex = activeIndex + 1;
      if (activeState.repeat) {{
        nextIndex = (nextIndex + itemCount) % itemCount;
      }} else if (nextIndex >= itemCount) {{
        return;
      }}
      const nextItem = activeState.items[nextIndex];
      if (!nextItem || nextItem.kind !== "image") {{
        return;
      }}
      const preload = new Image();
      preload.src = nextItem.src;
    }}

    function stopTimers() {{
      window.clearInterval(intervalHandle);
      intervalHandle = null;
      pendingAdvanceToken += 1;
    }}

    function resetStage(stage) {{
      stage.bgImage.onload = null;
      stage.bgVideo.onloadedmetadata = null;
      stage.image.onload = null;
      stage.video.onloadedmetadata = null;
      stage.video.onended = null;
      stage.bgImage.classList.remove("ready");
      stage.bgVideo.classList.remove("ready");
      stage.image.classList.remove("ready");
      stage.video.classList.remove("ready");
      stage.bgImage.style.display = "none";
      stage.bgVideo.style.display = "none";
      stage.image.style.display = "none";
      stage.video.style.display = "none";
      stage.bgVideo.pause();
      stage.bgVideo.removeAttribute("src");
      stage.bgVideo.load();
      stage.video.pause();
      stage.video.removeAttribute("src");
      stage.video.load();
    }}

    function getActiveStage() {{
      return stages[activeStageIndex];
    }}

    function getInactiveStage() {{
      return stages[(activeStageIndex + 1) % stages.length];
    }}

    function activateStage(stage) {{
      const nextIndex = stages.indexOf(stage);
      if (nextIndex === -1) {{
        return;
      }}
      stages[activeStageIndex].root.classList.remove("active");
      stage.root.classList.add("active");
      activeStageIndex = nextIndex;
    }}

    function showIdle(item) {{
      if (!item) {{
        for (const stage of stages) {{
          resetStage(stage);
          stage.root.classList.remove("active");
        }}
        return;
      }}
      renderItem(item, {{
        playlist_name: "Idle",
        mode: "idle",
        muted: activeState ? activeState.muted : true,
        volume: activeState ? activeState.volume : 0,
        loop: true,
      }}, 0);
    }}

    function scheduleImageAdvance(perItemSeconds) {{
      const token = pendingAdvanceToken;
      window.clearInterval(intervalHandle);
      intervalHandle = window.setInterval(() => {{
        if (token !== pendingAdvanceToken) {{
          return;
        }}
        advancePlaylist(1);
      }}, perItemSeconds * 1000);
    }}

    function prepareStage(stage, item, state, perItemSeconds) {{
      resetStage(stage);
      if (item.kind === "video") {{
        const isSingleRepeatingPlaylist =
          state.mode === "playlist" &&
          !!state.repeat &&
          Array.isArray(state.items) &&
          state.items.length === 1;
        const requestedFillMode = state.video_fill_mode || "contain";
        // On this Pi/Chromium stack, playing the same video twice simultaneously
        // (foreground + blurred backdrop) is not reliable enough. Fall back to
        // contain for the experimental blurred_fill mode so videos keep playing
        // at their normal aspect ratio.
        const fillMode = requestedFillMode === "blurred_fill" ? "contain" : requestedFillMode;
        const useBlurredFill = false;
        if (useBlurredFill) {{
          stage.bgVideo.style.display = "block";
          stage.bgVideo.muted = true;
          stage.bgVideo.loop = state.mode !== "single" || !!state.loop;
          stage.bgVideo.preload = "auto";
          stage.bgVideo.onloadedmetadata = () => {{
            fitMedia(stage.bgVideo, stage.bgVideo.videoWidth || 1, stage.bgVideo.videoHeight || 1, "cover");
            stage.bgVideo.classList.add("ready");
          }};
          stage.bgVideo.src = item.src;
          stage.bgVideo.currentTime = 0;
        }}
        stage.video.style.display = "block";
        stage.video.dataset.desiredMuted = (!!state.muted).toString();
        stage.video.dataset.desiredVolume = Math.max(0, Math.min(1, (state.volume || 0) / 100)).toString();
        // Keep playback muted on this kiosk path. The Pi/Chromium/cage setup is
        // more reliable when Chromium never tries to open an ALSA playback device.
        stage.video.muted = true;
        stage.video.volume = 0;
        stage.video.loop = state.mode === "single" ? !!state.loop : isSingleRepeatingPlaylist;
        stage.video.preload = "auto";
        stage.video.onloadedmetadata = () => {{
          const foregroundMode = fillMode === "cover" ? "cover" : "contain";
          fitMedia(stage.video, stage.video.videoWidth || 1, stage.video.videoHeight || 1, foregroundMode);
          stage.video.classList.add("ready");
        }};
        stage.video.onended = () => {{
          if (state.mode === "single" && !state.loop) {{
            showIdle(state.idle_item);
            return;
          }}
          if (isSingleRepeatingPlaylist) {{
            return;
          }}
          advancePlaylist(1);
        }};
        stage.video.src = item.src;
        stage.video.currentTime = 0;
      }} else {{
        stage.image.style.display = "block";
        stage.image.onload = () => {{
          fitMedia(stage.image, stage.image.naturalWidth || 1, stage.image.naturalHeight || 1, "contain");
          stage.image.classList.add("ready");
        }};
        stage.image.src = item.src;
      }}
      setHud(item, state, perItemSeconds);
    }}

    function renderItem(item, state, perItemSeconds) {{
      if (!item) {{
        return;
      }}
      const targetStage = getInactiveStage();
      prepareStage(targetStage, item, state, perItemSeconds);
      activateStage(targetStage);
      const previousStage = getInactiveStage();
      window.setTimeout(() => {{
        resetStage(previousStage);
      }}, {max(BROWSER_TRANSITION_DURATION_MS + 50, 150)});
      if (item.kind === "video") {{
        stagePlay(targetStage.video);
        if (targetStage.bgVideo.style.display === "block") {{
          targetStage.bgVideo.play().catch(() => {{}});
        }}
      }} else {{
        scheduleImageAdvance(perItemSeconds);
        preloadNextImage();
      }}
      hideCursor();
    }}

    function stagePlay(video) {{
      window.clearInterval(intervalHandle);
      intervalHandle = null;
      video.play().catch(() => {{}});
    }}

    function advancePlaylist(step) {{
      if (!activeState || !activeState.items || !activeState.items.length) {{
        return;
      }}
      const itemCount = activeState.items.length;
      let nextIndex = activeIndex + step;
      if (activeState.mode === "playlist") {{
        if (activeState.repeat) {{
          nextIndex = (nextIndex + itemCount) % itemCount;
        }} else {{
          if (nextIndex >= itemCount) {{
            showIdle(activeState.idle_item);
            return;
          }}
          if (nextIndex < 0) {{
            nextIndex = 0;
          }}
        }}
      }} else {{
        nextIndex = 0;
      }}
      activeIndex = nextIndex;
      const perItemSeconds = activeState.interval || 5.0;
      renderItem(activeState.items[activeIndex], activeState, perItemSeconds);
    }}

    function startFromState(state) {{
      activeState = state;
      activeIndex = 0;
      stopTimers();
      if (!state.items || !state.items.length) {{
        showIdle(state.idle_item);
        return;
      }}
      const perItemSeconds = state.interval || 5.0;
      renderItem(state.items[activeIndex], state, perItemSeconds);
    }}

    function applyControl(control) {{
      if (!control) {{
        return;
      }}
      if (control.action === "next") {{
        advancePlaylist(1);
      }} else if (control.action === "previous") {{
        advancePlaylist(-1);
      }} else if (control.action === "pause") {{
        const activeStage = getActiveStage();
        if (activeStage.video.style.display === "block") {{
          if (activeStage.video.paused) {{
            activeStage.video.play().catch(() => {{}});
          }} else {{
            activeStage.video.pause();
          }}
        }} else {{
          if (intervalHandle) {{
            stopTimers();
          }} else {{
            scheduleImageAdvance(activeState ? (activeState.interval || 5.0) : 5.0);
          }}
        }}
      }}
    }}

    async function pollState() {{
      try {{
        const response = await fetch(`${{stateUrl}}?ts=${{Date.now()}}`, {{ cache: "no-store" }});
        const state = await response.json();
        const signature = JSON.stringify({{
          mode: state.mode,
          playlist_name: state.playlist_name,
          items: state.items,
          repeat: state.repeat,
          loop: state.loop,
          interval: state.interval,
          shuffle: state.shuffle,
          transition: state.transition,
          transition_duration_ms: state.transition_duration_ms,
          video_fill_mode: state.video_fill_mode,
          idle_item: state.idle_item,
          volume: state.volume,
          muted: state.muted,
        }});
        if (signature !== currentSignature) {{
          currentSignature = signature;
          currentControlToken = state.control ? state.control.token : -1;
          startFromState(state);
        }} else if (state.control && state.control.token !== currentControlToken) {{
          currentControlToken = state.control.token;
          applyControl(state.control);
        }}
      }} catch (error) {{
        // ignore transient read errors while state file is being replaced
      }}
    }}

    hideCursor();
    window.addEventListener("resize", () => {{
      const activeStage = getActiveStage();
      if (activeStage.video.style.display === "block") {{
        const fillMode = activeState ? (activeState.video_fill_mode || "contain") : "contain";
        fitMedia(
          activeStage.video,
          activeStage.video.videoWidth || 1,
          activeStage.video.videoHeight || 1,
          fillMode === "cover" ? "cover" : "contain"
        );
        if (activeStage.bgVideo.style.display === "block") {{
          fitMedia(activeStage.bgVideo, activeStage.bgVideo.videoWidth || 1, activeStage.bgVideo.videoHeight || 1, "cover");
        }}
      }} else if (activeStage.image.style.display === "block") {{
        fitMedia(activeStage.image, activeStage.image.naturalWidth || 1, activeStage.image.naturalHeight || 1);
      }}
    }});
    window.addEventListener("mousemove", hideCursor, {{ passive: true }});
    window.addEventListener("pointermove", hideCursor, {{ passive: true }});
    window.addEventListener("focus", hideCursor);
    window.setInterval(hideCursor, 1000);
    pollState();
    window.setInterval(pollState, {BROWSER_STATE_POLL_MS});
  </script>
</body>
</html>
"""
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
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log(
            BROWSER_LOG_FILE,
            max_bytes=BROWSER_LOG_MAX_BYTES,
            backups=BROWSER_LOG_BACKUPS,
        )
        args = [
            CAGE_BIN,
            "-d",
            "--",
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
            "--mute-audio",
            "--password-store=basic",
            "--allow-file-access-from-files",
            "--autoplay-policy=no-user-gesture-required",
            BROWSER_HTML_FILE.as_uri(),
        ]
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
        ext = Path(path_str.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        return "video" if ext in VIDEO_EXTENSIONS else "image"

    def _make_item(self, path_str: str) -> Dict[str, Any]:
        return {
            "src": Path(path_str).as_uri(),
            "label": Path(path_str).name,
            "kind": self._item_kind(path_str),
        }

    def _write_state(self) -> None:
        tmp_path = BROWSER_STATE_FILE.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(self._state, handle)
        tmp_path.replace(BROWSER_STATE_FILE)

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
            idle_media=self._pick_existing_idle_media(IDLE_MEDIA_DEFAULT),
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
            idle_media=self._pick_existing_idle_media(IDLE_MEDIA_DEFAULT),
        ):
            return False
        self.slideshow_active = True
        self.slideshow_images = list(play_items)
        self.last_command = (
            f"browser mixed-playlist count={len(play_items)} interval={float(interval):.3f} shuffle={shuffle}"
        )
        return True

    def play_image_slideshow(
        self,
        images: List[str],
        interval: float,
        *,
        shuffle: bool = False,
    ) -> bool:
        if not images:
            return False
        play_images = list(images)
        if shuffle:
            random.shuffle(play_images)
        if not self._set_items_state(
            mode="playlist",
            playlist_name="Image Slideshow",
            items=play_images,
            interval=interval,
            repeat=True,
            loop=True,
            shuffle=shuffle,
            idle_media=self._pick_existing_idle_media(IDLE_MEDIA_DEFAULT),
        ):
            return False
        self.slideshow_active = True
        self.slideshow_images = list(play_images)
        self.last_command = (
            f"browser image-playlist count={len(play_images)} interval={float(interval):.3f} shuffle={shuffle}"
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
            idle_media=self._pick_existing_idle_media(IDLE_MEDIA_DEFAULT),
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
        self._last_volume = float(level)
        self._last_mute = level <= 0
        with self._state_lock:
            self._state["volume"] = self._last_volume
            self._state["muted"] = self._last_mute
            self._write_state()


class PiFrameClient:
    """Minimal WebSocket-to-browser bridge with basic status/volume updates."""

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.ws_connection: Optional[websocket.WebSocketApp] = None
        self.renderer = BrowserController(self._on_playback_end)
        self.current_playlist_name: str = ""
        self.current_playlist_id: str = ""
        self.current_video: str = ""
        self.current_slideshow: List[str] = []
        self.current_interval: float = 0.0
        self.current_shuffle: bool = False
        self.playback_state: str = "stopped"
        self.status_thread: Optional[threading.Thread] = None
        self.status_running = False
        self.system_info = get_system_info()

    def run(self) -> None:
        _log(
            "client_starting",
            client_id=self.config.client_id,
            name=self.config.name,
            server=self.config.server,
            group=self.config.group,
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
        playlist_name = params.get("playlist_name") or data.get("playlist_name", "")
        playlist_id = params.get("playlist_id") or data.get("playlist_id", "")
        if cmd == "play":
            url = data.get("url") or params.get("url")
            loop_flag_raw = params.get("loop", data.get("loop", True))
            loop_flag = _coerce_bool(loop_flag_raw, default=True)
            self.current_playlist_name = playlist_name
            self.current_playlist_id = playlist_id
            url = _normalize_media_url(url)
            if url:
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
        elif cmd == "video_playlist":
            items = (
                params.get("items")
                or params.get("videos")
                or data.get("items")
                or data.get("videos")
                or []
            )
            items = [_normalize_media_url(item) for item in items if item]
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
                self._send_render_command()
        elif cmd == "pause":
            _log("pause_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
            self.renderer.toggle_pause()
            self.playback_state = "playing"  # minimal toggle; not querying paused state
            self._send_render_command(action="pause")
        elif cmd == "next":
            _log("next_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
            self.renderer.playlist_next()
        elif cmd == "previous":
            _log("previous_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
            self.renderer.playlist_previous()
        elif cmd == "stop":
            _log("stop_command", playlist=playlist_name or "", playlist_id=playlist_id or "")
            if self.renderer.last_command:
                self._send_render_command(action="stop")
            self.renderer.show_idle(self._get_idle_media())
            self.current_video = ""
            self.current_slideshow = []
            self.playback_state = "stopped"
        elif cmd == "slideshow":
            images = params.get("images") or data.get("images") or []
            images = [_normalize_media_url(img) for img in images if img]
            interval = params.get("interval", 5)
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
                interval = float(interval)
            except (TypeError, ValueError):
                interval = 5.0
            self.current_playlist_name = playlist_name
            self.current_playlist_id = playlist_id
            # Avoid reloading for identical slideshow commands (same list/order + settings).
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
            if images and self.renderer.play_slideshow(
                images, interval, shuffle=shuffle_flag
            ):
                self.current_slideshow = list(self.renderer.slideshow_images) or images
                self.current_video = ""
                self.playback_state = "slideshow"
                self.current_interval = float(interval)
                self.current_shuffle = shuffle_flag
                self._send_render_command()
        elif cmd == "volume":
            level = params.get("level", data.get("level", 75))
            try:
                level = float(level)
            except (TypeError, ValueError):
                level = 75
            _log("volume_command", level=level)
            self.renderer.set_volume(level)

    def on_close(self, ws, close_status_code, close_msg) -> None:  # pylint: disable=unused-argument
        _log("websocket_closed", code=close_status_code, message=close_msg)
        self.ws_connection = None
        self._stop_status_updates()
        self.renderer.show_idle(self._get_idle_media())

    def on_error(self, ws, error: Exception) -> None:  # pylint: disable=unused-argument
        _log("websocket_error", error=error)

    def _on_playback_end(self) -> None:
        """Update local state when a non-looping single item ends."""
        self.current_video = ""
        self.current_slideshow = []
        self.current_shuffle = False
        self.playback_state = "stopped"
        self.renderer.show_idle(self._get_idle_media())

    def shutdown(self) -> None:
        self.renderer.shutdown()
        self._stop_status_updates()

    @staticmethod
    def _get_idle_media() -> str:
        idle_media = os.environ.get(IDLE_MEDIA_ENV, IDLE_MEDIA_DEFAULT)
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
        self.status_running = True
        self.status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self.status_thread.start()

    def _stop_status_updates(self) -> None:
        self.status_running = False
        if self.status_thread and self.status_thread.is_alive():
            self.status_thread.join(timeout=2.0)
        self.status_thread = None

    def _status_loop(self) -> None:
        while self.status_running:
            time.sleep(STATUS_UPDATE_INTERVAL)
            if not self.status_running or not self.ws_connection:
                continue
            status: Dict[str, Any] = {
                "current_video": self.current_video,
                "current_slideshow": self.current_slideshow,
                "current_playlist": self.current_playlist_name,
                "current_playlist_id": self.current_playlist_id,
                "playback_state": self.playback_state,
                "shuffle": self.current_shuffle,
                "slideshow_active": self.renderer.slideshow_active,
                "last_render_cmd": self.renderer.last_command or None,
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
