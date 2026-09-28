"""Keep the kiosk's display at the right rotation and resolution.

Started by piframe_client.py's launcher INSIDE the cage session, so
wlr-randr talks to the kiosk's own compositor. Once a second it checks
every output:

- Rotation (PIFRAME_OUTPUT_TRANSFORM). A TV suspend drops the HDMI link
  and cage forgets the transform; the output comes back unrotated, so the
  watcher puts the transform back (with the configured mode, one call) -
  the same thing the old bash watcher did.
- EDID recovery. The mode comes from the TV's EDID. When the EDID can't be
  read at the moment the kiosk starts or the link comes back (the TV
  waking up, a marginal HDMI contact), the kernel offers only fallback
  modes and cage settles on 1024x768: a 4:3 page the 16:9 TV then
  stretches - squished pictures between dark bars. While a connected HDMI
  port has no EDID, the watcher asks the kernel to read it again (at most
  every EDID_RETRY_S); once it has it, the watcher replugs the port in
  software if the compositor's mode list is behind the kernel's (wlroots
  rebuilds it only on a reconnect), then switches to the TV's mode.

A TV that identifies itself normally is never touched beyond the rotation:
the resolution is only changed while recovering from the fallback or
together with a rotation re-apply, and a replug only happens when the
kernel offers a mode the compositor doesn't list - once per recovery.
`--no-recover` (PIFRAME_DISPLAY_RECOVERY=0) turns the EDID recovery off
and leaves just the rotation watch.

Logs go to stdout, i.e. the browser log (/tmp/piframe_browser.log).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

AUTO_4K_MODE = (1920, 1080, 60.0)
EDID_RETRY_S = 30.0
# A fix that didn't take (wlr-randr failed, the TV refused the mode) is
# retried after this long instead of every second.
APPLY_RETRY_S = 5.0
DRM = Path("/sys/class/drm")


def log(event: str, **fields) -> None:
    parts = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[display_watch] {event} {parts}".rstrip(), flush=True)


def parse_mode(value: str) -> tuple[int, int, float] | None:
    """'1920x1080@60' -> (1920, 1080, 60.0). `auto`, `native` and '' -> None."""
    value = (value or "").strip().lower()
    if not value or value in ("auto", "native"):
        return None
    size, _, refresh = value.partition("@")
    try:
        width, height = (int(part) for part in size.split("x"))
        return width, height, float(refresh.removesuffix("hz") or 60)
    except ValueError:
        return None


def _closest(modes: list[dict], width: int, height: int, refresh: float) -> dict | None:
    same = [m for m in modes if m.get("width") == width and m.get("height") == height]
    if not same:
        return None
    return min(same, key=lambda m: abs(float(m.get("refresh") or 0) - refresh))


def desired_mode(modes: list[dict], configured: str) -> dict | None:
    """The mode this output should run, picked from wlr-randr's list, or
    None when it can't be known yet: with no EDID the kernel marks nothing
    preferred - only fallback modes - so there is nothing to switch to."""
    explicit = parse_mode(configured)
    if explicit:
        return _closest(modes, *explicit)
    preferred = next((m for m in modes if m.get("preferred")), None)
    if preferred is None:
        return None
    too_big = preferred.get("width", 0) * preferred.get("height", 0) > AUTO_4K_MODE[0] * AUTO_4K_MODE[1]
    if (configured or "").strip().lower() == "auto" and too_big:
        return _closest(modes, *AUTO_4K_MODE) or preferred
    return preferred


def _same_mode(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and (a["width"], a["height"], round(float(a["refresh"]), 2)) == (
        b["width"], b["height"], round(float(b["refresh"]), 2))


def plan(output: dict, transform: str, configured: str, *, recovering: bool = False) -> list[str]:
    """wlr-randr arguments that bring this output in line ([] = nothing to
    do). The mode is only set while recovering from the EDID fallback or
    together with a rotation re-apply - never on its own for a TV that
    identified itself. Mode and transform always go in ONE call - applying
    them apart flashed a landscape frame between the two."""
    modes = output.get("modes") or []
    current = next((m for m in modes if m.get("current")), None)
    wrong_transform = bool(transform) and str(output.get("transform") or "normal") != transform
    want = desired_mode(modes, configured) if (recovering or wrong_transform) else None
    wrong_mode = want is not None and not _same_mode(want, current)
    if not (wrong_transform or wrong_mode):
        return []
    args = []
    if wrong_mode:
        args += ["--mode", f"{want['width']}x{want['height']}@{float(want['refresh']):.3f}Hz"]
    if transform:
        args += ["--transform", transform]
    return args


def edid_missing(connector: Path) -> bool:
    """A connected port whose EDID came back empty - the fallback-modes case."""
    try:
        return (connector / "status").read_text().strip() == "connected" and not (connector / "edid").read_bytes()
    except OSError:
        return False


def compositor_behind(connector: Path, output: dict) -> bool:
    """The kernel's first mode (the EDID's preferred one) is missing from
    the compositor's list - wlroots built that list while the EDID was
    unreadable and only rebuilds it on a reconnect."""
    try:
        first = (connector / "modes").read_text().split()[0]
        width, height = (int(v) for v in first.split("x")[:2])
    except (OSError, ValueError, IndexError):
        return False
    return not any(m.get("width") == width and m.get("height") == height for m in output.get("modes") or [])


def connector_for(name: str) -> Path | None:
    hits = sorted(DRM.glob(f"card*-{name}"))
    return hits[0] if hits else None


def _write_status(connector: Path, value: str) -> bool:
    """The connector's status file takes 'detect' / 'off' (root only; the
    kiosk user has passwordless sudo)."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "tee", str(connector / "status")],
            input=value.encode(), capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def reprobe(connector: Path) -> bool:
    """Ask the kernel to read the EDID again."""
    return _write_status(connector, "detect")


