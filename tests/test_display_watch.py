"""display_watch: the kiosk's rotation + resolution watcher.

The failure it fixes: office-back's TV answered no EDID when the HDMI link
came up, the kernel offered only fallback modes, cage settled on 1024x768,
and the 16:9 TV stretched that 4:3 page - pictures squished between dark
bars, the mural creatures stretched with them.

The constraint it keeps: a TV that identifies itself normally - a Frame or
anything else (the mirror's Vizio) - is never touched beyond the rotation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import display_watch as dw


def _mode(w, h, refresh=60.0, preferred=False, current=False):
    return {"width": w, "height": h, "refresh": refresh, "preferred": preferred, "current": current}


FALLBACK = [_mode(1024, 768, 60.004002, current=True), _mode(800, 600, 60.317001), _mode(640, 480, 59.939999)]


def _tv(preferred=(1920, 1080, 60.0), current=None):
    """A TV's list as wlroots reports it once the EDID was read."""
    pw, ph, pr = preferred
    modes = [_mode(pw, ph, pr, preferred=True), _mode(1920, 1080, 60.0), _mode(1920, 1080, 59.939999),
             _mode(1280, 720, 60.0), _mode(1024, 768, 60.004002)]
    cur = current or preferred
    for m in modes:
        m["current"] = False
    next(m for m in modes if (m["width"], m["height"], m["refresh"]) == cur)["current"] = True
    return modes


def _output(modes, transform="90", name="HDMI-A-2"):
    return {"name": name, "enabled": True, "transform": transform, "modes": modes}


# --- choosing the mode -------------------------------------------------------


def test_parse_mode():
    assert dw.parse_mode("1920x1080@60") == (1920, 1080, 60.0)
    assert dw.parse_mode("1920x1080@59.94Hz") == (1920, 1080, 59.94)
    assert dw.parse_mode("1920x1080") == (1920, 1080, 60.0)
    assert dw.parse_mode("auto") is None and dw.parse_mode("native") is None and dw.parse_mode("") is None


def test_auto_caps_a_4k_tv_at_1080p_and_native_keeps_it():
    modes = _tv(preferred=(3840, 2160, 60.0))
    assert (dw.desired_mode(modes, "auto")["width"], dw.desired_mode(modes, "auto")["refresh"]) == (1920, 60.0)
    assert dw.desired_mode(modes, "native")["width"] == 3840


def test_no_edid_means_nothing_to_switch_to():
    assert dw.desired_mode(FALLBACK, "auto") is None
    assert dw.desired_mode(FALLBACK, "native") is None


# --- planning the fix ------------------------------------------------------------


def test_a_healthy_output_is_left_alone():
    assert dw.plan(_output(_tv()), "90", "auto") == []


def test_a_tv_that_picked_another_refresh_is_not_second_guessed():
    """Not recovering, rotation fine: the resolution is the TV's business."""
    other = _output(_tv(current=(1920, 1080, 59.939999)))
    assert dw.plan(other, "90", "auto") == []


def test_while_recovering_the_fallback_mode_is_switched_to_the_tvs():
    stuck = _output(_tv(current=(1024, 768, 60.004002)))
    assert dw.plan(stuck, "90", "auto") == []  # not recovering: hands off
    assert dw.plan(stuck, "90", "auto", recovering=True) == ["--mode", "1920x1080@60.000Hz", "--transform", "90"]


def test_without_an_edid_only_the_rotation_is_kept():
    assert dw.plan(_output(FALLBACK), "90", "auto", recovering=True) == []
    assert dw.plan(_output(FALLBACK, transform="normal"), "90", "auto") == ["--transform", "90"]


def test_a_lost_rotation_comes_back_with_the_configured_mode_in_one_call():
    """The old watcher's behavior: after a TV resume a 4K Frame is back at
    2160p and unrotated - auto puts 1080p back together with the rotation."""
    resumed = _output(_tv(preferred=(3840, 2160, 60.0)), transform="normal")
    assert dw.plan(resumed, "90", "auto") == ["--mode", "1920x1080@60.000Hz", "--transform", "90"]
    assert dw.plan(_output(_tv(), transform="normal"), "90", "auto") == ["--transform", "90"]


# --- the kernel's view -------------------------------------------------------------


def _connector(tmp_path, status="connected", edid=b"", modes="1920x1080\n1920x1080\n1280x720\n"):
    (tmp_path / "status").write_text(status + "\n")
    (tmp_path / "edid").write_bytes(edid)
    (tmp_path / "modes").write_text(modes)
    return tmp_path


