# -*- coding: utf-8 -*-
"""Dynamic cold-start segmentation for GNSS position streams.

The previous implementation used fixed product rules: BY week rollback and
HUACE time gaps.  Those rules are not valid for all recordings.  This module
uses a state-transition and coverage rule that adapts to arbitrary recording
durations:

* candidate start = first frame after a discontinuity or state loss where the
  receiver leaves a differential/fixed or already-running state and enters a
  no-time/approximate/no-solution/single-point starting state;
* a segment must have enough frames (absolute minimum, independent of rate);
* a segment must cover a duration threshold, not a fixed number of seconds;
* a discontinuity is dynamic on the recording-order elapsed axis:
  > max(minimum_gap, gap_factor * measured nominal period).  GPS-week resets
  (for example BeiYun default week 1356 -> true week) are time-axis resets and
  are collapsed to one nominal period, not treated as recording gaps;
* the first segment can be a valid cold start if it starts in the required
  starting state; a leading tail of a previous run is not treated as cold start;
* segment end is one byte after the last frame in that segment.  The next
  segment starts at its own first frame offset; when another stream happens to
  occupy bytes between two selected position frames, those bytes are not part
  of either position-segment selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from gnss_recovery import (PosFrame, RecoveryResult, parse_beidou_positions,
                           parse_beiyun_positions, parse_huace_positions,
                           recover_ascii_messages, select_huace_positions)

UNKNOWN_START = {'UNKNOWN', 'APPROXIMATE', 'COARSE', 'COARSESTEERING'}
NONE_OR_SINGLE = {'NONE', 'SINGLE', 'SPPDIFF', 'PSRDIFF'}
DIFFERENTIAL_FIXED = {
    'NARROW_INT', 'WIDE_INT', 'L1_INT', 'NARROW_FLOAT', 'L1_FLOAT',
    'PSRDIFF', 'SPPDIFF'
}


@dataclass
class Segment:
    index: int
    start: int
    end: int
    frames: int
    start_week: int
    start_tstat: str
    start_pos_type: str
    span: float
    is_coldstart: bool
    source: str
    start_elapsed: float
    end_elapsed: float
    discontinuity_before: float


def _nominal(frames: Sequence[PosFrame], default: float = 1.0) -> float:
    dts = sorted(b.t - a.t for a, b in zip(frames, frames[1:]) if 0 < b.t - a.t <= 3600.0)
    if not dts:
        return default
    return dts[len(dts) // 2]


def _elapsed_axis(times: Sequence[float], nominal: float) -> list[float]:
    """Build a recording-order elapsed axis, collapsing GPS-week resets.

    BeiYun cold starts can first report a default GPS week and later restore
    the true week.  Such raw timestamp jumps/reversals are protocol time-axis
    resets, not physical recording gaps.  They are collapsed to one nominal
    period, matching the elapsed-axis convention used by the reports.
    """
    elapsed = [0.0]
    for prev_t, cur_t in zip(times, times[1:]):
        dt = cur_t - prev_t
        if abs(dt) > 3600.0:
            elapsed.append(elapsed[-1] + nominal)
        elif dt > 0.0:
            elapsed.append(elapsed[-1] + dt)
        else:
            # Duplicate or slightly reversed timestamps are collapsed to one
            # nominal period, as in BY/HUACE build_elapsed().
            elapsed.append(elapsed[-1] + nominal)
    return elapsed


def _startup_state(r: PosFrame, time_required: bool = True) -> bool:
    """Startup-state frame test.

    BeiYun/HUACE cold starts lose the time solution, so the default rule
    requires a not-yet-established Time Status in addition to a no-solution /
    single-point state without a station ID.  UM980 keeps GPS time across a
    power cycle (battery-backed receiver clock; observed as FINE/true-week
    frames right after a restart), so for UM980 the essential cold-start
    evidence is a failed solution status instead: sol != SOL_COMPUTED together
    with a NONE/single-point position type and no station ID.
    """
    if r.pt not in NONE_OR_SINGLE or r.stn not in ('', '0'):
        return False
    if time_required:
        return r.ts in UNKNOWN_START
    return r.sol != 'SOL_COMPUTED'


def _was_differential_or_fixed(r: PosFrame) -> bool:
    return r.pt in DIFFERENTIAL_FIXED or r.stn not in ('', '0')


def segment_position_frames(
    frames: Sequence[PosFrame],
    *,
    min_frames: int = 50,
    min_duration_s: float = 20.0,
    gap_factor: float = 5.0,
    minimum_gap_s: float = 2.0,
    time_required: bool = True,
) -> list[Segment]:
    """Return all segments, including a leading tail segment when present.

    A boundary is accepted when either:
    1. there is a dynamic recording discontinuity, or
    2. a previous frame was differential/fixed/with station ID and the current
       frame is a startup-state frame (time not fully established, no station,
       NONE/SINGLE).
    The second condition is necessary because files can retain valid GPS time
    during a restart and therefore have no week rollback.

    ``time_required=False`` relaxes the startup-state test for receivers that
    keep a valid time across power cycles (UM980): the startup evidence is the
    failed solution status, not a lost Time Status.
    """
    if not frames:
        return []
    nominal = _nominal(frames)
    elapsed = _elapsed_axis([f.t for f in frames], nominal)
    gap_threshold = max(minimum_gap_s, gap_factor * nominal)
    boundary_indexes: list[int] = []
    discontinuities: dict[int, float] = {}

    for i in range(1, len(frames)):
        dt = elapsed[i] - elapsed[i - 1]
        raw_dt = frames[i].t - frames[i - 1].t
        recording_gap = dt > gap_threshold
        time_axis_reset = (
            abs(raw_dt) > 3600.0 and
            _startup_state(frames[i], time_required) and
            not _startup_state(frames[i - 1], time_required)
        )
        state_boundary = (
            _was_differential_or_fixed(frames[i - 1]) and
            _startup_state(frames[i], time_required)
        )
        if recording_gap or time_axis_reset or state_boundary:
            boundary_indexes.append(i)
            discontinuities[i] = dt

    bounds = sorted(set([0] + boundary_indexes + [len(frames)]))
    # A boundary at len(frames) would create no segment and is not needed.
    bounds = [b for b in bounds if b < len(frames)] + [len(frames)]
    all_segments: list[Segment] = []
    for a, b in zip(bounds, bounds[1:]):
        rows = frames[a:b]
        if not rows:
            continue
        duration = elapsed[b - 1] - elapsed[a]
        dt_before = discontinuities.get(a, 0.0)
        is_cold = (
            len(rows) >= min_frames and
            duration >= min_duration_s and
            _startup_state(rows[0], time_required)
        )
        all_segments.append(Segment(
            index=0,
            start=rows[0].start,
            end=rows[-1].end,
            frames=len(rows),
            start_week=0,
            start_tstat=rows[0].ts,
            start_pos_type=rows[0].pt,
            span=round(duration, 3),
            is_coldstart=is_cold,
            source=rows[0].source,
            start_elapsed=round(elapsed[a], 3),
            end_elapsed=round(elapsed[b - 1], 3),
            discontinuity_before=round(dt_before, 3),
        ))

    # The first segment is a leading tail unless it itself starts in the startup
    # state and meets the validity requirements.
    if all_segments and not all_segments[0].is_coldstart:
        all_segments[0].is_coldstart = False
    # Mark startup segments: a boundary may create a short/empty fragment; keep
    # all fragments in order so users can see them, but only valid cold starts
    # are returned by the convenience helper below.
    for i, seg in enumerate(all_segments, 1):
        seg.index = i
    return all_segments


def coldstart_segments(segments: Sequence[Segment]) -> list[Segment]:
    return [s for s in segments if s.is_coldstart]


def _frames_from_beiyun(raw: bytes) -> tuple[str, list[PosFrame], RecoveryResult]:
    recovered = recover_ascii_messages(raw)
    rows = parse_beiyun_positions(recovered.messages)
    if not rows:
        raise ValueError('未恢复到任何北云 BESTGNSSPOSA 有效定位帧')
    return 'BESTGNSSPOSA', rows, recovered


def _frames_from_huace(raw: bytes) -> tuple[str, list[PosFrame], RecoveryResult]:
    recovered = recover_ascii_messages(raw)
    candidates = parse_huace_positions(recovered.messages)
    source, rows, _ = select_huace_positions(candidates)
    return source, rows, recovered


def _frames_from_beidou(raw: bytes) -> tuple[str, list[PosFrame], RecoveryResult]:
    recovered = recover_ascii_messages(raw)
    rows = parse_beidou_positions(recovered.messages)
    if not rows:
        raise ValueError('未恢复到任何北斗 BESTNAVA 有效定位帧')
    return 'BESTNAVA', rows, recovered


def split_beiyun(raw: bytes, **kwargs) -> tuple[list[Segment], str, list[PosFrame], float, RecoveryResult]:
    source, rows, recovery = _frames_from_beiyun(raw)
    return segment_position_frames(rows, **kwargs), source, rows, _nominal(rows), recovery


def split_huace(raw: bytes, **kwargs) -> tuple[list[Segment], str, list[PosFrame], float, RecoveryResult]:
    source, rows, recovery = _frames_from_huace(raw)
    return segment_position_frames(rows, **kwargs), source, rows, _nominal(rows), recovery


def split_beidou(raw: bytes, **kwargs) -> tuple[list[Segment], str, list[PosFrame], float, RecoveryResult]:
    """Split a UM980 (Unicore N4) BESTNAVA stream into cold-start segments.

    UM980 keeps valid GPS time across power cycles, so the startup-state test
    is driven by the failed solution status (``time_required=False``) unless
    the caller overrides it.
    """
    kwargs.setdefault('time_required', False)
    source, rows, recovery = _frames_from_beidou(raw)
    return segment_position_frames(rows, **kwargs), source, rows, _nominal(rows), recovery
