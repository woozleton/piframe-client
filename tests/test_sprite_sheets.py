"""Sprite sheets on the wire: any number of frames, a grid (`cols`) and a play order (`seq`), checked per entity."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import piframe_client  # noqa: E402


def _entity(anim):
    return {"id": "e0",
            "sprite": {"kind": "image", "url": "/mnt/nas/_mural/sprites/fish.png", "w": 240, "h": 140,
                       "anim": anim},
            "motion": {"mode": "sweep", "sweep_s": 20, "y_period_s": 15, "y_amp_frac": 0.3, "phase_s": 0}}


@pytest.fixture(autouse=True)
def _local_src(monkeypatch):
    # The file:// src is the kiosk's own business; these tests are about the anim block.
    monkeypatch.setattr(piframe_client, "_media_file_src", lambda url: "file://" + url)


def _anim(anim):
    entity = piframe_client.PiFrameClient._extract_sprite_entity(0, _entity(anim))
    return entity["sprite"]["anim"] if entity else None


def test_a_sheet_of_any_length_in_a_grid_and_its_play_order():
    seq = [0, 1, 2, 3] * 10 + [4, 5] + [0] * 20
    assert _anim({"frames": 40, "fps": 25, "cols": 8}) == {"frames": 40, "fps": 25.0, "cols": 8}
    assert _anim({"frames": 6, "fps": 24, "seq": seq}) == {"frames": 6, "fps": 24.0, "seq": seq}
    # The old one-row strip reads as it always did.
    assert _anim({"frames": 8, "fps": 11.11}) == {"frames": 8, "fps": 11.11}
    assert _anim({"frames": 8, "fps": 12, "cols": 8}) == {"frames": 8, "fps": 12.0}


@pytest.mark.parametrize("anim", [
    {"frames": 1, "fps": 10},
    {"frames": 8, "fps": 31},
    {"frames": 8, "fps": 12, "cols": 9},
    {"frames": 8, "fps": 12, "cols": 0},
    {"frames": 8, "fps": 12, "seq": [0, 8]},
    {"frames": 8, "fps": 12, "seq": [3]},
    {"frames": 8, "fps": 12, "seq": [0, True]},
])
def test_a_broken_anim_drops_only_that_creature(anim, capsys):
    assert piframe_client.PiFrameClient._extract_sprite_entity(0, _entity(anim)) is None
    assert "reason=anim_" in capsys.readouterr().out


def test_the_template_steps_a_sheet_by_column_row_and_play_order():
    page = Path(piframe_client.__file__).with_name("browser_renderer_template.py").read_text(encoding="utf-8")
    assert "anim.seq[spriteMod(tick, anim.seq.length)]" in page
    assert "(-((index % anim.cols) * boxW)).toFixed(1)" in page
    assert "(Math.ceil(anim.frames / anim.cols) * boxH).toFixed(1)" in page
    assert piframe_client.SPRITE_SHEETS == 1