def replug(connector: Path) -> bool:
    """Make the compositor see the port unplug and come back - what a TV
    power cycle does. Always re-enables the port ('off' would otherwise
    stick until the next write)."""
    _write_status(connector, "off")
    time.sleep(1.0)
    for _ in range(5):
        if _write_status(connector, "detect"):
            return True
        time.sleep(1.0)
    return False


def read_outputs(wlr_randr: str) -> list[dict]:
    try:
        result = subprocess.run([wlr_randr, "--json"], capture_output=True, timeout=10)
        return json.loads(result.stdout or b"[]") if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def apply(wlr_randr: str, name: str, args: list[str]) -> bool:
    try:
        result = subprocess.run([wlr_randr, "--output", name, *args], capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Watcher:
    def __init__(self, wlr_randr: str, transform: str, configured: str, *, recover: bool = True):
        self.wlr_randr = wlr_randr
        self.transform = "" if transform in ("", "normal") else transform
        self.configured = configured
        self.recover = recover
        self.next_reprobe: dict[str, float] = {}
        self.next_apply: dict[str, float] = {}
        self.no_edid: set[str] = set()
        self.recovering: set[str] = set()
        self.seen: set[str] = set()

    def _recover_step(self, name: str, output: dict, now: float) -> bool:
        """EDID recovery for one output. True = the port is being replugged,
        leave the output alone this tick."""
        connector = connector_for(name)
        first_sight = name not in self.seen
        self.seen.add(name)
        if connector is None:
            return False
        if edid_missing(connector):
            if name not in self.no_edid:
                self.no_edid.add(name)
                self.recovering.add(name)
                log("edid_missing", output=name, note="TV identification unreadable - retrying")
            if now >= self.next_reprobe.get(name, 0.0):
                self.next_reprobe[name] = now + EDID_RETRY_S
                reprobe(connector)
            return False
        if name in self.no_edid or first_sight:
            came_back = name in self.no_edid
            self.no_edid.discard(name)
            if compositor_behind(connector, output):
                # Once per recovery (or kiosk start): the next ticks put the
                # rotation back and, while recovering, the TV's mode.
                self.recovering.add(name)
                log("edid_back", output=name, action="replug", ok=replug(connector))
                return True
            if came_back:
                log("edid_back", output=name)
        return False

    def tick(self, now: float) -> None:
        for output in read_outputs(self.wlr_randr):
            name = output.get("name") or ""
            if not name or not output.get("enabled", True):
                continue
            if self.recover and self._recover_step(name, output, now):
                continue
            recovering = name in self.recovering and name not in self.no_edid
            args = plan(output, self.transform, self.configured, recovering=recovering)
            if not args:
                if recovering:
                    self.recovering.discard(name)
                continue
            if now >= self.next_apply.get(name, 0.0):
                self.next_apply[name] = now + APPLY_RETRY_S
                log("apply", output=name, args=" ".join(args), ok=apply(self.wlr_randr, name, args))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wlr-randr", default="wlr-randr")
    parser.add_argument("--transform", default="")
    parser.add_argument("--mode", default="auto", help="auto | native | WxH[@Hz]")
    parser.add_argument("--no-recover", action="store_true", help="rotation watch only, no EDID recovery")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    opts = parser.parse_args()
    watcher = Watcher(opts.wlr_randr, opts.transform, opts.mode, recover=not opts.no_recover)
    while True:
        watcher.tick(time.monotonic())
        if opts.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
