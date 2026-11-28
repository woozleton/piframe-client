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


# ---------------------------------------------------------------- A row of a new length
#
# The harness clock starts on a whole multiple of 10 s: at time t the slot
# is floor(1.7e12 / 1e4 + t / 1e4) % length, so 170000000 + k, which is
# k % 4 for a row of 4 and (2 + k) % 3 for a row of 3.

def synced(names, *, interval=10.0, name=None) -> dict:
    row = {**slideshow(names, interval=interval), "sync": {"mode": "clock", "length": len(names)}}
    if name is not None:
        row["playlist_name"] = name
    return row


def test_a_shorter_synced_row_keeping_the_picture_paints_nothing(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
const t0 = renderToken;
await advance(3000);
await setState(row3);
const swapped = {
  renders: renderToken - t0, index: activeIndex, shown: lastPaintedSrc,
  count: activeState.items.length, reported: last("slideshow_index").index,
};
await advance(6999);
const before = renderToken - t0;
await advance(1);
report({swapped, before, after: renderToken - t0, index: activeIndex, next: !!img("c.jpg")});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]), row3=synced(["c.jpg", "d.jpg", "a.jpg"])))
    assert out == [{
        "swapped": {"renders": 0, "index": 2, "shown": "file:///media/a.jpg", "count": 3, "reported": 2},
        "before": 0, "after": 1, "index": 0, "next": True,
    }]


def test_a_shorter_synced_row_with_another_picture_cross_fades_once(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
const t0 = renderToken;
await advance(4000);
await setState(row3);
const swapped = {renders: renderToken - t0, index: activeIndex, d: !!img("d.jpg")};
await loadImage(img("d.jpg"), true);
const shown = stages[activeStageIndex].image === img("d.jpg");
await advance(5999);
const before = {renders: renderToken - t0, c: !!img("c.jpg")};
await advance(2);
report({swapped, shown, before, after: renderToken - t0, index: activeIndex, c: !!img("c.jpg")});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]), row3=synced(["c.jpg", "a.jpg", "d.jpg"])))
    assert out == [{
        "swapped": {"renders": 1, "index": 2, "d": True}, "shown": True,
        "before": {"renders": 1, "c": False}, "after": 2, "index": 0, "c": True,
    }]


