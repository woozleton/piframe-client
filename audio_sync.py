"""Whole-house synced audio: shared timeline math (Pi mirror).

This is a VERBATIM mirror of the pure-function section of woozlescape
core/audio_sync.py (the constants block and every function from
``cycle_length`` through ``clamp_latency_ms``) - the registry half
(sessions, persistence, HTTP-facing helpers) lives only on the server
and has no reason to exist here. Every player of a companion queue
(this Pi's sidecar mpv, and the Virtual Window's audio-companion mpv
via ``vwm/audio_sync_slave.py``) computes its position on the same
shared timeline from these functions alone, so a constant or formula
changed on one side and not the other silently desyncs the house.

woozlescape's ``tests/test_audio_sync.py`` imports THIS file
(``piframe-client/audio_sync.py``) and asserts the constants and math
match ``core/audio_sync.py`` byte-for-byte in behavior - change both
files together, never just one.

Full model: woozlescape docs/subsystems/audio-sync.md.
"""

from __future__ import annotations

# --- Timeline + discipline constants (FROZEN - mirrored from the server) ---

LEAD_S = 2.0               # a fresh session's epoch sits this far in the future
EXPIRE_S = 60.0            # an empty session lingers this long before it's dropped
DEADBAND_S = 0.012         # |err| <= this -> rate 1.0
NUDGE_MAX_ERR_S = 0.25     # DEADBAND < |err| <= this -> speed nudge
NUDGE_GAIN = 0.5           # rate = 1 + clamp(GAIN * err, -CLAMP, +CLAMP)
NUDGE_CLAMP = 0.005        # max +/-0.5% (~9 cents) - inaudible on ambience
RESYNC_COOLDOWN_S = 2.0    # no second resync this soon after one (mpv settling)
BOUNDARY_GUARD_S = 0.35    # skip corrections this close to a track boundary
CADENCE_S = 0.5            # discipline re-check period
ARM_LEAD_S = 0.4           # resync: seek this far ahead while paused, then release
ERR_WINDOW = 3             # decisions use the median of the last N errors

CLOCK_PING_S = 1.0         # Pi time_ping period while a synced session plays
CLOCK_IDLE_PING_S = 10.0   # ... and otherwise (keeps the estimate warm)
CLOCK_WINDOW = 16          # keep the most recent N (offset, rtt) samples
CLOCK_BEST = 4             # offset = median of the N lowest-RTT samples

LATENCY_MIN_MS = -1000     # per-surface audio delay clamp (int ms)
LATENCY_MAX_MS = 1000


# =========================================================================
# Pure functions - no I/O, no time.time(); clock / inputs are arguments.
# Mirrored verbatim from woozlescape core/audio_sync.py.
# =========================================================================


def cycle_length(durations) -> float:
    """Seconds in one full pass of the queue (0.0 for an empty / bad list)."""
    total = 0.0
    for d in durations or ():
        total += float(d)
    return total


def track_starts(durations) -> list[float]:
    """Cumulative start offset of each track within the cycle."""
    starts: list[float] = []
    acc = 0.0
    for d in durations or ():
        starts.append(acc)
        acc += float(d)
    return starts


def cycle_pos(epoch: float, durations, t: float) -> float:
    """Seconds into the looping cycle at server time ``t``. Negative while
    ``t`` is before the epoch (the session hasn't started yet): the value is
    then ``t - epoch``, i.e. minus the seconds until track 0 starts."""
    total = cycle_length(durations)
    if total <= 0:
        return 0.0
    raw = t - epoch
    if raw < 0:
        return raw
    return raw % total


def locate(durations, pos: float) -> tuple[int, float]:
    """Map a cycle position to ``(track_index, offset_into_track)``. A
    negative ``pos`` (pre-roll) maps to ``(0, pos)`` unchanged."""
    if pos < 0 or not durations:
        return (0, pos if pos < 0 else 0.0)
    starts = track_starts(durations)
    idx = 0
    for i, s in enumerate(starts):
        if pos >= s:
            idx = i
        else:
            break
    return (idx, pos - starts[idx])