def test_edid_missing_reads_the_connector(tmp_path):
    assert dw.edid_missing(_connector(tmp_path))
    assert not dw.edid_missing(_connector(tmp_path, edid=b"\x00\xff\xff\xff\xff\xff\xff\x00"))
    assert not dw.edid_missing(_connector(tmp_path, status="disconnected"))


def test_compositor_behind_only_when_it_lacks_the_kernels_mode(tmp_path):
    conn = _connector(tmp_path, edid=b"\x00" * 128)
    assert dw.compositor_behind(conn, _output(FALLBACK))  # built while the EDID was unreadable
    assert not dw.compositor_behind(conn, _output(_tv()))  # up to date


# --- the watcher over time -------------------------------------------------------------


class _Fleet:
    """Fake compositor + kernel for one output."""

    def __init__(self, monkeypatch, output, edid=False, behind=False, saved=False):
        self.output = output
        self.edid = edid
        self.behind = behind
        self.saved = saved  # a kept copy of the TV's EDID on disk
        self.connected = True
        self.reprobes, self.replugs, self.applied = [], [], []
        self.kept, self.loads, self.clears = [], [], []
        monkeypatch.setattr(dw, "read_outputs", lambda _w: [self.output] if self.output else [])
        monkeypatch.setattr(dw, "connector_for", lambda name: Path("/fake") / name)
        monkeypatch.setattr(dw, "edid_missing", lambda _c: not self.edid)
        monkeypatch.setattr(dw, "compositor_behind", lambda _c, _o: self.behind)
        monkeypatch.setattr(dw, "reprobe", lambda c: self.reprobes.append(c) or True)
        monkeypatch.setattr(dw, "replug", lambda c: self.replugs.append(c) or True)
        monkeypatch.setattr(dw, "apply", lambda _w, name, args: self.applied.append(list(args)) or True)
        monkeypatch.setattr(dw, "log", lambda *a, **k: None)
        monkeypatch.setattr(dw, "keep_edid", lambda c, name: self.kept.append(name) or False)
        monkeypatch.setattr(dw, "saved_edid", lambda name: type("P", (), {"exists": lambda _s: self.saved})())
        monkeypatch.setattr(dw, "load_saved_edid", self._load)
        monkeypatch.setattr(dw, "clear_saved_edid", lambda c: self.clears.append(c) or True)
        monkeypatch.setattr(dw, "port_connected", lambda _c: self.connected)

    def _load(self, connector, name):
        """The kernel takes the kept copy: the port reads an EDID again."""
        self.loads.append(name)
        self.edid = True
        return True


def test_stuck_output_is_asked_again_every_30s_then_recovers(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(FALLBACK))
    w = dw.Watcher("wlr-randr", "90", "auto")
    w.tick(0.0)
    w.tick(10.0)
    assert len(fleet.reprobes) == 1  # not every second
    w.tick(31.0)
    assert len(fleet.reprobes) == 2
    assert fleet.applied == []  # nothing to switch to yet, rotation is fine

    # The TV answers; the compositor's list is behind: one replug.
    fleet.edid, fleet.behind = True, True
    w.tick(40.0)
    w.tick(41.0)
    assert len(fleet.replugs) == 1

    # Back as a fresh output at the TV's mode but unrotated: rotation restored.
    fleet.output, fleet.behind = _output(_tv(), transform="normal"), False
    w.tick(42.0)
    assert fleet.applied == [["--transform", "90"]]


def test_recovery_without_a_replug_switches_the_mode(monkeypatch):
    """If the compositor already got the TV's modes, no replug - just the switch."""
    fleet = _Fleet(monkeypatch, _output(FALLBACK))
    w = dw.Watcher("wlr-randr", "90", "auto")
    w.tick(0.0)
    fleet.edid = True
    fleet.output = _output(_tv(current=(1024, 768, 60.004002)))
    w.tick(1.0)
    assert fleet.replugs == []
    assert fleet.applied == [["--mode", "1920x1080@60.000Hz", "--transform", "90"]]
    fleet.output = _output(_tv())  # the switch took
    for t in range(2, 60):
        w.tick(float(t))
    assert len(fleet.applied) == 1  # done - hands off again


def test_a_normal_tv_is_never_probed_replugged_or_resized(monkeypatch):
    """A Frame or any other TV that identifies itself: over two minutes the
    watcher does nothing at all."""
    fleet = _Fleet(monkeypatch, _output(_tv(current=(1920, 1080, 59.939999))), edid=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 120):
        w.tick(float(t))
    assert fleet.reprobes == [] and fleet.replugs == [] and fleet.applied == []