def test_a_shorter_synced_row_while_paused_waits_for_resume(tmp_path):
    out = run_page(tmp_path, scenario("""
await advance(30000);
await setState(row4);              // slot 3: d.jpg
await loadImage(img("d.jpg"), true);
applyControl({action: "pause", token: 1});
const t0 = renderToken;
await setState(row3);
const paused = {renders: renderToken - t0, index: activeIndex, shown: lastPaintedSrc};
await advance(10000);
const still = renderToken - t0;
applyControl({action: "pause", token: 2});
await flush();
const resumed = {renders: renderToken - t0, index: activeIndex, c: !!img("c.jpg")};
await advance(9999);
const before = renderToken - t0;
await advance(1);
report({paused, still, resumed, before, after: renderToken - t0, index: activeIndex});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]), row3=synced(["c.jpg", "a.jpg", "d.jpg"])))
    assert out == [{
        "paused": {"renders": 0, "index": 0, "shown": "file:///media/d.jpg"},
        "still": 0,
        "resumed": {"renders": 1, "index": 0, "c": True},
        "before": 1, "after": 2, "index": 1,
    }]


def test_another_synced_list_of_a_new_length_starts_over_and_ends_a_pause(tmp_path):
    """Only the same playlist swaps in place (pruned, or under the same name
    still holding the picture on screen): other content of another length
    starts the sync afresh, as it always has - a row under the same name
    without the picture on screen, or a longer row under another name."""
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
applyControl({action: "pause", token: 1});
const t0 = renderToken;
await setState(other3);
const other = {renders: renderToken - t0, paused: syncPaused, index: activeIndex, count: activeState.items.length};
await loadImage(img("z.jpg"), true);
applyControl({action: "pause", token: 2});
const t1 = renderToken;
await setState(grown5);
report({other, grown: {renders: renderToken - t1, paused: syncPaused, count: activeState.items.length}});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]), other3=synced(["x.jpg", "y.jpg", "z.jpg"]),
        grown5=synced(["x.jpg", "y.jpg", "z.jpg", "v.jpg", "w.jpg"], name="Another Playlist")))
    assert out == [{
        "other": {"renders": 1, "paused": False, "index": 2, "count": 3},
        "grown": {"renders": 1, "paused": False, "count": 5},
    }]


# A longer row for the same playlist (pictures added while it plays): the
# manager pins the picture on screen at the current clock slot. A row of 6 is
# at slot (2 + k) % 6, a row of 5 at k % 5.

def test_a_longer_synced_row_keeping_the_picture_paints_nothing(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
const t0 = renderToken;
await advance(3000);
await setState(row6);
const swapped = {
  renders: renderToken - t0, index: activeIndex, shown: lastPaintedSrc,
  count: activeState.items.length, reported: last("slideshow_index").index,
};
await advance(6999);
const before = renderToken - t0;
await advance(1);
report({swapped, before, after: renderToken - t0, index: activeIndex, next: !!img("b.jpg")});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]),
        row6=synced(["x.jpg", "y.jpg", "a.jpg", "b.jpg", "c.jpg", "d.jpg"])))
    assert out == [{
        "swapped": {"renders": 0, "index": 2, "shown": "file:///media/a.jpg", "count": 6, "reported": 2},
        "before": 0, "after": 1, "index": 3, "next": True,
    }]


def test_a_longer_synced_row_with_another_picture_cross_fades_once(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
const t0 = renderToken;
await advance(4000);
await setState(row6);
const swapped = {renders: renderToken - t0, index: activeIndex, y: !!img("y.jpg")};
await loadImage(img("y.jpg"), true);
const shown = stages[activeStageIndex].image === img("y.jpg");
await advance(5999);
const before = {renders: renderToken - t0, b: !!img("b.jpg")};
await advance(2);
report({swapped, shown, before, after: renderToken - t0, index: activeIndex, b: !!img("b.jpg")});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]),
        row6=synced(["x.jpg", "a.jpg", "y.jpg", "b.jpg", "c.jpg", "d.jpg"])))
    assert out == [{
        "swapped": {"renders": 1, "index": 2, "y": True}, "shown": True,
        "before": {"renders": 1, "b": False}, "after": 2, "index": 3, "b": True,
    }]


def test_a_longer_synced_row_while_paused_keeps_the_pause_and_resumes_on_the_clock(tmp_path):
    out = run_page(tmp_path, scenario("""
await advance(30000);
await setState(row4);              // slot 3: d.jpg
await loadImage(img("d.jpg"), true);
applyControl({action: "pause", token: 1});
const t0 = renderToken;
await setState(row6);
const paused = {
  renders: renderToken - t0, paused: syncPaused, index: activeIndex, shown: lastPaintedSrc,
  reported: last("pause_state").paused, count: activeState.items.length,
};
await advance(10000);
const still = renderToken - t0;
applyControl({action: "pause", token: 2});
await flush();
const resumed = {renders: renderToken - t0, paused: syncPaused, index: activeIndex, x: !!img("x.jpg")};
await advance(9999);
const before = renderToken - t0;
await advance(1);
report({paused, still, resumed, before, after: renderToken - t0, index: activeIndex});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]),
        row6=synced(["x.jpg", "y.jpg", "a.jpg", "d.jpg", "b.jpg", "c.jpg"])))
    assert out == [{
        "paused": {"renders": 0, "paused": True, "index": 3, "shown": "file:///media/d.jpg",
                   "reported": True, "count": 6},
        "still": 0,
        "resumed": {"renders": 1, "paused": False, "index": 0, "x": True},
        "before": 1, "after": 2, "index": 1,
    }]


