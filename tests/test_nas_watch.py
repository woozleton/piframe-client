"""The NAS watch: does the share answer? A timed read off the main flow, never more than one in flight, and the page
told through `nas_ok` in its state file. No real share and no real waiting: the read, the clock and the threads are
the test's own."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

# Make the repo root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import piframe_client  # noqa: E402
from piframe_client import NasWatch  # noqa: E402


class Rig:
    """A NasWatch whose read threads and timeout timers wait for the test to run them."""

    def __init__(self, read, root="/mnt/nas"):
        self.now = 1000.0
        self.threads = []
        self.timers = []
        self.changes = []
        self.reads = 0

        def counted(root):
            self.reads += 1
            return read(root)

        self.watch = NasWatch(
            root, read=counted, clock=lambda: self.now, interval=15.0, timeout=3.0,
            on_change=self.changes.append,
            start_thread=self.threads.append,
            start_timer=lambda seconds, fn: self.timers.append((seconds, fn)),
        )

    def run_threads(self):
        threads = list(self.threads)
        self.threads.clear()
        for fn in threads:
            fn()

    def run_timers(self):
        timers = list(self.timers)
        self.timers.clear()
        for _seconds, fn in timers:
            fn()


def _answers(root):
    return None


def _raises(root):
    raise OSError("Host is down")


def test_nothing_known_before_the_first_read_answers():
    rig = Rig(_answers)
    assert rig.watch.ok is None
    assert rig.watch.poll() is None  # the read is started, not waited for
    assert len(rig.threads) == 1 and rig.timers[0][0] == 3.0


def test_a_read_that_answers_is_true():
    rig = Rig(_answers)
    rig.watch.poll()
    rig.now += 0.2
    rig.run_threads()
    assert rig.watch.ok is True and rig.changes == [True]
    rig.run_timers()  # its timeout timer finds nothing in flight
    assert rig.watch.ok is True and rig.changes == [True]


def test_a_read_that_raises_is_false():
    rig = Rig(_raises)
    rig.watch.poll()
    rig.run_threads()
    assert rig.watch.ok is False and rig.changes == [False]


def test_an_empty_folder_that_reads_fine_is_true(tmp_path, monkeypatch):
    empty = tmp_path / "nas"
    empty.mkdir()
    monkeypatch.setattr(piframe_client.os.path, "ismount", lambda path: True)
    rig = Rig(piframe_client._read_nas_folder, root=str(empty))
    rig.watch.poll()
    rig.run_threads()
    assert rig.reads == 1 and rig.watch.ok is True


def test_a_hung_read_is_false_after_the_timeout_and_no_second_read_starts():
    rig = Rig(_answers)
    rig.watch.poll()
    hung = rig.threads.pop()  # started, never returns (yet)
    rig.now += 2.9
    assert rig.watch.poll() is None and rig.threads == []
    rig.now += 0.1
    rig.run_timers()  # the timeout
    assert rig.watch.ok is False and rig.changes == [False]
    for _ in range(10):  # it hangs on through many due checks
        rig.now += 15.0
        assert rig.watch.poll() is False
    assert rig.threads == [] and rig.timers == [] and rig.reads == 0
    # It finally returns: that frees the slot but decides nothing ...
    hung()
    assert rig.reads == 1 and rig.watch.ok is False
    # ... and the next check starts a fresh read, which answers.
    rig.watch.poll()
    assert len(rig.threads) == 1
    rig.run_threads()
    assert rig.watch.ok is True and rig.changes == [False, True]


def test_the_poll_itself_times_a_read_out_without_its_timer():
    rig = Rig(_answers)
    rig.watch.poll()
    rig.threads.clear()
    rig.timers.clear()
    rig.now += 3.0
    assert rig.watch.poll() is False


def test_a_hung_read_that_fails_at_last_lets_the_next_check_start_fresh():
    rig = Rig(_raises)
    rig.watch.poll()
    hung = rig.threads.pop()
    rig.now += 3.0
    rig.run_timers()
    hung()  # Host is down, after a long wait
    rig.now += 60.0
    rig.watch.poll()
    assert len(rig.threads) == 1


def test_reads_are_at_most_one_an_interval():
    rig = Rig(_answers)
    rig.watch.poll()
    rig.run_threads()
    for _ in range(7):  # the status loop's ticks, 2 s apart
        rig.now += 2.0
        rig.watch.poll()
    assert rig.threads == [] and rig.reads == 1
    rig.now += 1.0  # 15 s after the first read started
    rig.watch.poll()
    assert len(rig.threads) == 1


def test_only_a_change_is_told():
    rig = Rig(_answers)
    for _ in range(3):
        rig.watch.poll()
        rig.run_threads()
        rig.now += 15.0
    assert rig.reads == 3 and rig.changes == [True]


def test_real_threads_a_hung_read_times_out_and_answers_later():
    """The daemon thread and timer for real, with a 10 ms timeout."""
    release = threading.Event()
    changed = threading.Event()
    seen = []

    def hangs(root):
        release.wait(5)

    def on_change(value):
        seen.append(value)
        changed.set()

    watch = NasWatch("/mnt/nas", read=hangs, timeout=0.01, interval=0.0, on_change=on_change)
    watch.poll()
    assert changed.wait(2) and seen == [False]
    assert watch.poll() is False  # still in flight: no second read
    changed.clear()
    release.set()
    for _ in range(200):  # the hung read returns; then a fresh read answers
        if changed.wait(0.01):
            break
        watch.poll()
    assert seen == [False, True]


def test_the_page_is_told_only_when_the_answer_changes(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(piframe_client, "BROWSER_STATE_FILE", state_file)
    renderer = SimpleNamespace(
        _state_lock=threading.Lock(),
        _state={"mode": "playlist", "items": ["a"], "banner": None, "control": None, "nas_ok": None},
    )
    writes = []

    def write_state():
        writes.append(dict(renderer._state))
        piframe_client.BrowserController._write_state(renderer)

    renderer._write_state = write_state
    set_nas_ok = piframe_client.BrowserController.set_nas_ok
    set_nas_ok(renderer, False)
    set_nas_ok(renderer, False)
    set_nas_ok(renderer, True)
    assert [w["nas_ok"] for w in writes] == [False, True]
    on_disk = json.loads(state_file.read_text())
    assert on_disk == {"mode": "playlist", "items": ["a"], "banner": None, "control": None, "nas_ok": True}


def test_a_content_change_keeps_the_latest_answer(monkeypatch, tmp_path):
    """_set_items_state rewrites the content keys only: nas_ok rides through."""
    monkeypatch.setattr(piframe_client, "BROWSER_STATE_FILE", tmp_path / "state.json")
    renderer = piframe_client.BrowserController.__new__(piframe_client.BrowserController)
    renderer._state_lock = threading.Lock()
    renderer._state = {"nas_ok": False, "banner": None}
    renderer._last_volume = 50.0
    renderer._last_mute = False
    renderer._ensure_running = lambda: True
    renderer._write_state = lambda: None
    renderer._classify_item_issue = lambda path: None
    renderer._set_items_state(
        mode="playlist", playlist_name="P", items=[str(tmp_path / "a.jpg")], interval=5.0,
        repeat=True, loop=False, shuffle=False, idle_media="",
    )
    assert renderer._state["nas_ok"] is False and renderer._state["mode"] == "playlist"


def test_the_default_read_wants_the_share_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(piframe_client.os.path, "ismount", lambda path: False)
    try:
        piframe_client._read_nas_folder(str(tmp_path))
    except OSError:
        pass
    else:
        raise AssertionError("an unmounted folder must not count as answering")
    monkeypatch.setattr(piframe_client.os.path, "ismount", lambda path: True)
    piframe_client._read_nas_folder(str(tmp_path))  # mounted and empty: answers
