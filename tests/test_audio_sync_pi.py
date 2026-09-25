"""Whole-house synced audio: Pi-side unit tests.

No Pi, mpv, or network required - the companion mpv IPC socket and
subprocess are always faked (monkeypatched seams), and the discipline
thread is never actually started; `_discipline_tick()` /
`MpvCompanion._resync()` are called directly and synchronously with a
scripted clock + reader so the tests run instantly.

Run from the piframe-client repo root:
    python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked -
# `python -m pytest tests -q` from the repo root already puts it on
# sys.path[0], but a bare `pytest tests` (different rootdir insertion
# rules) would not.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import audio_sync
import piframe_client


D2 = [10.0, 20.0]  # cycle 30s; tracks start at 0, 10


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTime:
    """Stands in for the `time` module inside piframe_client for the
    discipline/resync tests - `sleep()` advances the same counter
    `time()`/`monotonic()` read, so a real wait never happens but the
    resync release-wait loop still converges deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(float(seconds), 0.0)


class FakeClock:
    """Drop-in for ClockSync in discipline/resync tests - reads the
    same FakeTime counter so time.sleep() calls inside _resync also
    advance what server_now() reports."""

    def __init__(self, faketime: FakeTime, offset: float = 0.0) -> None:
        self._faketime = faketime
        self._offset = offset

    def server_now(self) -> float:
        return self._faketime.t + self._offset

    def offset(self) -> float:
        return self._offset

    def estimate(self):
        return (self._offset, 0.01)


class FakeReader:
    """Fake `_CompanionIPCReader`. `properties[name]` is either a plain
    value (returned every call) or a zero-arg callable (invoked fresh
    each call, so a test can script a value that changes across a
    wait-loop's polls)."""

    def __init__(self, properties=None) -> None:
        self.properties = dict(properties or {})
        self.commands: list[list] = []
        self.property_calls: list[str] = []

    def get_property(self, name: str, timeout: float = 1.0):
        self.property_calls.append(name)
        value = self.properties.get(name)
        return value() if callable(value) else value

    def command(self, cmd) -> bool:
        self.commands.append(list(cmd))
        return True


def _bypass_mpv(companion: "piframe_client.MpvCompanion", monkeypatch) -> list:
    """Stub the process-lifecycle + fire-and-forget IPC seams so
    `load()`/`stop()` never touch a real subprocess or socket, and never
    start the (real, threaded) discipline loop. Returns the list every
    `_send_command` call is recorded into."""
    monkeypatch.setattr(companion, "_ensure_running", lambda: True)
    monkeypatch.setattr(companion, "_is_running", lambda: True)
    monkeypatch.setattr(companion, "_ensure_discipline_thread", lambda: None)
    sent: list = []

    def fake_send(cmd):
        sent.append(list(cmd))
        return True

    monkeypatch.setattr(companion, "_send_command", fake_send)
    return sent


# ---------------------------------------------------------------------------
# The mirror imports
# ---------------------------------------------------------------------------


def test_mirror_module_imports_with_only_stdlib():
    assert audio_sync.CADENCE_S == 0.5
    assert audio_sync.ARM_LEAD_S == 0.4
    assert audio_sync.discipline_action(0.0, 99.0) == ("none", 1.0)
    assert audio_sync.cycle_length(D2) == 30.0


def test_piframe_client_imports_cleanly():
    # Import-time side effects (env-var reads, Path() construction) must
    # stay side-effect-free on a non-Pi dev machine - no subprocess, no
    # socket, no filesystem writes at module scope.
    assert hasattr(piframe_client, "MpvCompanion")
    assert hasattr(piframe_client, "ClockSync")


# ---------------------------------------------------------------------------
# ClockSync
# ---------------------------------------------------------------------------


def test_clock_sync_before_first_sample_offset_is_zero():
    clock = piframe_client.ClockSync()
    assert clock.estimate() is None
    assert clock.offset() == 0.0


def test_clock_sync_estimate_from_scripted_pongs():
    clock = piframe_client.ClockSync()
    # Three fast, low-RTT rounds agree on ~+20ms; one slow, asymmetric
    # round disagrees - clock_offset() should favor the fast ones.
    pings = [
        (10.000, 10.020, 10.004),
        (11.000, 11.021, 11.005),
        (12.000, 12.019, 12.006),
        (13.000, 13.080, 13.120),
    ]
    for t0, server_time, t3 in pings:
        clock.add_sample(t0, server_time, t3)
    expected_samples = [audio_sync.clock_sample(*p) for p in pings]
    assert clock.estimate() == audio_sync.clock_offset(expected_samples)
    off, rtt = clock.estimate()
    assert off == pytest.approx(0.020, abs=0.002)
    assert clock.offset() == pytest.approx(off)
    assert clock.server_now() == pytest.approx(piframe_client.time.time() + off, abs=0.05)