def test_an_odd_tv_is_replugged_at_most_once(monkeypatch):
    """A TV whose kernel mode the compositor never lists: one replug at the
    kiosk start, never a loop."""
    fleet = _Fleet(monkeypatch, _output(_tv()), edid=True, behind=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 300):
        w.tick(float(t))
    assert len(fleet.replugs) == 1


def test_recovery_can_be_switched_off(monkeypatch):
    """PIFRAME_DISPLAY_RECOVERY=0: rotation watch only, like before."""
    fleet = _Fleet(monkeypatch, _output(FALLBACK, transform="normal"))
    w = dw.Watcher("wlr-randr", "90", "auto", recover=False)
    for t in range(0, 60):
        w.tick(float(t))
    assert fleet.reprobes == [] and fleet.replugs == []
    assert fleet.applied and all(args == ["--transform", "90"] for args in fleet.applied)


def test_a_fix_that_does_not_take_is_retried_slowly(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(_tv(), transform="normal"), edid=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 10):
        w.tick(float(t))
    assert len(fleet.applied) == 2  # t=0 and t=5, not ten times


# --- the TV's own EDID, kept -------------------------------------------------------


def test_keep_edid_keeps_a_real_edid_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "STATE_DIR", tmp_path / "state")
    real = dw.EDID_HEADER + bytes(248)
    conn = _connector(tmp_path, edid=real)
    assert dw.keep_edid(conn, "HDMI-A-2")
    assert dw.saved_edid("HDMI-A-2").read_bytes() == real
    assert not dw.keep_edid(conn, "HDMI-A-2")  # unchanged: not written again
    assert not dw.keep_edid(_connector(tmp_path, edid=bytes(128)), "HDMI-A-2")  # not an EDID
    assert not dw.keep_edid(_connector(tmp_path, edid=b""), "HDMI-A-2")
    assert dw.saved_edid("HDMI-A-2").read_bytes() == real
    other = dw.EDID_HEADER + b"" + bytes(119)
    assert dw.keep_edid(_connector(tmp_path, edid=other), "HDMI-A-2")  # a new TV on the port


def test_the_kept_copy_goes_to_the_ports_debugfs_file():
    assert dw._override_file(Path("/sys/class/drm/card1-HDMI-A-2")) == Path("/sys/kernel/debug/dri/1/HDMI-A-2/edid_override")


def test_a_tv_that_answers_has_its_edid_kept_every_30s(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(_tv()), edid=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 61):
        w.tick(float(t))
    assert fleet.kept == ["HDMI-A-2"] * 3  # t=0, 30, 60


def test_a_silent_tv_gets_its_kept_edid_after_a_minute_once(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(FALLBACK), saved=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 60):
        w.tick(float(t))
    assert fleet.loads == [] and len(fleet.reprobes) == 2  # asks the TV first
    fleet.behind = True
    w.tick(60.0)
    assert fleet.loads == ["HDMI-A-2"]
    w.tick(61.0)  # the kernel has an EDID again: the usual recovery
    assert len(fleet.replugs) == 1
    fleet.output, fleet.behind = _output(_tv()), False
    for t in range(62, 300):
        w.tick(float(t))
    assert fleet.loads == ["HDMI-A-2"] and fleet.clears == []
    assert fleet.kept == []  # the loaded copy is never kept as the TV's


def test_without_a_kept_copy_it_only_keeps_asking(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(FALLBACK))
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 120):
        w.tick(float(t))
    assert fleet.loads == [] and len(fleet.reprobes) == 4


def test_a_disconnect_drops_the_kept_copy_and_asks_the_tv_first(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(FALLBACK), saved=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 61):
        w.tick(float(t))
    fleet.output = _output(_tv())
    w.tick(61.0)
    # The TV sleeps: wlroots drops the output, the kernel says disconnected.
    fleet.output, fleet.connected = None, False
    w.tick(62.0)
    w.tick(63.0)
    assert len(fleet.clears) == 1
    # It wakes up silent again: a minute of asking it before the copy.
    fleet.output, fleet.connected, fleet.edid = _output(FALLBACK), True, False
    for t in range(64, 124):
        w.tick(float(t))
    assert fleet.loads == ["HDMI-A-2"]
    w.tick(124.0)
    assert fleet.loads == ["HDMI-A-2"] * 2


def test_a_moment_off_the_list_while_still_connected_keeps_the_copy(monkeypatch):
    """The replug drops the output from wlroots for a moment; the kernel still
    says connected, so the copy stays."""
    fleet = _Fleet(monkeypatch, _output(FALLBACK), saved=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 61):
        w.tick(float(t))
    fleet.output = None
    w.tick(61.0)
    assert fleet.clears == []
