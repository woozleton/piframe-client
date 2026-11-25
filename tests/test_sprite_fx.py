"""How a guest comes in and goes out: an entity's `fx` is kept whole when every key in it is right, dropped whole
(the entity kept, drawn as it always was) when any one is wrong, and absent stays absent; the status says so."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import piframe_client  # noqa: E402

FX = {"in": "slide", "out": "slide", "s": 0.6, "dy": 420.5, "out_at": 1700000020.25}


def _entity(**extra):
    return {"id": "card",
            "sprite": {"kind": "circle", "size": 60, "color": "#ffc03c"},
            "motion": {"mode": "pinned", "x_frac": 0.2, "y_frac": 0.8},
            **extra}


def _extract(raw):
    return piframe_client.PiFrameClient._extract_sprite_entity(0, raw)


@pytest.mark.parametrize("fx", [
    FX,
    {"in": "fade", "out": "fade", "s": 0.6, "out_at": 1700000020},
    {"in": "fade", "s": 5},
    {"out": "slide", "s": 0.001, "dy": 0, "out_at": 1},
    {"in": "slide", "out": "fade", "s": 2, "dy": 20000},
])
def test_a_valid_fx_is_kept_as_sent(fx):
    entity = _extract(_entity(fx=fx))
    assert entity["fx"] == fx
    assert all(isinstance(entity["fx"][k], float) for k in ("s", "dy", "out_at") if k in fx)


def test_only_the_five_keys_are_passed_on():
    entity = _extract(_entity(fx=dict(FX, ease="bounce", extra=[1])))
    assert entity["fx"] == FX


@pytest.mark.parametrize("change", [
    {"in": "wipe"}, {"out": "zoom"}, {"in": None}, {"out": ""}, {"in": True},
    {"s": None}, {"s": 0}, {"s": -0.6}, {"s": 5.01}, {"s": "0.6"}, {"s": True}, {"s": float("nan")},
    {"s": float("inf")},
    {"dy": -1}, {"dy": 20000.5}, {"dy": "420"}, {"dy": False}, {"dy": float("nan")}, {"dy": None},
    {"out_at": 0}, {"out_at": -5}, {"out_at": "1700000020"}, {"out_at": float("inf")}, {"out_at": True},
    {"out_at": None},
])
def test_any_wrong_key_drops_the_whole_fx_and_keeps_the_entity(change):
    entity = _extract(_entity(fx={**FX, **change}))
    assert entity == _extract(_entity())
    assert "fx" not in entity


def test_an_fx_without_s_takes_the_usual_0_6():
    entity = _extract(_entity(fx={"in": "fade", "out": "fade", "out_at": 1700000020}))
    assert entity["fx"] == {"in": "fade", "out": "fade", "s": 0.6, "out_at": 1700000020.0}


def test_an_fx_without_a_kind_is_no_fx():
    assert "fx" not in _extract(_entity(fx={"s": 0.6, "dy": 100, "out_at": 1700000020}))


@pytest.mark.parametrize("fx", [None, "fade", 0.6, ["fade"], True])
def test_an_fx_that_is_not_an_object_is_no_fx(fx):
    assert _extract(_entity(fx=fx)) == _extract(_entity())


def test_an_entity_without_fx_is_unchanged():
    assert _extract(_entity()) == {
        "id": "card",
        "sprite": {"kind": "circle", "size": 60.0, "color": "#ffc03c"},
        "motion": {"mode": "pinned", "x_frac": 0.2, "y_frac": 0.8, "phase_s": 0.0, "flip": False},
    }


def test_fx_survives_the_whole_show():
    show = {"epoch": 1.0, "latency_ms": 0,
            "world": {"w": 4000, "h": 1000}, "window": {"x": 0, "y": 0, "w": 500, "h": 1000},
            "entities": [_entity(fx=FX), {**_entity(), "id": "photo", "fx": dict(FX)}, {**_entity(), "id": "fish"}]}
    shown = piframe_client.PiFrameClient._extract_sprite_show(show)
    assert [e.get("fx") for e in shown["entities"]] == [FX, FX, None]


def test_the_status_says_this_frame_plays_fx():
    assert piframe_client.SPRITE_FX == 1
    source = Path(piframe_client.__file__).read_text(encoding="utf-8")
    assert '"sprite_fx": SPRITE_FX' in source