def position_at(epoch: float, durations, t: float) -> tuple[int, float]:
    """``(track_index, offset_into_track)`` the timeline is at, at server
    time ``t``. Before the epoch this is ``(0, negative seconds-to-start)``."""
    return locate(durations, cycle_pos(epoch, durations, t))


def player_cycle_pos(durations, index: int, offset: float) -> float:
    """A player's ``(playlist index, playhead)`` as a cycle position - the
    inverse of ``locate`` - so errors compare in one coordinate even when a
    player crossed a track boundary a few ms early or late."""
    starts = track_starts(durations)
    if not starts:
        return 0.0
    i = max(0, min(int(index), len(starts) - 1))
    return starts[i] + float(offset)


def signed_error(target: float, actual: float, cycle: float) -> float:
    """Signed, seam-safe error between the timeline ``target`` and the
    player's ``actual`` cycle position: ``((target - actual + C/2) mod C) -
    C/2``. Positive = player is BEHIND (speed up); negative = ahead.
    Across the loop seam (actual 59.9, target 0.1, C=60) it's +0.2, not -59.8.
    """
    if cycle <= 0:
        return 0.0
    half = cycle / 2.0
    return ((target - actual + half) % cycle) - half


def near_boundary(durations, pos: float, guard: float = BOUNDARY_GUARD_S) -> bool:
    """True when cycle position ``pos`` is within ``guard`` seconds of any
    track start (the cycle seam included), where mpv is between files and
    its playhead/index can't be trusted. Also True for a pre-roll ``pos``."""
    total = cycle_length(durations)
    if total <= 0 or pos < 0:
        return True
    for s in track_starts(durations):
        d = abs(pos - s) % total
        if min(d, total - d) < guard:
            return True
    return False


def discipline_action(err_s: float, since_resync_s: float) -> tuple[str, float]:
    """Map a (median-filtered) signed error to ``(action, rate)``:

    - ``("none", 1.0)``   inside the deadband; hold / reassert rate 1.0.
    - ``("nudge", rate)``  ``1 + clamp(GAIN * err)``: behind speeds up.
    - ``("resync", 1.0)``  gross error: pause, seek ``ARM_LEAD_S`` ahead of
      the timeline, release on the mark.
    - ``("hold", 1.0)``   gross error inside ``RESYNC_COOLDOWN_S`` of the
      last resync - mpv's playhead is still settling; do nothing.
    """
    mag = abs(err_s)
    if mag <= DEADBAND_S:
        return ("none", 1.0)
    if mag <= NUDGE_MAX_ERR_S:
        nudge = NUDGE_GAIN * err_s
        if nudge > NUDGE_CLAMP:
            nudge = NUDGE_CLAMP
        elif nudge < -NUDGE_CLAMP:
            nudge = -NUDGE_CLAMP
        return ("nudge", 1.0 + nudge)
    if since_resync_s < RESYNC_COOLDOWN_S:
        return ("hold", 1.0)
    return ("resync", 1.0)


def median(values) -> float | None:
    """Median of a sequence (None when empty)."""
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def clock_sample(t0: float, server_t: float, t3: float) -> tuple[float, float]:
    """One ping/pong round: client sent at ``t0``, server stamped
    ``server_t``, reply arrived at ``t3`` (client clock). Returns
    ``(offset, rtt)`` with ``server_now ~= client_now + offset``."""
    return (server_t - (t0 + t3) / 2.0, t3 - t0)


def clock_offset(samples) -> tuple[float, float] | None:
    """Estimate ``(offset, rtt)`` from recent ``(offset, rtt)`` samples: the
    median offset of the ``CLOCK_BEST`` lowest-RTT ones (a slow round trip
    is an asymmetric one, so it's the least trustworthy). None when empty."""
    pairs = [(float(o), float(r)) for o, r in samples or ()]
    if not pairs:
        return None
    best = sorted(pairs, key=lambda p: p[1])[:CLOCK_BEST]
    return (median(o for o, _ in best), median(r for _, r in best))


def clamp_latency_ms(value) -> int | None:
    """Coerce an operator-entered delay to a clamped int (None if invalid)."""
    try:
        ms = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(LATENCY_MIN_MS, min(LATENCY_MAX_MS, ms))
