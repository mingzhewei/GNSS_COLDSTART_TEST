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
* a discontinuity is dynamic: > max(minimum_gap, gap_factor * measured nominal
  period), and is also required for a boundary when the state transition alone
  is ambiguous;
* the first segment can be a valid cold start if it starts in the required
  starting state; a leading tail of a previous run is not treated as cold start;
* segment end is the message offset just before the next segment start, so
  bytes are not discarded between segments (the next segment starts exactly at
  the previous segment's end boundary in message order).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from gnss_recovery import (PosFrame, RecoveryResult, parse_beiyun_positions,
                           parse_huace_positions, recover_ascii_messages,
                           select_huace_positions)

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
    dts = sorted(b.t - a.t for a, b in zip(frames, frames[1:]) if b.t > a.t)
    if not dts:
        return default
    return dts[len(dts) // 2]


def _startup_state(r: PosFrame) -> bool:
    return r.ts in UNKNOWN_START and r.pt in NONE_OR_SINGLE and r.stn in ('', '0')


def _was_differential_or_fixed(r: PosFrame) -> bool:
    return r.pt in DIFFERENTIAL_FIXED or r.stn not in ('', '0')


def segment_position_frames(
    frames: Sequence[PosFrame],
    *,
    min_frames: int = 50,
    min_duration_s: float = 20.0,
    gap_factor: float = 5.0,
    minimum_gap_s: float = 2.0,
) -> list[Segment]:
    """Return all segments, including a leading tail segment when present.

    A boundary is accepted when either:
    1. there is a dynamic recording discontinuity, or
    2. a previous frame was differential/fixed/with station ID and the current
       frame is a startup-state frame (time not fully established, no station,
       NONE/SINGLE).
    The second condition is necessary because files can retain valid GPS time
    during a restart and therefore have no week rollback.
    """
    if not frames:
        return []
    nominal = _nominal(frames)
    gap_threshold = max(minimum_gap_s, gap_factor * nominal)
    boundary_indexes: list[int] = []
    discontinuities: dict[int, float] = {}

    prev = frames[0]
    for i in range(1, len(frames)):
        cur = frames[i]
        dt = cur.t - prev.t
        recording_gap = dt > gap_threshold
        state_boundary = _was_differential_or_fixed(prev) and _startup_state(cur)
        if recording_gap or state_boundary:
            boundary_indexes.append(i)
            discontinuities[i] = dt
        prev = cur

    bounds = sorted(set([0] + boundary_indexes + [len(frames)]))
    # A boundary at len(frames) would create no segment and is not needed.
    bounds = [b for b in bounds if b < len(frames)] + [len(frames)]
    base_time = frames[0].t
    all_segments: list[Segment] = []
    for a, b in zip(bounds, bounds[1:]):
        rows = frames[a:b]
        if not rows:
            continue
        duration = rows[-1].t - rows[0].t
        dt_before = discontinuities.get(a, 0.0)
        is_cold = (
            len(rows) >= min_frames and
            duration >= min_duration_s and
            _startup_state(rows[0])
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
            start_elapsed=round(rows[0].t - base_time, 3),
            end_elapsed=round(rows[-1].t - base_time, 3),
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


def split_beiyun(raw: bytes, **kwargs) -> tuple[list[Segment], str, list[PosFrame], float, RecoveryResult]:
    source, rows, recovery = _frames_from_beiyun(raw)
    return segment_position_frames(rows, **kwargs), source, rows, _nominal(rows), recovery


def split_huace(raw: bytes, **kwargs) -> tuple[list[Segment], str, list[PosFrame], float, RecoveryResult]:
    source, rows, recovery = _frames_from_huace(raw)
    return segment_position_frames(rows, **kwargs), source, rows, _nominal(rows), recovery
