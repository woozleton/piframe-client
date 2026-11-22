"""The kiosk page's own logic, run in Node with a fake DOM and fake timers.

No Pi, Chromium or network: tests/kiosk_page_harness.js loads the page's
script from render_browser_html() and plays a scenario against it (state
files, image loads, video frames, time passing). Skipped where Node is
not installed.

Run from the piframe-client repo root:
    python -m pytest tests -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from browser_renderer_template import render_browser_html  # noqa: E402

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("kiosk_page_harness.js")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node to run the kiosk page")


def _page_script() -> str:
    html = render_browser_html(
        rotation_degrees=0,
        show_hud=False,
        transition_duration_ms=500,
        state_file_uri="file:///tmp/piframe_browser_state.json",
        nas_root="/mnt/nas",
        poll_ms=250,
        event_endpoint="http://127.0.0.1:18888/event",
    )
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    return scripts[-1]


def run_page(tmp_path: Path, scenario: str) -> list:
    page = tmp_path / "page.js"
    page.write_text(_page_script(), encoding="utf-8")
    scen = tmp_path / "scenario.js"
    scen.write_text(scenario, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(HARNESS), str(page), str(scen)],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def item(kind: str, name: str) -> dict:
    return {"kind": kind, "src": f"file:///media/{name}", "label": name, "is_audio": False}


def slideshow(names, *, interval=10.0, banner=None) -> dict:
    return {
        "mode": "playlist", "playlist_name": "Mixed Playlist",
        "items": [item("image", n) for n in names],
        "repeat": True, "loop": False, "interval": interval, "shuffle": False, "sync": None,
        "idle_item": None, "banner": banner, "volume": 0, "muted": True,
    }


def videos(names, *, repeat=True) -> dict:
    single = len(names) == 1
    return {
        "mode": "single" if single else "playlist",
        "playlist_name": "Single Video" if single else "Video Playlist",
        "items": [item("video", n) for n in names],
        "repeat": repeat, "loop": repeat if single else False, "interval": 5.0,
        "shuffle": False, "sync": None, "idle_item": None, "banner": None, "volume": 0, "muted": True,
    }


# Shared scenario helpers: find the element playing a file, read the banner.
PRELUDE = """
const img = (name) => stages.map((s) => s.image).find((i) => i.src === "file:///media/" + name);
const vid = (name) => stages.map((s) => s.video).find((v) => v.src === "file:///media/" + name);
const banner = () => bannerEl.classList.contains("visible") ? bannerEl.textContent : "";
const last = (type) => events.filter((e) => e.type === type).slice(-1)[0];
"""


def scenario(body: str, **states) -> str:
    consts = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in states.items())
    return PRELUDE + consts + body


# ---------------------------------------------------------------- Fix 1

def test_a_missing_pictures_banner_goes_when_the_next_picture_shows(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
await loadImage(img("gone.jpg"), false);
const during = banner();
await advance(10000);
await loadImage(img("good.jpg"), true);
report({during, after: banner(), index: activeIndex});
""", s=slideshow(["gone.jpg", "good.jpg", "good2.jpg"],
                 banner={"message": "Media file missing", "level": "warning"})))
    assert out == [{"during": "Media file missing", "after": "", "index": 1}]


def test_a_picture_that_loads_after_its_decode_wait_still_takes_the_banner_down(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
await loadImage(img("gone.jpg"), false);
await advance(10000);
await advance(1500);  // the decode wait runs out: the slot shows before the file lands
const before = banner();
await loadImage(img("slow.jpg"), true);
report({before, after: banner()});
""", s=slideshow(["gone.jpg", "slow.jpg"])))
    assert out == [{"before": "Media file missing", "after": ""}]


def test_a_list_of_only_missing_files_keeps_saying_so(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
const seen = [];
for (const name of ["a.jpg", "b.jpg", "a.jpg"]) {
  await loadImage(img(name), false);
  seen.push(banner());
  await advance(5000);
  seen.push(banner());  // between failures, before the next one errors
  await advance(5000);
}
report(seen);
""", s=slideshow(["a.jpg", "b.jpg"])))
    assert out == [["Media file missing"] * 6]


