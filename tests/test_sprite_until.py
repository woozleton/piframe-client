"""A guest's own end: an entity's `until` (epoch seconds) is kept when it is a finite number above 0 and left off
otherwise, without dropping the entity; an entity without one is what it always was, and the status says so."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import piframe_client  # noqa: E402


def _entity(**extra):
    return {"id": "banner",
            "sprite": {"kind": "circle", "size": 60, "color": "#ffc03c"},
            "motion": {"mode": "pinned", "x_frac": 0.2, "y_frac": 0.8},
            **extra}


def _extract(raw):
    return piframe_client.PiFrameClient._extract_sprite_entity(0, raw)


@pytest.mark.parametrize("until", [1700000020, 1700000020.5, 0.001])
def test_a_real_until_is_kept(until):
    entity = _extract(_entity(until=until))
    assert entity["until"] == float(until)
    assert isinstance(entity["until"], float)


@pytest.mark.parametrize("until", [None, True, False, "1700000020", 0, -5, float("nan"), float("inf"),
                                   float("-inf"), [1700000020], {"t": 1}])
def test_any_other_until_is_left_off_and_the_entity_kept(until):
    entity = _extract(_entity(until=until))
    assert entity == _extract(_entity())
    assert "until" not in entity


def test_an_entity_without_until_is_unchanged():
    assert _extract(_entity()) == {
        "id": "banner",
        "sprite": {"kind": "circle", "size": 60.0, "color": "#ffc03c"},
        "motion": {"mode": "pinned", "x_frac": 0.2, "y_frac": 0.8, "phase_s": 0.0, "flip": False},
    }


def test_until_survives_the_whole_show():
    show = {"epoch": 1.0, "latency_ms": 0,
            "world": {"w": 4000, "h": 1000}, "window": {"x": 0, "y": 0, "w": 500, "h": 1000},
            "entities": [_entity(until=1700000020), {**_entity(), "id": "fish"}]}
    shown = piframe_client.PiFrameClient._extract_sprite_show(show)
    assert [e.get("until") for e in shown["entities"]] == [1700000020.0, None]


def test_the_status_says_this_frame_ends_guests_itself():
    assert piframe_client.SPRITE_UNTIL == 1
    source = Path(piframe_client.__file__).read_text(encoding="utf-8")
    assert '"sprite_until": SPRITE_UNTIL' in source
