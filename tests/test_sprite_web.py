"""Web sprite pictures: an https address on one photo site (SPRITE_WEB_HOSTS) is kept exactly as sent and loaded by
the kiosk itself; every other web address is refused, a local path works as before, and the status says so."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import piframe_client  # noqa: E402


def _entity(url, **extra):
    return {"id": "photo",
            "sprite": {"kind": "image", "url": url, "w": 420, "h": 280, **extra},
            "motion": {"mode": "pinned", "x_frac": 0.2, "y_frac": 0.8}}


def _sprite(url, **extra):
    entity = piframe_client.PiFrameClient._extract_sprite_entity(0, _entity(url, **extra))
    return entity["sprite"] if entity else None


@pytest.mark.parametrize("url", [
    "https://t.plnspttrs.net/12345/1234567_abcdef_280.jpg",
    "https://cdn.planespotters.net/photo/1/2/3_t.jpg?x=1&y=%20a",
    "https://img2.t.plnspttrs.net/Some%20Path/x.jpg",
])
def test_a_planespotters_picture_is_kept_exactly_as_sent(url):
    assert piframe_client.SPRITE_WEB_HOSTS == ("t.plnspttrs.net", "cdn.planespotters.net")
    assert _sprite(url) == {"kind": "image", "url": url, "src": url, "w": 420.0, "h": 280.0}


@pytest.mark.parametrize("url", [
    "http://t.plnspttrs.net/x.jpg",
    "https://example.com/x.jpg",
    "https://t.plnspttrs.net.evil.example/x.jpg",
    "https://evilt.plnspttrs.net/x.jpg",
    "https://a@t.plnspttrs.net/x.jpg",
    "https://a:b@t.plnspttrs.net/x.jpg",
    "https://t.plnspttrs.net:443/x.jpg",
    "https://t.plnspttrs.net:8443/x.jpg",
    "https://evil.example\\.t.plnspttrs.net/x.jpg",
    "https://t.plnspttrs.net/x y.jpg",
    "HTTPS://t.plnspttrs.net/x.jpg",
])
def test_any_other_web_address_is_refused(url, capsys):
    assert _sprite(url) is None
    assert "reason=sprite_url_unusable" in capsys.readouterr().out


def test_a_web_picture_ignores_an_anim_and_stays_a_still():
    url = "https://t.plnspttrs.net/1/x.jpg"
    assert _sprite(url, anim={"frames": 8, "fps": 12}) == {
        "kind": "image", "url": url, "src": url, "w": 420.0, "h": 280.0}
    # Even a broken one: it is ignored, not a reason to drop the picture.
    assert _sprite(url, anim="junk")["src"] == url


def test_a_web_picture_still_needs_its_size():
    entity = _entity("https://t.plnspttrs.net/1/x.jpg")
    entity["sprite"]["w"] = 0
    assert piframe_client.PiFrameClient._extract_sprite_entity(0, entity) is None


def test_a_local_path_works_as_before(monkeypatch):
    monkeypatch.setattr(piframe_client, "_media_file_src", lambda url: "file://" + url)
    sprite = _sprite("/mnt/nas/_mural/sprites/card.png", anim={"frames": 4, "fps": 8})
    assert sprite == {"kind": "image", "url": "/mnt/nas/_mural/sprites/card.png",
                      "src": "file:///mnt/nas/_mural/sprites/card.png", "w": 420.0, "h": 280.0,
                      "anim": {"frames": 4, "fps": 8.0}}


def test_the_status_says_this_frame_shows_web_pictures():
    assert piframe_client.SPRITE_WEB == 1
    source = Path(piframe_client.__file__).read_text(encoding="utf-8")
    assert '"sprite_web": SPRITE_WEB' in source


def test_a_later_entity_draws_on_top_whatever_box_it_gets():
    page = Path(piframe_client.__file__).with_name("browser_renderer_template.py").read_text(encoding="utf-8")
    assert "slot.el.style.zIndex = String(i);" in page