def test_clock_sync_window_caps_at_clock_window_samples():
    clock = piframe_client.ClockSync()
    for i in range(audio_sync.CLOCK_WINDOW + 5):
        clock.add_sample(float(i), float(i) + 0.01, float(i) + 0.02)
    assert len(clock._samples) == audio_sync.CLOCK_WINDOW


# ---------------------------------------------------------------------------
# MpvCompanion.load() - same-session guard vs a genuine new session
# ---------------------------------------------------------------------------


def _sync(session="sess-a", epoch=1000.0, durations=None, latency_ms=0):
    return {
        "session": session,
        "epoch": epoch,
        "durations": list(durations if durations is not None else D2),
        "latency_ms": latency_ms,
    }


def test_new_synced_session_loads_paused_with_pitch_correction_off(monkeypatch):
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    sent = _bypass_mpv(companion, monkeypatch)

    ok = companion.load(["a.mp3", "b.mp3"], repeat=True, sync=_sync())

    assert ok is True
    assert ["set_property", "audio-pitch-correction", "no"] in sent
    assert ["set_property", "pause", True] in sent
    assert ["loadfile", "a.mp3", "replace"] in sent
    assert ["loadfile", "b.mp3", "append"] in sent
    assert companion._sync["session"] == "sess-a"
    assert companion._loaded_items == ["a.mp3", "b.mp3"]
    assert companion._self_paused is True
    assert companion._armed is False


def test_same_session_reroute_does_not_reload(monkeypatch):
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    sent = _bypass_mpv(companion, monkeypatch)
    companion.load(["a.mp3", "b.mp3"], repeat=True, sync=_sync(latency_ms=0))
    sent.clear()

    # Same session id, same items, mpv still "running" - the server
    # re-sending the start message (visual swap, re-route) must not
    # touch mpv at all; only latency_ms is refreshed.
    ok = companion.load(["a.mp3", "b.mp3"], repeat=True, sync=_sync(latency_ms=40))

    assert ok is True
    assert sent == []
    assert companion._sync["latency_ms"] == 40


def test_different_session_id_reloads(monkeypatch):
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    sent = _bypass_mpv(companion, monkeypatch)
    companion.load(["a.mp3", "b.mp3"], repeat=True, sync=_sync(session="sess-a"))
    sent.clear()

    companion.load(["a.mp3", "b.mp3"], repeat=True, sync=_sync(session="sess-b"))

    assert ["set_property", "pause", True] in sent
    assert ["loadfile", "a.mp3", "replace"] in sent
    assert companion._sync["session"] == "sess-b"
    assert companion._armed is False  # a fresh session always re-arms


def test_leaving_sync_for_unsynced_load_resets_speed_and_pitch_correction(monkeypatch):
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    sent = _bypass_mpv(companion, monkeypatch)
    companion.load(["a.mp3"], repeat=True, sync=_sync(durations=[10.0]))
    sent.clear()

    companion.load(["c.mp3"], repeat=True, sync=None)

    assert ["set_property", "speed", 1.0] in sent
    assert ["set_property", "audio-pitch-correction", "yes"] in sent
    assert ["set_property", "pause", False] in sent
    assert companion._sync is None


def test_stop_clears_sync_state_even_when_mpv_not_running(monkeypatch):
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    _bypass_mpv(companion, monkeypatch)
    companion.load(["a.mp3"], repeat=True, sync=_sync(durations=[10.0]))
    generation_before = companion._generation

    # mpv "crashed" between load() and stop() - stop() must still drop
    # the synced-session bookkeeping so a stale discipline resync can't
    # release into a session that no longer exists.
    monkeypatch.setattr(companion, "_is_running", lambda: False)
    companion.stop()

    assert companion._sync is None
    assert companion._generation != generation_before


# ---------------------------------------------------------------------------
# Discipline step
# ---------------------------------------------------------------------------


def _armed_companion(monkeypatch, faketime, *, armed: bool, properties):
    companion = piframe_client.MpvCompanion(FakeClock(faketime))
    companion._sync = _sync(durations=D2)
    companion._loaded_items = ["a.mp3", "b.mp3"]
    companion._armed = armed
    companion._discipline_running = True
    companion._ipc = FakeReader(properties)
    return companion


