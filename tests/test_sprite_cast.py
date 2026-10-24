"""A cast of up to 256 creatures: the frame takes it whole, says so in its status, and keeps it in a file of its own
that the page reads only when it changes."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import piframe_client  # noqa: E402


def _show(count):
    return {
        "epoch": 1.0, "latency_ms": 0,
        "world": {"w": 4000, "h": 1000}, "window": {"x": 0, "y": 0, "w": 500, "h": 1000},
        "entities": [{"id": f"e{i}", "sprite": {"kind": "circle", "size": 60, "color": "#ffc03c"},
                      "motion": {"mode": "sweep", "sweep_s": 20, "y_period_s": 15, "y_amp_frac": 0.3,
                                 "phase_s": -i * 0.1}} for i in range(count)],
    }


def test_a_cast_of_256_is_taken_whole_and_one_more_is_not():
    assert piframe_client.SPRITE_MAX_ENTITIES == 256
    shown = piframe_client.PiFrameClient._extract_sprite_show(_show(256))
    assert shown is not None and len(shown["entities"]) == 256
    assert piframe_client.PiFrameClient._extract_sprite_show(_show(257)) is None


def test_the_creatures_go_in_a_file_of_their_own(tmp_path, monkeypatch):
    monkeypatch.setattr(piframe_client, "BROWSER_SPRITE_FILE", tmp_path / "sprite.json")
    written = []
    renderer = SimpleNamespace(_state_lock=threading.Lock(), _state={"mode": "idle"}, _sprite_seq=0)
    renderer._write_state = lambda: written.append(dict(renderer._state))

    piframe_client.BrowserController.set_sprite(renderer, {"entities": ["a", "b"]})
    assert json.loads((tmp_path / "sprite.json").read_text(encoding="utf-8")) == {"entities": ["a", "b"]}
    assert written[-1] == {"mode": "idle", "sprite_seq": 1}
    piframe_client.BrowserController.set_sprite(renderer, {"entities": ["c"]})
    assert written[-1] == {"mode": "idle", "sprite_seq": 2}
    piframe_client.BrowserController.set_sprite(renderer, None)
    assert written[-1] == {"mode": "idle"}


def test_the_status_and_the_page_carry_it():
    source = Path(piframe_client.__file__).read_text(encoding="utf-8")
    assert '"sprite_entities": SPRITE_MAX_ENTITIES' in source
    page = Path(piframe_client.__file__).with_name("browser_renderer_template.py").read_text(encoding="utf-8")
    assert "const SPRITE_MAX_ENTITIES = 256;" in page
    # Read only when its number changes, its boxes found by id, each route measured once.
    assert "if (state.sprite_seq !== spriteSeq)" in page
    assert "const held = spriteSlotById.get(id);" in page
    assert "const cum = spriteRouteLengths(pts, worldW, worldH);" in page
