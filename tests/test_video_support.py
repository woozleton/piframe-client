"""The frame says what videos it plays (`video_support` in every status), and takes .avi / .wmv / .flv for video it
can't play rather than for a broken picture.

The status dict is built inline in PiFrameClient._status_loop (it needs a live connection), so these test the
function it calls, _video_support(), which takes nothing: what it reports can't depend on what is playing."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import piframe_client  # noqa: E402
from piframe_client import BrowserController  # noqa: E402


def test_video_support_is_exactly_the_contract():
    assert piframe_client._video_support() == {
        "codecs": ["av1", "h264", "vp8", "vp9"],
        "containers": ["m4v", "mkv", "mov", "mp4", "webm"],
        "max_edge": 1920,
        "max_edge_by_codec": {"av1": 3840},
    }


def test_video_support_takes_nothing_and_is_a_fresh_copy_each_time():
    assert inspect.signature(piframe_client._video_support).parameters == {}
    first = piframe_client._video_support()
    first["codecs"].append("hevc")
    first["max_edge_by_codec"]["hevc"] = 1
    assert piframe_client._video_support()["codecs"] == ["av1", "h264", "vp8", "vp9"]
    assert piframe_client._video_support()["max_edge_by_codec"] == {"av1": 3840}


def test_the_status_loop_sends_it():
    source = inspect.getsource(piframe_client.PiFrameClient._status_loop)
    assert '"video_support": _video_support(),' in source


def test_the_containers_are_the_playable_video_extensions():
    containers = piframe_client._video_support()["containers"]
    assert containers == sorted(e.lstrip(".") for e in piframe_client.VIDEO_PLAYABLE_EXTENSIONS)
    for ext in ("avi", "wmv", "flv"):
        assert ext not in containers


def test_avi_wmv_and_flv_are_video_to_the_page():
    for name in ("clip.avi", "CLIP.WMV", "clip.flv", "/mnt/nas/x/clip.avi?x=1"):
        assert BrowserController._item_kind(name) == "video", name
        assert BrowserController._is_audio_only(name) is False


def test_the_other_kinds_are_unchanged():
    assert [BrowserController._item_kind(n) for n in (
        "a.mp4", "a.mkv", "a.webm", "a.mov", "a.m4v", "a.mp3", "a.flac",
        "a.jpg", "a.png", "a.gif", "a.heic", "idle.html", "a.unknown",
    )] == ["video"] * 7 + ["image"] * 4 + ["html", "image"]