def test_discipline_arming_seeks_to_timeline_offset_and_releases(monkeypatch):
    faketime = FakeTime(start=1005.0)  # epoch=1000, so pos=5.0s into track 0
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=False,  # not yet armed -> arming branch even though pos >= 0
        properties={"pause": False, "playlist-pos": 0, "playback-time": 5.0},
    )

    companion._discipline_tick()

    reader = companion._ipc
    # mark = 1005.0 + ARM_LEAD_S(0.4) = 1005.4 -> cycle_pos = 5.4s into
    # track 0 (durations [10, 20], starts [0, 10]).
    assert ["set_property", "pause", True] in reader.commands
    seeks = [c for c in reader.commands if c[0] == "seek"]
    assert len(seeks) == 1
    assert seeks[0][1] == pytest.approx(5.4, abs=1e-6)
    assert seeks[0][2] == "absolute+exact"
    assert reader.commands[-2:] == [
        ["set_property", "pause", False],
        ["set_property", "speed", 1.0],
    ]
    assert companion._armed is True
    assert companion._self_paused is False
    assert companion._last_action == "arming"
    # Release happened once server_now caught up to the mark.
    assert faketime.t == pytest.approx(1005.4, abs=1e-6)


def test_discipline_arming_prerolls_to_track_zero_before_epoch(monkeypatch):
    faketime = FakeTime(start=998.0)  # before epoch=1000 -> pre-roll
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=False,
        properties={"pause": False, "playlist-pos": 0, "playback-time": 0.0},
    )

    companion._discipline_tick()

    reader = companion._ipc
    seeks = [c for c in reader.commands if c[0] == "seek"]
    assert len(seeks) == 1
    # Pre-roll releases exactly at epoch (0, 0), not ARM_LEAD_S from now.
    assert seeks[0][1] == pytest.approx(0.0, abs=1e-6)
    assert companion._armed is True
    assert faketime.t == pytest.approx(1000.0, abs=1e-6)


def test_discipline_nudge_sets_speed(monkeypatch):
    faketime = FakeTime(start=1005.0)
    monkeypatch.setattr(piframe_client, "time", faketime)
    # Timeline target (idx 0, off 5.0); player reports 4.85 -> 0.15s
    # behind -> nudge speeds up (clamped to the +0.5% ceiling).
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=True,
        properties={"pause": False, "playlist-pos": 0, "playback-time": 4.85},
    )

    companion._discipline_tick()

    reader = companion._ipc
    speed_cmds = [c for c in reader.commands if c[:2] == ["set_property", "speed"]]
    assert len(speed_cmds) == 1
    assert speed_cmds[0][2] == pytest.approx(1.005, abs=1e-6)
    assert companion._last_action == "nudge"
    assert companion._last_rate == pytest.approx(1.005, abs=1e-6)
    # No pause/seek - a nudge is a speed tweak, not a resync.
    assert all(c[0] != "seek" for c in reader.commands)


def test_discipline_none_playback_time_skips_not_zero(monkeypatch):
    faketime = FakeTime(start=1005.0)
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=True,
        properties={"pause": False, "playlist-pos": 0, "playback-time": None},
    )

    companion._discipline_tick()

    reader = companion._ipc
    assert companion._last_action == "skip"
    assert reader.commands == []  # no speed/seek write from a None read


def test_discipline_operator_pause_reports_paused_and_stops_early(monkeypatch):
    faketime = FakeTime(start=1005.0)
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=True,
        properties={"pause": True, "playlist-pos": 0, "playback-time": 5.0},
    )
    companion._err_window.append(0.2)  # stale error from before the pause

    companion._discipline_tick()

    reader = companion._ipc
    assert companion._last_action == "paused"
    assert reader.commands == []  # never touches mpv - it's the operator's pause
    assert "playlist-pos" not in reader.property_calls  # short-circuits after the pause read
    assert list(companion._err_window) == []  # cleared so resume doesn't react to stale drift


def test_discipline_no_sync_loaded_is_a_noop(monkeypatch):
    faketime = FakeTime(start=1005.0)
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = piframe_client.MpvCompanion(FakeClock(faketime))
    companion._ipc = FakeReader({})

    companion._discipline_tick()  # must not raise with no session loaded

    assert companion._ipc.property_calls == []


def test_sync_heartbeat_none_when_not_synced():
    companion = piframe_client.MpvCompanion(piframe_client.ClockSync())
    assert companion.sync_heartbeat() is None


def test_sync_heartbeat_reports_session_and_latency(monkeypatch):
    faketime = FakeTime(start=1005.0)
    monkeypatch.setattr(piframe_client, "time", faketime)
    companion = _armed_companion(
        monkeypatch,
        faketime,
        armed=True,
        properties={"pause": False, "playlist-pos": 0, "playback-time": 5.0},
    )
    hb = companion.sync_heartbeat()
    assert hb["session"] == "sess-a"
    assert hb["latency_ms"] == 0
    assert hb["action"] == companion._last_action
