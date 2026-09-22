# -*- coding: utf-8 -*-
"""Protocol recovery and dynamic segmentation tests.

Run:  python test_coldstart_recovery.py
The tests use synthetic streams so they do not depend on local sample data.
"""
from pathlib import Path
import tempfile

from coldstart_split import (coldstart_segments, segment_position_frames,
                             split_beiyun, split_huace)
from gnss_recovery import PosFrame, recover_ascii_messages, parse_huace_positions


def crc32b(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc & 0xFFFFFFFF


def hash_sentence(payload: str) -> bytes:
    body = payload.encode('ascii')
    return b'#' + body + b'*%08X\r\n' % crc32b(body)


def binary_noise() -> bytes:
    return bytes.fromhex('aa447200' + '00112233445566778899aabbccddeeff')


def frame(t, ts, pt, stn, offset):
    return PosFrame(t=t, ts=ts, sol='SOL_COMPUTED', pt=pt, stn=stn, age=0.0,
                    svs=30, soln=30, multi=30, source='TEST', message_index=0,
                    start=offset, end=offset + 100)


def test_embedded_ascii_recovery():
    a = hash_sentence('BESTPA,1,0,0,COM1,FINESTEERING,2437,96800000,0,0;SOL_COMPUTED,NONE')
    raw = binary_noise() + a + binary_noise()
    recovered = recover_ascii_messages(raw)
    assert len(recovered.messages) == 1
    assert recovered.messages[0].name == 'BESTPA'
    assert not recovered.messages[0].at_line_start
    assert recovered.embedded == 1


def test_segment_boundary_without_week_rollback():
    frames = []
    off = 0
    for i in range(60):
        frames.append(frame(1000.0 + i * 0.2, 'FINESTEERING', 'NARROW_INT', '1793', off)); off += 100
    for i in range(60):
        frames.append(frame(1100.0 + i * 0.2, 'COARSE', 'NONE', '', off)); off += 100
    segs = segment_position_frames(frames, min_frames=50, min_duration_s=10.0)
    assert len(segs) == 2
    assert not segs[0].is_coldstart
    assert segs[1].is_coldstart
    assert segs[1].start == frames[60].start
    assert segs[0].end <= segs[1].start


def test_huace_stream_selection_prefers_continuous_bestpa():
    # Both cover the same time span, but BESTPA has more valid frames.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'mixed.log'
        body_a = ','.join(['SOL_COMPUTED','NONE','WGS84','0','31.0','121.0','6.0','0.0','0.01','0.01','0.01','1.0','1.0','30','30','30','30','0','0','0','0','0','1793'])
        payload_a = 'BESTPA,1,0,0,COM1,FINESTEERING,2437,96800000,0,0;' + body_a
        body_b = ','.join(['SOL_COMPUTED','NONE','31.0','121.0','6.0','0.0','WGS84','0.01','0.01','0.01','1793','1.0','1.0','30','30','30','30','0','0','0','0'])
        payload_b = 'BESTPOSA,COM1,0,1.0,FINESTEERING,2437,96800.000,0,0,0;' + body_b
        raw = binary_noise()
        for _ in range(20):
            raw += hash_sentence(payload_a)
        raw += binary_noise()
        for _ in range(2):
            raw += hash_sentence(payload_b)
        path.write_bytes(raw)
        segments, source, rows, nominal, recovery = split_huace(path.read_bytes())
        assert source == 'BESTPA'
        assert len(rows) == 20
        assert recovery.embedded >= 1


def test_huace_approximate_startup_is_not_full_time():
    # APPROXIMATE is documented as approximate time, not valid/fine time. The
    # first FINESTEERING frame is the "valid time" boundary used by reports.
    frames = []
    off = 0
    for i in range(35):
        frames.append(frame(1000.0 + i * 0.1, 'APPROXIMATE', 'NONE', '', off)); off += 100
    for i in range(15):
        frames.append(frame(1003.5 + i * 0.1, 'FINESTEERING', 'SINGLE', '', off)); off += 100
    segs = segment_position_frames(frames, min_frames=10, min_duration_s=1.0)
    assert len(segs) == 1
    assert segs[0].start_tstat == 'APPROXIMATE'
    # Segment start is a valid cold-start candidate even when approximate time
    # has already been restored by the receiver clock.
    assert segs[0].is_coldstart


def test_beiyun_frames_preserve_recording_order_across_week_reset():
    # Week 1356 is the receiver default observed during cold starts.  The true
    # week is restored later.  A timestamp sort would move later recorded
    # default-week frames before earlier true-week frames.
    frames = [
        frame(1_473_000_000.0, 'FINESTEERING', 'NARROW_INT', '1793', 0),
        frame(820_108_819.4, 'UNKNOWN', 'NONE', '', 200),
        frame(820_108_819.6, 'UNKNOWN', 'NONE', '', 400),
    ]
    assert [r.start for r in sorted(frames, key=lambda r: r.start)] == [0, 200, 400]
    segs = segment_position_frames(frames, min_frames=1, min_duration_s=0.0)
    assert len(segs) == 2
    assert not segs[0].is_coldstart
    assert segs[1].is_coldstart


def test_segmentation_uses_recording_order_not_timestamp_order():
    # Later default-week frames can have timestamps that sort before earlier
    # true-week frames.  Segmentation must not use those default-week offsets
    # as segment bounds in a way that makes an in-order segment empty.
    frames = []
    off = 0
    # Previous running tail.
    for i in range(60):
        frames.append(frame(1_473_000_000.0 + i * 0.2, 'FINESTEERING', 'NARROW_INT', '1793', off)); off += 100
    # New cold start: default week, later in the file, but numerically early.
    for i in range(60):
        frames.append(frame(820_108_819.0 + i * 0.2, 'UNKNOWN', 'NONE', '', off)); off += 100
    segs = segment_position_frames(frames, min_frames=50, min_duration_s=10.0)
    assert len(segs) == 2
    assert not segs[0].is_coldstart
    assert segs[1].is_coldstart
    assert segs[1].frames == 60


def test_timestamp_reset_from_freewheeling_is_a_boundary():
    # The first three BeiYun 0920 indoor starts changed from FREEWHEELING/NONE
    # to UNKNOWN/default-week.  They did not follow a fixed solution, but the
    # GPS timestamp reset is still a cold-start boundary.
    frames = [
        frame(1_473_000_000.0, 'FREEWHEELING', 'NONE', '', 0),
        frame(820_108_819.0, 'UNKNOWN', 'NONE', '', 100),
    ]
    segs = segment_position_frames(frames, min_frames=1, min_duration_s=0.0)
    assert len(segs) == 2
    assert segs[1].start == 100
    assert segs[1].is_coldstart


if __name__ == '__main__':
    test_embedded_ascii_recovery()
    test_segment_boundary_without_week_rollback()
    test_huace_stream_selection_prefers_continuous_bestpa()
    test_huace_approximate_startup_is_not_full_time()
    test_beiyun_frames_preserve_recording_order_across_week_reset()
    test_segmentation_uses_recording_order_not_timestamp_order()
    test_timestamp_reset_from_freewheeling_is_a_boundary()
    print('all tests passed')