def test_pictures_added_and_removed_in_one_burst_keep_the_picture_on_screen(tmp_path):
    """b.jpg leaves and x.jpg, y.jpg join in one re-send; later a.jpg, y.jpg and
    d.jpg leave and w.jpg joins (a shorter row that is not a pruned one). Each
    still holds the picture on screen, so neither repaints."""
    out = run_page(tmp_path, scenario("""
await setState(row4);
await loadImage(img("a.jpg"), true);
const t0 = renderToken;
await advance(3000);
await setState(burst5);
const grown = {renders: renderToken - t0, index: activeIndex, shown: lastPaintedSrc, count: activeState.items.length};
await advance(7000);               // slot 1 of 5: x.jpg
await loadImage(img("x.jpg"), true);
const flipped = {renders: renderToken - t0, index: activeIndex};
await advance(3000);
await setState(burst3);
const shrunk = {renders: renderToken - t0, index: activeIndex, shown: lastPaintedSrc, count: activeState.items.length};
report({grown, flipped, shrunk});
""", row4=synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"]),
        burst5=synced(["a.jpg", "x.jpg", "c.jpg", "y.jpg", "d.jpg"]),
        burst3=synced(["x.jpg", "c.jpg", "w.jpg"])))
    assert out == [{
        "grown": {"renders": 0, "index": 0, "shown": "file:///media/a.jpg", "count": 5},
        "flipped": {"renders": 1, "index": 1},
        "shrunk": {"renders": 1, "index": 0, "shown": "file:///media/x.jpg", "count": 3},
    }]


# ---------------------------------------------------------------- NAS unavailable, only when it is

def nas_item(kind: str, name: str) -> dict:
    return {"kind": kind, "src": f"file:///mnt/nas/pictures/{name}", "label": name, "is_audio": False}


NAS_PRELUDE = """
const nasImg = (name) => stages.map((s) => s.image).find((i) => i.src === "file:///mnt/nas/pictures/" + name);
"""


def nas_slideshow(names, **extra) -> dict:
    return {**slideshow(names), "items": [nas_item("image", n) for n in names], **extra}


def test_a_failed_nas_picture_names_the_share_only_when_it_is_not_answering(tmp_path):
    seen = {}
    for label, extra in (("down", {"nas_ok": False}), ("up", {"nas_ok": True}),
                         ("unknown", {"nas_ok": None}), ("missing", {})):
        out = run_page(tmp_path, scenario(NAS_PRELUDE + """
await setState(s);
await loadImage(nasImg("gone.jpg"), false);
report(banner());
""", s=nas_slideshow(["gone.jpg", "good.jpg"], **extra)))
        seen[label] = out[0]
    assert seen == {"down": "NAS unavailable", "up": "Media file missing",
                    "unknown": "Media file missing", "missing": "Media file missing"}


def test_the_latest_nas_ok_is_the_one_read(tmp_path):
    out = run_page(tmp_path, scenario(NAS_PRELUDE + """
await setState(up);
await setState(down);   // only nas_ok differs: nothing repaints, the answer is remembered
await loadImage(nasImg("gone.jpg"), false);
const first = banner();
await setState(up);
report({first, message: itemErrorMessage(activeState.items[0])});
""", up=nas_slideshow(["gone.jpg", "good.jpg"], nas_ok=True),
        down=nas_slideshow(["gone.jpg", "good.jpg"], nas_ok=False)))
    assert out == [{"first": "NAS unavailable", "message": "Media file missing"}]


def test_a_file_outside_the_nas_folder_is_missing_whatever_the_share_says(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(s);
await loadImage(img("gone.jpg"), false);
report(banner());
""", s={**slideshow(["gone.jpg", "good.jpg"]), "nas_ok": False}))
    assert out == ["Media file missing"]


def test_the_nas_banner_goes_when_the_next_picture_shows(tmp_path):
    out = run_page(tmp_path, scenario(NAS_PRELUDE + """
await setState(s);
await loadImage(nasImg("gone.jpg"), false);
const during = banner();
await advance(10000);
await loadImage(nasImg("good.jpg"), true);
report({during, after: banner(), index: activeIndex});
""", s=nas_slideshow(["gone.jpg", "good.jpg", "good2.jpg"], nas_ok=False)))
    assert out == [{"during": "NAS unavailable", "after": "", "index": 1}]


def test_a_nas_ok_change_leaves_a_synced_slideshow_alone(tmp_path):
    row = synced(["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
    out = run_page(tmp_path, scenario("""
await setState(row);
const a = img("a.jpg");
await loadImage(a, true);
await advance(3000);
const t0 = renderToken, idx = activeIndex, sets = a.srcSets;
for (const ok of [false, true, null, false]) {
  await setState({...row, nas_ok: ok});
}
const kept = {renders: renderToken - t0, index: activeIndex - idx, srcSets: a.srcSets - sets,
              shown: lastPaintedSrc, active: stages[activeStageIndex].image === a};
await advance(6999);
const before = renderToken - t0;
await advance(1);
report({kept, before, after: renderToken - t0, index: activeIndex});
""", row=row))
    assert out == [{
        "kept": {"renders": 0, "index": 0, "srcSets": 0, "shown": "file:///media/a.jpg", "active": True},
        "before": 0, "after": 1, "index": 1,
    }]


def test_a_nas_ok_change_leaves_a_playing_video_alone(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(v);
const a = vid("a.mp4");
await loadVideo(a);
a.currentTime = 42;
const t0 = renderToken, sets = a.srcSets, loads = a.loadCalls;
await setState({...v, nas_ok: false});
await setState({...v, nas_ok: true});
report({same: vid("a.mp4") === a, active: stages[activeStageIndex].video === a, renders: renderToken - t0,
        srcSets: a.srcSets - sets, loads: a.loadCalls - loads, time: a.currentTime, paused: a.paused,
        index: activeIndex});
""", v=videos(["a.mp4", "b.mp4"])))
    assert out == [{"same": True, "active": True, "renders": 0, "srcSets": 0, "loads": 0,
                    "time": 42, "paused": False, "index": 0}]