def test_a_good_picture_leaves_other_banners_up(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
await loadImage(img("good.jpg"), true);
report(banner());
""", s=slideshow(["good.jpg", "good2.jpg"],
                 banner={"message": "Server disconnected", "level": "error"})))
    assert out == ["Server disconnected"]


def test_a_video_that_plays_takes_down_the_banner_and_a_failed_videos_message(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
await loadImage(img("gone.jpg"), false);
await advance(10000);
const bad = vid("bad.mp4");
bad.onerror();
const failing = osdEl.classList.contains("visible") ? osdValueEl.textContent : "";
await advance(3500);
await loadVideo(vid("good.mp4"));
report({failing, banner: banner(), osd: osdEl.classList.contains("visible")});
""", s={**slideshow(["gone.jpg"]),
        "items": [item("image", "gone.jpg"), item("video", "bad.mp4"), item("video", "good.mp4")]}))
    assert out == [{"failing": "This video can't be played on this display", "banner": "", "osd": False}]


# ---------------------------------------------------------------- Fix 2

def test_a_new_list_led_by_the_video_on_screen_keeps_it_playing(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(first);
advancePlaylist(1);  // a.mp4, second in the list, is on screen
await flush();
const a = vid("a.mp4");
await loadVideo(a);
a.currentTime = 42;
const srcSets = a.srcSets, loads = a.loadCalls;
await setState(pruned);
const kept = {
  same: vid("a.mp4") === a, srcSets: a.srcSets - srcSets, loads: a.loadCalls - loads,
  time: a.currentTime, paused: a.paused, active: stages[activeStageIndex].video === a,
  index: activeIndex, count: activeState.items.length, reported: last("slideshow_index").index,
};
a.onended();
await flush();
report({kept, next: !!vid("c.mp4"), nextIndex: activeIndex});
""", first=videos(["x.mp4", "a.mp4", "b.mp4", "c.mp4"]), pruned=videos(["a.mp4", "c.mp4", "x.mp4"])))
    assert out == [{
        "kept": {"same": True, "srcSets": 0, "loads": 0, "time": 42, "paused": False, "active": True,
                 "index": 0, "count": 3, "reported": 0},
        "next": True, "nextIndex": 1,
    }]


def test_a_list_pruned_to_the_video_on_screen_loops_it(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(first);
const a = vid("a.mp4");
await loadVideo(a);
const srcSets = a.srcSets;
await setState(pruned);
report({loop: a.loop, srcSets: a.srcSets - srcSets, mode: stages[activeStageIndex].playState.mode});
""", first=videos(["a.mp4", "b.mp4"]), pruned=videos(["a.mp4"])))
    assert out == [{"loop": True, "srcSets": 0, "mode": "single"}]


def test_a_paused_video_stays_paused_and_says_so(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(first);
const a = vid("a.mp4");
await loadVideo(a);
applyControl({action: "pause", token: 1});
await setState(pruned);
report({paused: a.paused, reported: last("pause_state").paused, srcSets: a.srcSets});
""", first=videos(["a.mp4", "b.mp4", "c.mp4"]), pruned=videos(["a.mp4", "c.mp4"])))
    assert out == [{"paused": True, "reported": True, "srcSets": 1}]


def test_a_new_list_led_by_another_video_starts_it(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(first);
await loadVideo(vid("a.mp4"));
await setState(next);
const b = vid("b.mp4");
report({b: !!b, active: stages[activeStageIndex].video === b, playing: !b.paused});
""", first=videos(["a.mp4", "b.mp4"]), next=videos(["b.mp4", "c.mp4"])))
    assert out == [{"b": True, "active": True, "playing": True}]


def test_a_new_picture_list_led_by_the_picture_on_screen_keeps_its_time(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(first);
const p1 = img("p1.jpg");
await loadImage(p1, true);
await advance(4000);
await setState(pruned);
const kept = {active: stages[activeStageIndex].image === p1, index: activeIndex, others: !!img("p3.jpg")};
await advance(5999);
const before = !!img("p3.jpg");
await advance(2);
report({kept, before, after: !!img("p3.jpg"), index: activeIndex});
""", first=slideshow(["p1.jpg", "p2.jpg", "p3.jpg"]), pruned=slideshow(["p1.jpg", "p3.jpg"])))
    assert out == [{"kept": {"active": True, "index": 0, "others": False},
                    "before": False, "after": True, "index": 1}]


def test_a_clock_synced_row_is_not_carried_over(tmp_path):
    synced = {**slideshow(["p1.jpg", "p2.jpg"]), "sync": {"mode": "clock", "length": 2}}
    out = run_page(tmp_path, scenario("""
await setState(first);
await loadImage(img("p1.jpg"), true);
report(continueOnScreen(activeState, synced));
""", first=slideshow(["p1.jpg", "p2.jpg"]), synced=synced))
    assert out == [False]
