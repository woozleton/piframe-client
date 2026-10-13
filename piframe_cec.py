#!/usr/bin/env python3
"""
PiFrame CEC: switches this frame's TV on and off over HDMI-CEC for Home
Assistant.

Runs as its own systemd unit (piframe-cec), separate from the kiosk, so a
kiosk restart never touches the TV and screen control keeps working when the
kiosk is broken. Identical on every frame - nothing here is per-host:

  * Talks to /dev/cec* directly through the kernel CEC ioctls (no cec-ctl,
    no libcec). Opens every adapter and uses whichever one currently has a
    valid physical address, so the TV can be on either Pi HDMI port and any
    TV input; the TV tells us which input we are on.
  * Registers as a Playback device (OSD name = hostname without "frame-").
  * Home Assistant talks to it over MQTT with discovery: one device per
    frame, a Screen switch and a diagnostic TV power sensor. HA's on/off is
    published RETAINED, so the broker holds each screen's desired state;
    it is also cached locally so a power cut (Pi boots faster than HA)
    still recovers.

Behaviour (field-tested 2026-09-22 on the fleet's Samsung Frames and the
mirror's repair-kit Vizio):

  * on  = <Image View On> to the TV, then broadcast <Active Source>.
  * off = broadcast <Active Source>, then <Standby>. The stairs Samsung
    ignored a bare <Standby>; announcing ourselves as source first fixed it.
  * Only a power report of "on" means on. Off reads as standby, to-on
    (stairs!) or to-standby depending on the TV. The mirror's Vizio reports
    "on" no matter what and ignores <Standby>; its off is a smart plug, so
    this service only has to wake it when mains power returns.
  * The TV regaining power (physical address back after >= 5s gone) or the
    Pi having just booted, with the screen meant to be on -> wake it. A
    plain service restart (OTA) never changes the TV.

Also a small CLI for bootstrap and hand debugging (works alongside the
running service):  --probe | --on | --off
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import paho.mqtt.client as mqtt  # type: ignore
except ImportError:  # pragma: no cover - CEC-only mode without the package
    mqtt = None

# ---------------------------------------------------------------------------
# Kernel CEC ABI (linux/cec.h). Values and struct layouts verified on the
# fleet's Pi 5 / kernel 6.12 by compiling against the installed header.
# ---------------------------------------------------------------------------
CEC_ADAP_G_PHYS_ADDR = 0x80026101
CEC_ADAP_G_LOG_ADDRS = 0x805C6103
CEC_ADAP_S_LOG_ADDRS = 0xC05C6104
CEC_TRANSMIT = 0xC0386105
CEC_RECEIVE = 0xC0386106
CEC_DQEVENT = 0xC0506107
CEC_S_MODE = 0x40046109

CEC_MODE_INITIATOR = 0x01
CEC_MODE_FOLLOWER = 0x10

CEC_EVENT_STATE_CHANGE = 1
CEC_EVENT_LOST_MSGS = 2

CEC_TX_STATUS_OK = 0x01
CEC_RX_STATUS_OK = 0x01
CEC_RX_STATUS_FEATURE_ABORT = 0x04

# struct cec_msg (56 bytes): tx_ts, rx_ts, len, timeout, sequence, flags,
# msg[16], reply, rx_status, tx_status, arb_lost, nack, low_drive, error, pad
MSG_FMT = "=QQIIII16sBBBBBBBx"
# struct cec_log_addrs (92 bytes): log_addr[4], log_addr_mask, cec_version,
# num_log_addrs, vendor_id, flags, osd_name[15], primary_device_type[4],
# log_addr_type[4], all_device_types[4], features[4][12], pad
LOG_ADDRS_FMT = "=4sHBBII15s4s4s4s48sx"
# struct cec_event (80 bytes): ts, event, flags, union (64 bytes)
EVENT_FMT = "=QII64s"

PA_INVALID = 0xFFFF
LA_TV = 0
LA_BROADCAST = 15
LA_TYPE_PLAYBACK = 3
PRIM_DEVTYPE_PLAYBACK = 4
CEC_VERSION_1_4 = 5
VENDOR_ID_NONE = 0xFFFFFFFF

# Opcodes
OP_FEATURE_ABORT = 0x00
OP_IMAGE_VIEW_ON = 0x04
OP_STANDBY = 0x36
OP_SET_OSD_NAME = 0x47
OP_ROUTING_CHANGE = 0x80
OP_ACTIVE_SOURCE = 0x82
OP_REPORT_PHYSICAL_ADDR = 0x84
OP_REQUEST_ACTIVE_SOURCE = 0x85
OP_SET_STREAM_PATH = 0x86
OP_DEVICE_VENDOR_ID = 0x87
OP_GIVE_DEVICE_VENDOR_ID = 0x8C
OP_MENU_REQUEST = 0x8D
OP_MENU_STATUS = 0x8E
OP_GIVE_DEVICE_POWER_STATUS = 0x8F
OP_REPORT_POWER_STATUS = 0x90
OP_CEC_VERSION = 0x9E
OP_GIVE_OSD_NAME = 0x46

# Directed messages that are replies/reports we asked for - never answer
# these with <Feature Abort>.
REPLY_OPCODES = {
    OP_FEATURE_ABORT, OP_REPORT_POWER_STATUS, OP_CEC_VERSION, OP_SET_OSD_NAME,
    OP_REPORT_PHYSICAL_ADDR, OP_DEVICE_VENDOR_ID, OP_MENU_STATUS,
}

POWER_NAMES = {0: "on", 1: "standby", 2: "to-on", 3: "to-standby"}
VENDOR_NAMES = {0x0000F0: "Samsung", 0x00E091: "LG", 0x080046: "Sony"}

# ---------------------------------------------------------------------------
# Tunables (env overrides)
# ---------------------------------------------------------------------------
POWER_POLL_SECONDS = float(os.environ.get("PIFRAME_CEC_POLL_SECONDS", "30"))
# How long the physical address must stay valid before we act on it (the
# mirror's hotplug flapped once while the TV booted).
PA_SETTLE_SECONDS = 3.0
# How long it must have been gone for its return to count as "the TV lost
# power" rather than a hotplug blip.
PA_GONE_SECONDS = 5.0
# A Pi up for less than this counts as freshly booted (power cut / reboot).
BOOT_WINDOW_SECONDS = 600.0
VERIFY_SECONDS = 4.0
MAX_RESENDS = 2
SOURCE_THEN_STANDBY_GAP = 1.0

MQTT_HOST = os.environ.get("PIFRAME_MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("PIFRAME_MQTT_PORT", "1883") or 1883)
MQTT_USER = os.environ.get("PIFRAME_MQTT_USER", "").strip()
MQTT_PASSWORD = os.environ.get("PIFRAME_MQTT_PASSWORD", "")
DISCOVERY_PREFIX = os.environ.get("PIFRAME_MQTT_DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"

HOSTNAME = socket.gethostname()
REPO_DIR = Path(__file__).resolve().parent


def log(*parts: object) -> None:
    print("[cec]", *parts, flush=True)


def pa_str(pa: int) -> str:
    if pa == PA_INVALID:
        return "f.f.f.f"
    return ".".join(str((pa >> shift) & 0xF) for shift in (12, 8, 4, 0))


def osd_name() -> str:
    name = HOSTNAME[len("frame-"):] if HOSTNAME.startswith("frame-") else HOSTNAME
    return name[:14] or "piframe"


def uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def repo_sha() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ---------------------------------------------------------------------------
# One /dev/cecN adapter
# ---------------------------------------------------------------------------
class Adapter:
    def __init__(self, path: str, follower: bool) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        mode = CEC_MODE_INITIATOR | (CEC_MODE_FOLLOWER if follower else 0)
        fcntl.ioctl(self.fd, CEC_S_MODE, struct.pack("=I", mode))
        self.pa = self.phys_addr()
        self.valid_since: Optional[float] = time.monotonic() if self.pa != PA_INVALID else None
        self.invalid_since: Optional[float] = None if self.pa != PA_INVALID else time.monotonic()

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def phys_addr(self) -> int:
        buf = bytearray(2)
        fcntl.ioctl(self.fd, CEC_ADAP_G_PHYS_ADDR, buf)
        return struct.unpack("=H", buf)[0]

    def _get_log_addrs(self) -> tuple:
        buf = bytearray(struct.calcsize(LOG_ADDRS_FMT))
        fcntl.ioctl(self.fd, CEC_ADAP_G_LOG_ADDRS, buf)
        return struct.unpack(LOG_ADDRS_FMT, buf)

    def log_addr(self) -> Optional[int]:
        """Our claimed logical address, or None while unconfigured."""
        la_bytes, mask = self._get_log_addrs()[0:2]
        if not mask:
            return None
        la = la_bytes[0]
        return None if la == 0xFF else la

    def configure(self, name: str) -> None:
        """Register as one Playback device. Idempotent; the kernel keeps
        this across hotplug and re-claims whenever the PA comes back.
        No RC passthrough on purpose: TV-remote keys must not turn into
        keystrokes inside the kiosk browser."""
        (_la, _mask, _version, num, _vendor, flags, cur_name,
         _prim, la_type, *_rest) = self._get_log_addrs()
        want_name = name.encode()[:14]
        if (num == 1 and la_type[0] == LA_TYPE_PLAYBACK and flags == 0
                and cur_name.rstrip(b"\0") == want_name):
            return
        if num:
            clear = struct.pack(LOG_ADDRS_FMT, b"\0" * 4, 0, 0, 0, 0, 0, b"", b"", b"", b"", b"")
            fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, bytearray(clear))
        want = struct.pack(
            LOG_ADDRS_FMT, b"\0" * 4, 0, CEC_VERSION_1_4, 1, VENDOR_ID_NONE, 0,
            want_name.ljust(15, b"\0"), bytes([PRIM_DEVTYPE_PLAYBACK, 0, 0, 0]),
            bytes([LA_TYPE_PLAYBACK, 0, 0, 0]), b"", b"",
        )
        # Blocks until the address is claimed when the TV is connected
        # (well under a second); returns at once when it isn't.
        fcntl.ioctl(self.fd, CEC_ADAP_S_LOG_ADDRS, bytearray(want))

    def transmit(self, dest: int, payload: bytes, reply: int = 0,
                 timeout_ms: int = 1000) -> Tuple[bool, Optional[bytes]]:
        """Send one message. Returns (acked, reply_bytes). reply_bytes is the
        full reply (header + opcode + operands) when `reply` was requested
        and the TV answered with that opcode."""
        la = self.log_addr()
        if la is None:
            return False, None
        data = bytes([(la << 4) | dest]) + payload
        buf = bytearray(struct.pack(
            MSG_FMT, 0, 0, len(data), timeout_ms if reply else 0, 0, 0,
            data.ljust(16, b"\0"), reply, 0, 0, 0, 0, 0, 0,
        ))
        try:
            fcntl.ioctl(self.fd, CEC_TRANSMIT, buf)
        except OSError as exc:
            log(f"{self.path}: transmit {payload[:1].hex()} failed: {exc}")
            return False, None
        fields = struct.unpack(MSG_FMT, buf)
        length, msg, rx_status, tx_status = fields[2], fields[6], fields[8], fields[9]
        acked = bool(tx_status & CEC_TX_STATUS_OK)
        if reply and (rx_status & CEC_RX_STATUS_OK) and not (rx_status & CEC_RX_STATUS_FEATURE_ABORT):
            return acked, msg[:length]
        return acked, None

    def receive(self) -> Optional[bytes]:
        buf = bytearray(struct.calcsize(MSG_FMT))
        try:
            fcntl.ioctl(self.fd, CEC_RECEIVE, buf)
        except OSError:
            return None
        fields = struct.unpack(MSG_FMT, buf)
        return fields[6][:fields[2]]

    def dqevent(self) -> Optional[Tuple[int, int]]:
        """(event, phys_addr) - phys_addr only meaningful for state changes."""
        buf = bytearray(struct.calcsize(EVENT_FMT))
        try:
            fcntl.ioctl(self.fd, CEC_DQEVENT, buf)
        except OSError:
            return None
        _ts, event, _flags, union = struct.unpack(EVENT_FMT, buf)
        return event, struct.unpack_from("=H", union, 0)[0]

    # High-level commands -------------------------------------------------
    def active_source(self) -> bool:
        return self.transmit(LA_BROADCAST, bytes([OP_ACTIVE_SOURCE, self.pa >> 8, self.pa & 0xFF]))[0]

    def wake(self) -> bool:
        ok = self.transmit(LA_TV, bytes([OP_IMAGE_VIEW_ON]))[0]
        time.sleep(0.3)
        return self.active_source() and ok

    def standby(self) -> bool:
        self.active_source()
        time.sleep(SOURCE_THEN_STANDBY_GAP)
        return self.transmit(LA_TV, bytes([OP_STANDBY]))[0]

    def power_status(self) -> str:
        acked, rep = self.transmit(LA_TV, bytes([OP_GIVE_DEVICE_POWER_STATUS]), reply=OP_REPORT_POWER_STATUS)
        if rep and len(rep) >= 3:
            return POWER_NAMES.get(rep[2], f"unknown-{rep[2]}")
        return "no-reply"

    def vendor(self) -> Optional[int]:
        _acked, rep = self.transmit(LA_TV, bytes([OP_GIVE_DEVICE_VENDOR_ID]), reply=OP_DEVICE_VENDOR_ID)
        if rep and len(rep) >= 5:
            return (rep[2] << 16) | (rep[3] << 8) | rep[4]
        return None


def open_adapters(follower: bool) -> List[Adapter]:
    adapters = []
    for path in sorted(str(p) for p in Path("/dev").glob("cec[0-9]*")):
        try:
            adapters.append(Adapter(path, follower))
        except OSError as exc:
            log(f"{path}: cannot open ({exc})")
    return adapters


def connected(adapters: List[Adapter]) -> Optional[Adapter]:
    for ad in adapters:
        if ad.pa != PA_INVALID:
            return ad
    return None


# ---------------------------------------------------------------------------
# MQTT / Home Assistant link
# ---------------------------------------------------------------------------
class HaLink:
    """Owns the paho client. Callbacks run on paho's thread and only push
    onto the service's queue; all publishing happens from the main loop."""

    def __init__(self, inbox: "queue.Queue[tuple]", wake_fd: int) -> None:
        self.inbox = inbox
        self.wake_fd = wake_fd
        self.enabled = bool(mqtt is not None and MQTT_HOST)
        base = f"piframe/{HOSTNAME}"
        self.t_set = f"{base}/screen/set"
        self.t_state = f"{base}/screen/state"
        self.t_power = f"{base}/tv_power"
        self.t_avail = f"{base}/availability"
        self.client = None
        if not self.enabled:
            reason = "paho-mqtt not installed" if mqtt is None else "PIFRAME_MQTT_HOST not set"
            log(f"MQTT disabled ({reason}); running CEC-only")
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"piframe-cec-{HOSTNAME}")
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        client.will_set(self.t_avail, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client

    def start(self) -> None:
        if self.client is None:
            return
        log(f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER or '(anonymous)'}")
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        if self.client is None:
            return
        try:
            self.client.publish(self.t_avail, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()

    def _post(self, item: tuple) -> None:
        self.inbox.put(item)
        try:
            os.write(self.wake_fd, b"x")
        except OSError:
            pass

    def _on_connect(self, client, _userdata, _flags, reason_code, _props) -> None:
        if reason_code.is_failure:
            log(f"MQTT connect refused: {reason_code}")
            return
        client.subscribe(self.t_set, qos=1)
        self._post(("connected",))

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props) -> None:
        log(f"MQTT disconnected ({reason_code}); paho will reconnect")

    def _on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace").strip().upper()
        if payload in ("ON", "OFF"):
            self._post(("set", payload == "ON", bool(msg.retain)))

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self.client is not None:
            self.client.publish(topic, payload, qos=1, retain=retain)

    def announce(self, state: str, power: str) -> None:
        """Discovery configs + availability + current state, on every
        (re)connect. Entity ids come from device name + entity name:
        switch.frame_stairs_screen, sensor.frame_stairs_tv_power."""
        node = f"piframe_{HOSTNAME}"
        device = {
            "identifiers": [node],
            "name": HOSTNAME,
            "manufacturer": "woozlescape",
            "model": "piframe-cec",
            "sw_version": repo_sha(),
        }
        switch = {
            "name": "Screen",
            "unique_id": f"{node}_screen",
            "command_topic": self.t_set,
            "state_topic": self.t_state,
            "payload_on": "ON",
            "payload_off": "OFF",
            "retain": True,
            "availability_topic": self.t_avail,
            "icon": "mdi:television",
            "device": device,
        }
        sensor = {
            "name": "TV power",
            "unique_id": f"{node}_tv_power",
            "state_topic": self.t_power,
            "availability_topic": self.t_avail,
            "entity_category": "diagnostic",
            "icon": "mdi:hdmi-port",
            "device": device,
        }
        self.publish(f"{DISCOVERY_PREFIX}/switch/{node}/screen/config", json.dumps(switch))
        self.publish(f"{DISCOVERY_PREFIX}/sensor/{node}/tv_power/config", json.dumps(sensor))
        self.publish(self.t_avail, "online")
        self.publish(self.t_state, state)
        self.publish(self.t_power, power)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------
class Service:
    def __init__(self) -> None:
        self.running = True
        self.adapters = open_adapters(follower=True)
        self.state_file = Path(os.environ.get("STATE_DIRECTORY") or Path.home() / ".local/state/piframe-cec") / "state.json"
        self.desired = self._load_desired()
        self.power = "disconnected"
        self.inbox: "queue.Queue[tuple]" = queue.Queue()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        self.ha = HaLink(self.inbox, self.wake_w)
        self.timers: Dict[str, Tuple[float, Callable[[], None]]] = {}
        self.target: Optional[bool] = None   # last commanded state being verified
        self.resends = 0
        # A fresh boot (power cut, reboot) wakes the screen once the TV is
        # reachable, if it is meant to be on. A plain restart does not.
        self.wake_reason: Optional[str] = "pi booted" if uptime_seconds() < BOOT_WINDOW_SECONDS else None

    # persistence ----------------------------------------------------------
    def _load_desired(self) -> bool:
        try:
            return json.loads(self.state_file.read_text()).get("screen", "ON") == "ON"
        except (OSError, ValueError, AttributeError):
            return True  # frames are meant to be on

    def _save_desired(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"screen": "ON" if self.desired else "OFF"}))
            tmp.replace(self.state_file)
        except OSError as exc:
            log(f"cannot persist desired state: {exc}")

    # timers ---------------------------------------------------------------
    def schedule(self, key: str, delay: float, fn: Callable[[], None]) -> None:
        self.timers[key] = (time.monotonic() + delay, fn)

    def _run_timers(self) -> None:
        now = time.monotonic()
        for key, (due, fn) in list(self.timers.items()):
            if due <= now and self.timers.get(key, (None,))[0] == due:
                del self.timers[key]
                fn()

    def _next_timeout_ms(self) -> int:
        if not self.timers:
            return 1000
        soonest = min(due for due, _fn in self.timers.values())
        return max(0, min(1000, int((soonest - time.monotonic()) * 1000)))

    # state ----------------------------------------------------------------
    @property
    def active(self) -> Optional[Adapter]:
        return connected(self.adapters)

    def set_power(self, power: str) -> None:
        if power != self.power:
            log(f"TV power: {self.power} -> {power}")
            self.power = power
            self.ha.publish(self.ha.t_power, power)
            self.ha.publish(self.ha.t_state, "ON" if power == "on" else "OFF")

    def poll_power(self) -> str:
        ad = self.active
        self.set_power(ad.power_status() if ad else "disconnected")
        return self.power

    def _poll_loop(self) -> None:
        self.poll_power()
        self.schedule("poll", POWER_POLL_SECONDS, self._poll_loop)

    # commands -------------------------------------------------------------
    def command(self, on: bool, why: str, resend: bool = False) -> None:
        if not resend:
            self.resends = 0
        ad = self.active
        if ad is None:
            log(f"{'wake' if on else 'standby'} skipped ({why}): no TV connected")
            return
        log(f"{'wake' if on else 'standby'} ({why}) via {ad.path}, input {pa_str(ad.pa)}")
        acked = ad.wake() if on else ad.standby()
        if not acked:
            log("  TV did not acknowledge")
        self.target = on
        self.schedule("verify", VERIFY_SECONDS, self._verify)

    def _verify(self) -> None:
        if self.target is None:
            return
        power = self.poll_power()
        reached = (power == "on") if self.target else (power != "on")
        if reached or self.target != self.desired:
            self.target, self.resends = None, 0
            return
        if self.resends >= MAX_RESENDS:
            log(f"TV still reports {power} after {MAX_RESENDS} resends; giving up until the next change")
            self.target, self.resends = None, 0
            return
        self.resends += 1
        self.command(self.target, f"resend {self.resends}, TV reports {power}", resend=True)

    def apply_desired(self, on: bool, retained: bool) -> None:
        changed = on != self.desired
        self.desired = on
        if changed:
            self._save_desired()
        # A retained value replayed on (re)connect only acts when it differs
        # from what we already had; a live command from HA always acts.
        if changed or not retained:
            self.command(on, "home assistant" + (" (while offline)" if retained else ""))

    # CEC events + messages ------------------------------------------------
    def on_pa_change(self, ad: Adapter, pa: int) -> None:
        now = time.monotonic()
        old = ad.pa
        ad.pa = pa
        if pa == old:
            return
        log(f"{ad.path}: physical address {pa_str(old)} -> {pa_str(pa)}")
        if pa == PA_INVALID:
            ad.invalid_since, ad.valid_since = now, None
            if self.active is None:
                self.set_power("disconnected")
            return
        gone_for = now - ad.invalid_since if ad.invalid_since is not None else 0.0
        ad.valid_since = now
        if gone_for >= PA_GONE_SECONDS and self.wake_reason is None:
            self.wake_reason = f"TV power returned after {gone_for:.0f}s"
        self.schedule("settle", PA_SETTLE_SECONDS, self._settled)

    def _settled(self) -> None:
        ad = self.active
        if ad is None or ad.valid_since is None or time.monotonic() - ad.valid_since < PA_SETTLE_SECONDS - 0.05:
            return
        try:
            ad.configure(osd_name())
        except OSError as exc:
            log(f"{ad.path}: configure failed: {exc}")
        if self.wake_reason and self.desired:
            self.command(True, self.wake_reason)
        self.wake_reason = None
        self.schedule("poll", 1.0, self._poll_loop)

    def on_message(self, ad: Adapter, msg: bytes) -> None:
        if len(msg) < 2:
            return
        src, dest, op = msg[0] >> 4, msg[0] & 0xF, msg[1]
        mine = ad.log_addr()
        directed = dest != LA_BROADCAST
        if op == OP_REQUEST_ACTIVE_SOURCE or (op == OP_SET_STREAM_PATH and len(msg) >= 4
                                              and (msg[2] << 8 | msg[3]) == ad.pa):
            # The TV is looking for its source (it just woke, or selected our
            # input): answer so it lands on the kiosk.
            ad.active_source()
        elif op == OP_GIVE_DEVICE_POWER_STATUS and directed:
            ad.transmit(src, bytes([OP_REPORT_POWER_STATUS, 0x00]))
        elif op == OP_MENU_REQUEST and directed:
            ad.transmit(src, bytes([OP_MENU_STATUS, 0x01]))
        elif op == OP_GIVE_OSD_NAME and directed:
            ad.transmit(src, bytes([OP_SET_OSD_NAME]) + osd_name().encode())
        elif op == OP_REPORT_POWER_STATUS and len(msg) >= 3:
            self.set_power(POWER_NAMES.get(msg[2], f"unknown-{msg[2]}"))
        elif op in (OP_STANDBY, OP_ROUTING_CHANGE, OP_ACTIVE_SOURCE):
            # Someone used the TV remote (or another source took over):
            # refresh soon, don't fight it.
            self.schedule("poll", 2.0, self._poll_loop)
        elif directed and dest == mine and op not in REPLY_OPCODES:
            ad.transmit(src, bytes([OP_FEATURE_ABORT, op, 0x00]))

    # main loop ------------------------------------------------------------
    def run(self) -> None:
        if not self.adapters:
            log("no /dev/cec* adapters; nothing to do")
            while self.running:
                time.sleep(60)
            return
        for ad in self.adapters:
            try:
                ad.configure(osd_name())
            except OSError as exc:
                log(f"{ad.path}: configure failed: {exc}")
        ad = self.active
        log(f"started: host={HOSTNAME} osd={osd_name()!r} adapters={[a.path for a in self.adapters]} "
            f"connected={ad.path + ' ' + pa_str(ad.pa) if ad else 'none'} desired={'ON' if self.desired else 'OFF'} "
            f"boot-wake={'yes' if self.wake_reason else 'no'}")
        if ad is not None:
            self.schedule("settle", PA_SETTLE_SECONDS, self._settled)
        else:
            self.set_power("disconnected")
        self.ha.start()

        poller = select.poll()
        by_fd = {a.fd: a for a in self.adapters}
        for a in self.adapters:
            poller.register(a.fd, select.POLLIN | select.POLLPRI)
        poller.register(self.wake_r, select.POLLIN)

        while self.running:
            try:
                ready = poller.poll(self._next_timeout_ms())
            except InterruptedError:
                continue
            for fd, ev in ready:
                if fd == self.wake_r:
                    try:
                        os.read(self.wake_r, 512)
                    except OSError:
                        pass
                    continue
                a = by_fd[fd]
                if ev & select.POLLPRI:
                    got = a.dqevent()
                    if got and got[0] == CEC_EVENT_STATE_CHANGE:
                        self.on_pa_change(a, got[1])
                    elif got and got[0] == CEC_EVENT_LOST_MSGS:
                        log(f"{a.path}: kernel dropped messages (queue overflow)")
                if ev & select.POLLIN:
                    msg = a.receive()
                    if msg:
                        self.on_message(a, msg)
            while True:
                try:
                    item = self.inbox.get_nowait()
                except queue.Empty:
                    break
                if item[0] == "connected":
                    log("MQTT connected")
                    self.ha.announce("ON" if self.power == "on" else "OFF", self.power)
                elif item[0] == "set":
                    self.apply_desired(item[1], item[2])
            self._run_timers()
        self.ha.stop()
        for a in self.adapters:
            a.close()

    def stop(self, *_args: object) -> None:
        self.running = False
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI helpers (bootstrap check + hand debugging)
# ---------------------------------------------------------------------------
def cli(action: str) -> int:
    adapters = open_adapters(follower=False)
    ad = connected(adapters)
    if ad is None:
        print("CEC: no TV connected on any HDMI port (or no /dev/cec*)")
        return 3
    # The service normally owns registration; claim here only if it hasn't.
    deadline = time.monotonic() + 5
    while ad.log_addr() is None and time.monotonic() < deadline:
        try:
            ad.configure(osd_name())
        except OSError:
            pass
        time.sleep(0.3)
    if action == "on":
        ad.wake()
    elif action == "off":
        ad.standby()
    if action in ("on", "off"):
        time.sleep(VERIFY_SECONDS)
    vendor = ad.vendor()
    power = ad.power_status()
    for a in adapters:
        a.close()
    if vendor is None and power == "no-reply":
        print(f"CEC: {ad.path} input {pa_str(ad.pa)} - TV NOT answering CEC. "
              "Enable HDMI-CEC on the TV (Samsung: Anynet+) and check the cable.")
        return 2
    vname = VENDOR_NAMES.get(vendor, "unknown vendor") if vendor is not None else "vendor unknown"
    vhex = f" 0x{vendor:06x}" if vendor is not None else ""
    print(f"CEC: {ad.path} input {pa_str(ad.pa)} - TV answering ({vname}{vhex}), power {power}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe", action="store_true", help="report the connected TV and exit")
    group.add_argument("--on", action="store_true", help="wake the TV and exit")
    group.add_argument("--off", action="store_true", help="put the TV in standby and exit")
    args = parser.parse_args()
    if args.probe or args.on or args.off:
        return cli("on" if args.on else "off" if args.off else "probe")
    service = Service()
    signal.signal(signal.SIGTERM, service.stop)
    signal.signal(signal.SIGINT, service.stop)
    service.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