def test_a_nas_ok_change_leaves_a_mural_video_alone(tmp_path):
    mural = {**videos(["m.mp4"]), "sync": {"mode": "clock_video", "epoch": 1699999990, "duration": 30,
                                           "latency_ms": 0}}
    out = run_page(tmp_path, scenario("""
await setState(mural);
const m = vid("m.mp4");
await loadVideo(m);
const t0 = renderToken, sets = m.srcSets, handle = videoSyncHandle;
await setState({...mural, nas_ok: false});
report({same: vid("m.mp4") === m, renders: renderToken - t0, srcSets: m.srcSets - sets,
        handle: videoSyncHandle === handle && handle !== null});
""", mural=mural))
    assert out == [{"same": True, "renders": 0, "srcSets": 0, "handle": True}]


def test_a_missing_banner_is_renamed_when_the_share_stops_answering(tmp_path):
    """A file can fail a few seconds before the watch notices the share is
    down: the banner then says what it was, and nothing repaints."""
    out = run_page(tmp_path, scenario(NAS_PRELUDE + """
await setState(up);
await loadImage(nasImg("gone.jpg"), false);
const first = banner();
const t0 = renderToken;
await setState(down);
report({first, after: banner(), renders: renderToken - t0});
""", up=nas_slideshow(["gone.jpg", "good.jpg"], nas_ok=True),
        down=nas_slideshow(["gone.jpg", "good.jpg"], nas_ok=False)))
    assert out == [{"first": "Media file missing", "after": "NAS unavailable", "renders": 0}]


def test_a_local_files_missing_banner_keeps_its_name_when_the_share_goes(tmp_path):
    out = run_page(tmp_path, scenario("""
await setState(up);
await loadImage(img("gone.jpg"), false);
const first = banner();
await setState(down);
report({first, after: banner()});
""", up={**slideshow(["gone.jpg", "good.jpg"]), "nas_ok": True},
        down={**slideshow(["gone.jpg", "good.jpg"]), "nas_ok": False}))
    assert out == [{"first": "Media file missing", "after": "Media file missing"}]


# ---------------------------------------------------------------- A guest's own end

# The harness answers every read with the one state, so the cast rides in it
# too: the page reads it as both its state file and its sprite file.
SPRITES = """
const base = 1700000000;
const ent = (id, until) => Object.assign({id, sprite: {kind: "circle", size: 100, color: "#ffc03c"},
  motion: {mode: "pinned", x_frac: 0.5, y_frac: 0.5}}, until === undefined ? {} : {until});
const cast = (seq, entities, latency_ms) => ({mode: "idle", items: [], idle_item: null, banner: null,
  sprite_seq: seq, world: {w: 1920, h: 1080}, window: {x: 0, y: 0, w: 1920, h: 1080}, latency_ms, entities});
const shown = (id) => { const s = spriteSlotById.get(id); return !!s && s.visible === true; };
"""


