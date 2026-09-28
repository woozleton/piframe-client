"""piframe_cec's display rescue: power-cycle a TV that stays on with an
unreadable EDID (the kiosk stuck on a squished 1024x768 fallback) - and,
because the frames must work with any TV, never anything else: never a TV
that is off, meant to be off, without CEC, that ignores standby, or that
hasn't shown it can be woken again."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# piframe_cec talks to /dev/cec* through fcntl ioctls at runtime only; a stub
# lets the tests import it on any OS.
sys.modules.setdefault("fcntl", types.ModuleType("fcntl"))

import piframe_cec as cec  # noqa: E402


def _drm(tmp_path, *, status="connected", edid=b""):
    port = tmp_path / "card1-HDMI-A-2"
    port.mkdir(parents=True, exist_ok=True)
    (port / "status").write_text(status + "\n")
    (port / "edid").write_bytes(edid)
    return tmp_path


def test_display_stuck_and_readable_read_the_kernel(tmp_path):
    assert cec.display_stuck(_drm(tmp_path)) and not cec.display_readable(tmp_path)
    good = _drm(tmp_path, edid=b"\x00\xff\xff\xff\xff\xff\xff\x00")
    assert not cec.display_stuck(good) and cec.display_readable(good)
    gone = _drm(tmp_path, status="disconnected")
    assert not cec.display_stuck(gone) and not cec.display_readable(gone)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _Ha:
    t_power, t_state = "power", "state"

    def publish(self, *_a, **_k):
        pass


def _service(monkeypatch, tmp_path=None, *, stuck=True, desired=True, power="on", reachable=True):
    svc = object.__new__(cec.Service)
    svc.desired, svc.power, svc.target = desired, power, None
    svc.stuck_since, svc.rescue_next, svc.rescue_attempts = None, 0.0, 0
    svc.off_since, svc.standby_reachable = None, reachable
    svc.state_file = (tmp_path or Path("/nonexistent")) / "state.json"
    svc.ha = _Ha()
    svc.timers = {}
    svc.commands = []
    clock = _Clock()
    monkeypatch.setattr(cec.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cec, "display_stuck", lambda: svc.stuck)
    monkeypatch.setattr(cec, "display_readable", lambda: svc.readable if svc.readable is not None else not svc.stuck)
    svc.readable = None  # default: readable whenever not stuck
    monkeypatch.setattr(cec, "log", lambda *a: None)
    svc.stuck = stuck
    monkeypatch.setattr(cec.Service, "command", lambda self, on, why, resend=False: self.commands.append(on))
    return svc, clock


def _run_for(svc, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        clock.now += cec.RESCUE_CHECK_SECONDS
        svc._rescue_check()
        due = svc.timers.get("rescue-wake")
        if due and due[0] <= clock.now:
            del svc.timers["rescue-wake"]
            due[1]()


# --- when it acts ---------------------------------------------------------------


def test_a_tv_stuck_for_three_minutes_is_power_cycled(monkeypatch):
    svc, clock = _service(monkeypatch)
    _run_for(svc, clock, 150)
    assert svc.commands == []  # display_watch gets its chance first
    _run_for(svc, clock, 90)
    assert svc.commands == [False, True]  # off, then on after RESCUE_OFF_SECONDS
    _run_for(svc, clock, 1800)
    assert svc.commands == [False, True]  # still stuck: not again within the hour
    _run_for(svc, clock, 2400)
    assert svc.commands == [False, True, False, True]


def test_two_cycles_that_did_not_help_stop_until_the_edid_reads_again(monkeypatch):
    svc, clock = _service(monkeypatch)
    _run_for(svc, clock, 3 * 3600)
    assert svc.commands == [False, True, False, True]  # RESCUE_MAX_ATTEMPTS, then no more
    svc.stuck = False  # the TV answers again: a fresh episode later
    _run_for(svc, clock, 60)
    svc.stuck = True
    _run_for(svc, clock, 3600)
    assert svc.commands == [False, True, False, True, False, True]


def test_a_tv_that_just_disconnects_does_not_reset_the_limit(monkeypatch):
    """Only a readable EDID starts a fresh episode - a TV that drops HDMI
    during each cycle must not earn itself another two."""
    svc, clock = _service(monkeypatch)
    _run_for(svc, clock, 3 * 3600)
    assert len(svc.commands) == 4
    svc.stuck, svc.readable = False, False  # disconnected for a while
    _run_for(svc, clock, 120)
    svc.stuck, svc.readable = True, None
    _run_for(svc, clock, 3 * 3600)
    assert len(svc.commands) == 4


# --- when it never acts ----------------------------------------------------------


def test_a_tv_never_seen_answering_in_standby_is_never_cycled(monkeypatch):
    """It might drop off the bus in standby and never come back - or, like
    the mirror's Vizio, ignore standby. Either way: hands off."""
    svc, clock = _service(monkeypatch, reachable=False)
    _run_for(svc, clock, 3 * 3600)
    assert svc.commands == []


def test_a_tv_that_is_off_or_silent_is_never_woken(monkeypatch):
    for power in ("standby", "to-standby", "to-on", "no-reply", "disconnected"):
        svc, clock = _service(monkeypatch, power=power)
        _run_for(svc, clock, 900)
        assert svc.commands == [], power


def test_a_screen_meant_to_be_off_is_left_alone(monkeypatch):
    svc, clock = _service(monkeypatch, desired=False)
    _run_for(svc, clock, 900)
    assert svc.commands == []


def test_a_healthy_tv_is_never_touched(monkeypatch):
    svc, clock = _service(monkeypatch, stuck=False)
    _run_for(svc, clock, 7200)
    assert svc.commands == []


def test_home_assistant_turning_the_screen_off_mid_rescue_wins(monkeypatch):
    svc, clock = _service(monkeypatch)
    _run_for(svc, clock, 210)
    assert svc.commands[:1] == [False]
    svc.desired = False  # HA said off during the 20s off window
    _run_for(svc, clock, 60)
    assert svc.commands == [False]


def test_no_rescue_while_another_command_is_being_verified(monkeypatch):
    svc, clock = _service(monkeypatch)
    svc.target = True
    _run_for(svc, clock, 900)
    assert svc.commands == []


# --- learning that a TV stays reachable in standby ----------------------------------


def test_standby_reachability_needs_two_minutes_of_answers_while_off(monkeypatch, tmp_path):
    svc, clock = _service(monkeypatch, tmp_path, reachable=False)
    svc.set_power("to-standby")
    clock.now += 60
    svc.set_power("to-standby")
    assert not svc.standby_reachable
    clock.now += 70
    svc.set_power("to-standby")
    assert svc.standby_reachable
    assert json.loads(svc.state_file.read_text())["standby_reachable"] is True  # survives restarts


def test_a_tv_that_drops_off_the_bus_in_standby_is_not_learned(monkeypatch, tmp_path):
    """'to-standby' once, then gone: never learned."""
    svc, clock = _service(monkeypatch, tmp_path, reachable=False)
    svc.set_power("to-standby")
    for _ in range(10):
        clock.now += 30
        svc.set_power("disconnected")
    svc.set_power("to-standby")  # a new off period starts from here
    assert not svc.standby_reachable
