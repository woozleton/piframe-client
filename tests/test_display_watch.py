"""display_watch: the kiosk's rotation + resolution watcher.

The failure it fixes: office-back's TV answered no EDID when the HDMI link
came up, the kernel offered only fallback modes, cage settled on 1024x768,
and the 16:9 TV stretched that 4:3 page - pictures squished between dark
bars, the mural creatures stretched with them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import display_watch as dw


def _mode(w, h, refresh=60.0, preferred=False, current=False):
    return {"width": w, "height": h, "refresh": refresh, "preferred": preferred, "current": current}


FALLBACK = [_mode(1024, 768, 60.004002, current=True), _mode(800, 600, 60.317001), _mode(640, 480, 59.939999)]


def _tv_1080(current=(1920, 1080)):
    modes = [_mode(1920, 1080, 60.0, preferred=True), _mode(1920, 1080, 59.939999), _mode(1280, 720, 60.0),
             _mode(1024, 768, 60.004002)]
    for m in modes:
        m["current"] = (m["width"], m["height"]) == current and m["refresh"] in (60.0, 60.004002)
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
    modes = [_mode(3840, 2160, 60.0, preferred=True), _mode(1920, 1080, 60.0), _mode(1920, 1080, 59.939999)]
    assert (dw.desired_mode(modes, "auto")["width"], dw.desired_mode(modes, "auto")["refresh"]) == (1920, 60.0)
    assert dw.desired_mode(modes, "native")["width"] == 3840


def test_no_edid_means_nothing_to_switch_to():
    assert dw.desired_mode(FALLBACK, "auto") is None
    assert dw.desired_mode(FALLBACK, "native") is None


# --- planning the fix ----------------------------------------------------------


def test_a_healthy_output_is_left_alone():
    assert dw.plan(_output(_tv_1080()), "90", "auto") == []


def test_the_fallback_mode_is_switched_once_the_tv_answers():
    stuck = _output(_tv_1080(current=(1024, 768)))
    assert dw.plan(stuck, "90", "auto") == ["--mode", "1920x1080@60.000Hz", "--transform", "90"]


def test_without_an_edid_only_the_rotation_is_kept():
    assert dw.plan(_output(FALLBACK), "90", "auto") == []
    assert dw.plan(_output(FALLBACK, transform="normal"), "90", "auto") == ["--transform", "90"]


def test_a_lost_rotation_comes_back_with_the_mode_in_one_call():
    assert dw.plan(_output(_tv_1080(), transform="normal"), "90", "auto") == ["--transform", "90"]


# --- the EDID check -------------------------------------------------------------


def test_edid_missing_reads_the_connector(tmp_path):
    (tmp_path / "status").write_text("connected\n")
    (tmp_path / "edid").write_bytes(b"")
    assert dw.edid_missing(tmp_path)
    (tmp_path / "edid").write_bytes(b"\x00\xff\xff\xff\xff\xff\xff\x00")
    assert not dw.edid_missing(tmp_path)
    (tmp_path / "status").write_text("disconnected\n")
    (tmp_path / "edid").write_bytes(b"")
    assert not dw.edid_missing(tmp_path)


# --- the watcher over time ---------------------------------------------------------


class _Fleet:
    """Fake compositor + kernel for one output."""

    def __init__(self, monkeypatch, output, edid=False):
        self.output = output
        self.edid = edid
        self.reprobes, self.replugs, self.applied = [], [], []
        monkeypatch.setattr(dw, "read_outputs", lambda _w: [self.output])
        monkeypatch.setattr(dw, "connector_for", lambda name: Path("/fake") / name)
        monkeypatch.setattr(dw, "edid_missing", lambda _c: not self.edid)
        monkeypatch.setattr(dw, "reprobe", lambda c: self.reprobes.append(c) or True)
        monkeypatch.setattr(dw, "replug", lambda c: self.replugs.append(c) or True)
        monkeypatch.setattr(dw, "apply", lambda _w, name, args: self.applied.append(list(args)) or True)
        monkeypatch.setattr(dw, "log", lambda *a, **k: None)


def test_stuck_output_is_asked_again_every_30s_then_recovers(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(FALLBACK))
    w = dw.Watcher("wlr-randr", "90", "auto")
    w.tick(0.0)
    w.tick(10.0)
    assert len(fleet.reprobes) == 1  # not every second
    w.tick(31.0)
    assert len(fleet.reprobes) == 2
    assert fleet.applied == []  # nothing to switch to yet, rotation is fine

    # The TV answers: the kernel has the EDID, the compositor still only the
    # fallback list - one replug so it rebuilds the list.
    fleet.edid = True
    w.tick(40.0)
    w.tick(41.0)
    assert len(fleet.replugs) == 1

    # Back as a fresh output at the TV's mode but unrotated: rotation restored.
    fleet.output = _output(_tv_1080(), transform="normal")
    w.tick(42.0)
    assert fleet.applied == [["--transform", "90"]]


def test_a_healthy_frame_is_never_replugged_or_probed(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(_tv_1080()), edid=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 120, 1):
        w.tick(float(t))
    assert fleet.reprobes == [] and fleet.replugs == [] and fleet.applied == []


def test_a_fix_that_does_not_take_is_retried_slowly(monkeypatch):
    fleet = _Fleet(monkeypatch, _output(_tv_1080(), transform="normal"), edid=True)
    w = dw.Watcher("wlr-randr", "90", "auto")
    for t in range(0, 10):
        w.tick(float(t))
    assert len(fleet.applied) == 2  # t=0 and t=5, not ten times