def test_a_guest_goes_at_its_until_and_latency_does_not_move_it(tmp_path):
    out = run_page(tmp_path, scenario(SPRITES + """
await setState(cast(1, [ent("fish"), ent("guest", base + 20)], 60000));
spriteFrame();
const before = {fish: shown("fish"), guest: shown("guest")};
const box = spriteSlotById.get("guest").el;
await advance(20000);
spriteFrame();
const look = JSON.stringify(box.style);
spriteFrame();
report({before, fish: shown("fish"), guest: spriteSlotById.has("guest"), hidden: box.style.visibility,
        free: spriteFreeSlots.length, untouched: JSON.stringify(box.style) === look, running: spriteRunning()});
"""))
    assert out == [{"before": {"fish": True, "guest": True}, "fish": True, "guest": False, "hidden": "hidden",
                    "free": 1, "untouched": True, "running": True}]


def test_a_cast_that_has_all_ended_is_no_cast(tmp_path):
    out = run_page(tmp_path, scenario(SPRITES + """
await setState(cast(1, [ent("guest", base + 5)], 0));
spriteFrame();
const before = {guest: shown("guest"), running: spriteRunning()};
await advance(5000);
report({before, running: spriteRunning(), held: spriteSlotById.size,
        hidden: spriteSlots.every((s) => s.el.style.visibility === "hidden")});
"""))
    assert out == [{"before": {"guest": True, "running": True}, "running": False, "held": 0, "hidden": True}]


def test_a_reloaded_page_never_draws_an_ended_guest(tmp_path):
    # The cast still on disk after a restart: the ended guest gets no box at all.
    out = run_page(tmp_path, scenario(SPRITES + """
await setState(cast(7, [ent("guest", base - 1), ent("fish")], 0));
spriteFrame();
const mixed = {boxes: spriteSlots.length, fish: shown("fish"), guest: spriteSlotById.has("guest")};
report(mixed);
"""))
    assert out == [{"boxes": 1, "fish": True, "guest": False}]


def test_a_reloaded_page_with_only_ended_guests_starts_no_layer(tmp_path):
    out = run_page(tmp_path, scenario(SPRITES + """
await setState(cast(7, [ent("guest", base - 1), ent("other", base)], 0));
report({running: spriteRunning(), boxes: spriteSlots.length});
"""))
    assert out == [{"running": False, "boxes": 0}]


# ---------------------------------------------------------------- A guest coming in and going out

# A pinned circle, 100 world units on a 1920 x 1080 window of the same world, so a world unit is a pixel: its
# place's top is 540 - 50 = 490.
FX = SPRITES + r"""
const fxEnt = (id, fx) => Object.assign(ent(id), fx === undefined ? {} : {fx});
const look = (id) => { const s = spriteSlotById.get(id); if (!s) return null;
  const m = /translate3d\(([-\d.]+)px,([-\d.]+)px/.exec(s.el.style.transform || "");
  return {y: m ? +m[2] : null, opacity: s.el.style.opacity === undefined ? "unset" : s.el.style.opacity,
          shown: s.visible === true}; };
"""


def test_a_fade_comes_in_over_s_on_the_page_clock(tmp_path):
    out = run_page(tmp_path, scenario(FX + """
await setState(cast(1, [fxEnt("card", {in: "fade", s: 0.6})], 0));
const seen = [];
spriteFrame(); seen.push(look("card"));
await advance(300); spriteFrame(); seen.push(look("card"));
await advance(300); spriteFrame(); seen.push(look("card"));
await advance(300); spriteFrame(); seen.push(look("card"));
report(seen);
"""))
    assert out == [[{"y": 490, "opacity": "0", "shown": True}, {"y": 490, "opacity": "0.5", "shown": True},
                    {"y": 490, "opacity": "", "shown": True}, {"y": 490, "opacity": "", "shown": True}]]


def test_a_slide_comes_up_from_dy_below_easing_out_and_the_photo_moves_with_it(tmp_path):
    out = run_page(tmp_path, scenario(FX + """
const fx = {in: "slide", out: "slide", s: 0.6, dy: 400, out_at: base + 60};
await setState(cast(1, [fxEnt("card", fx), fxEnt("photo", fx)], 0));
const seen = [];
for (const ms of [0, 300, 300]) { await advance(ms); spriteFrame(); seen.push([look("card"), look("photo")]); }
report(seen);
"""))
    # 400 * (1 - 0.5) ** 3 = 50 down at half way, and both drawn the same.
    assert out == [[[{"y": 890, "opacity": "0", "shown": True}] * 2,
                    [{"y": 540, "opacity": "0.5", "shown": True}] * 2,
                    [{"y": 490, "opacity": "", "shown": True}] * 2]]


