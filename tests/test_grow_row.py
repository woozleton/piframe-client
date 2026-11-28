"""A longer clock-synced row for the same playlist swaps in place on the page (see test_kiosk_page.py). The frame
says it can (`"features": ["grow_row"]` when it registers and in every status), and the page sees the manager's
playlist name, which is what tells it the longer row is the same playlist."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import piframe_client  # noqa: E402


def test_the_features_are_one_constant():
    assert piframe_client.CLIENT_FEATURES == ("grow_row",)


def test_register_says_it(monkeypatch):
    monkeypatch.setattr(piframe_client, "_collect_system_metrics", lambda: None)
    sent = []
    client = SimpleNamespace(
        config=SimpleNamespace(client_id="frame-1", name="Frame", group="Group"),
        system_info={},
        _start_status_updates=lambda: None,
        renderer=SimpleNamespace(set_banner=lambda message: None, is_running=True),
    )
    piframe_client.PiFrameClient.on_open(client, SimpleNamespace(send=sent.append))
    assert json.loads(sent[0])["features"] == ["grow_row"]


def test_the_status_loop_sends_it():
    source = inspect.getsource(piframe_client.PiFrameClient._status_loop)
    assert '"features": list(CLIENT_FEATURES),' in source


def test_a_slideshow_carries_its_playlist_name_to_the_page(monkeypatch):
    calls = []
    renderer = SimpleNamespace(
        _set_items_state=lambda **kw: calls.append(kw) or True,
        _pick_existing_idle_media=lambda path: "",
    )
    piframe_client.BrowserController.play_slideshow(
        renderer, ["/mnt/nas/a.jpg"], 10.0, sync={"mode": "clock", "length": 1}, playlist_name="Garden",
    )
    piframe_client.BrowserController.play_slideshow(renderer, ["/mnt/nas/a.jpg"], 10.0)
    assert [c["playlist_name"] for c in calls] == ["Garden", "Mixed Playlist"]