def test_a_resend_keeps_the_way_in_where_it_was_and_a_new_guest_starts_its_own(tmp_path):
    out = run_page(tmp_path, scenario(FX + """
const fx = {in: "fade", s: 0.6};
await setState(cast(1, [fxEnt("card", fx)], 0));
spriteFrame();
await advance(300);
await setState(cast(2, [ent("fish"), fxEnt("card", fx), fxEnt("late", fx)], 0));
spriteFrame();
const mid = {card: look("card"), late: look("late")};
await advance(300); spriteFrame();
report({mid, card: look("card"), late: look("late")});
"""))
    assert out == [{"mid": {"card": {"y": 490, "opacity": "0.5", "shown": True},
                            "late": {"y": 490, "opacity": "0", "shown": True}},
                    "card": {"y": 490, "opacity": "", "shown": True},
                    "late": {"y": 490, "opacity": "0.5", "shown": True}}]


def test_the_way_out_ends_at_out_at_on_the_until_clock_and_it_stays_hidden(tmp_path):
    out = run_page(tmp_path, scenario(FX + """
const fx = {in: "slide", out: "slide", s: 0.6, dy: 400, out_at: base + 10};
await setState(cast(1, [fxEnt("card", fx)], 60000));
spriteFrame();
const seen = [];
for (const ms of [9400, 300, 300, 1000]) { await advance(ms); spriteFrame(); seen.push(look("card")); }
const box = spriteSlotById.get("card").el;
report({seen, hidden: box.style.visibility, held: spriteSlotById.has("card")});
"""))
    # Its place is untouched by latency_ms (pinned); the way out reads the plain clock: 400 * 0.5 ** 3 = 50 down.
    assert out == [{"seen": [{"y": 490, "opacity": "", "shown": True}, {"y": 540, "opacity": "0.5", "shown": True},
                             {"y": 540, "opacity": "0.5", "shown": False},
                             {"y": 540, "opacity": "0.5", "shown": False}],
                    "hidden": "hidden", "held": True}]


@pytest.mark.parametrize("fx", ["none", {"in": "fade", "s": 9}, {"in": "wipe", "s": 0.6}, {"s": 0.6}, "fade"])
def test_no_or_a_broken_fx_draws_it_exactly_as_before(tmp_path, fx):
    out = run_page(tmp_path, scenario(FX + f"""
const fx = {json.dumps(fx)};
await setState(cast(1, [fxEnt("card", fx === "none" ? undefined : fx)], 0));
spriteFrame();
report(look("card"));
"""))
    assert out == [{"y": 490, "opacity": "unset", "shown": True}]


def test_no_s_is_0_6_and_a_way_out_overlapping_the_way_in_adds_its_offset(tmp_path):
    # out_at 0.3 s after it first draws: at 0.15 s p = 0.25 (no s: 0.6) and q = (0.15 - (0.3 - 0.6)) / 0.6 = 0.75.
    out = run_page(tmp_path, scenario(FX + """
await setState(cast(1, [fxEnt("card", {in: "slide", out: "slide", dy: 400, out_at: base + 0.3})], 0));
spriteFrame();
await advance(150); spriteFrame();
report(look("card"));
"""))
    # Opacity min(0.25, 0.25); down 400 * (0.75 ** 3 + 0.75 ** 3) = 337.5, from 490.
    assert out == [{"y": 827.5, "opacity": "0.25", "shown": True}]


@pytest.mark.parametrize("fx", [{"in": "fade", "out": "fade", "s": 0.6},
                                {"in": "fade", "s": 0.6, "out_at": 1700000001}])
def test_out_without_out_at_or_out_at_without_out_never_hides_it(tmp_path, fx):
    out = run_page(tmp_path, scenario(FX + f"""
await setState(cast(1, [fxEnt("card", {json.dumps(fx)})], 0));
spriteFrame();
await advance(5000); spriteFrame();
report(look("card"));
"""))
    assert out == [{"y": 490, "opacity": "", "shown": True}]
