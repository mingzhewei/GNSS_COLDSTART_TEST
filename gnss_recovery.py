# -*- coding: utf-8 -*-
"""Robust recovery of ASCII messages embedded in mixed binary/ASCII GNSS logs.

The recovery rule is deliberately protocol-based rather than line-based:
* a candidate must start with ``#`` or ``$`` and have a plausible ASCII message name;
* ``#`` messages are accepted only when their reflected CRC-32 matches;
* ``$`` messages are accepted only when their NMEA XOR checksum matches;
* a message does not need to begin after a newline, so ASCII messages embedded
  between binary blocks are recovered as long as the complete sentence and its
  checksum are present.

The module also provides product-specific position-frame parsing.  HUACE has two
ASCII header layouts documented in the M7 V2.7 manual: NovAtel-style messages
(for example BESTPOSA) use week field 6 and seconds field 7, while GMF messages
(for example BESTPA/RTKPA) use week field 7 and TOW-ms field 8 (one-based field
numbers).  These layouts are kept separate and are never silently normalized.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import statistics
from typing import Iterable


@dataclass
class RecoveredMessage:
    name: str
    hf: list[str] | None
    bf: list[str]
    kind: str                 # 'hash' or 'nmea'
    start: int
    end: int                  # exclusive, after newline when present
    at_line_start: bool


@dataclass
class RecoveryResult:
    messages: list[RecoveredMessage]
    bad_offsets: list[int] = field(default_factory=list)

    @property
    def bad(self) -> int:
        return len(self.bad_offsets)

    @property
    def embedded(self) -> int:
        return sum(not m.at_line_start for m in self.messages)

    def range(self, start: int, end: int) -> tuple[list[RecoveredMessage], int]:
        msgs = [m for m in self.messages if start <= m.start < end]
        bad = sum(start <= off < end for off in self.bad_offsets)
        return msgs, bad


@dataclass
class PosFrame:
    t: float
    ts: str
    sol: str
    pt: str
    stn: str
    age: float
    svs: int
    soln: int
    multi: int
    source: str
    message_index: int
    start: int
    end: int


def crc32b(data: bytes) -> int:
    """Reflected CRC-32 used by '#' ASCII messages (poly 0xEDB88320, init 0)."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc & 0xFFFFFFFF


def _plausible_name(raw: bytes, start: int) -> tuple[str, int] | None:
    """Return an ASCII message name and the offset of its following comma."""
    n = len(raw)
    i = start + 1
    if i >= n or not (65 <= raw[i] <= 90):
        return None
    j = i + 1
    while j < n:
        b = raw[j]
        if (48 <= b <= 57) or (65 <= b <= 90) or b == 95:
            j += 1
            continue
        if b == 44:
            name = raw[i:j].decode('ascii')
            return (name, j + 1) if len(name) >= 3 else None
        return None
    return None


def recover_ascii_messages(raw: bytes) -> RecoveryResult:
    """Recover checksum-valid ASCII sentences from a raw byte stream.

    Only plausible message-name candidates are counted as failures.  Random '#'
    or '$' bytes inside binary blocks are ignored rather than being reported as
    damaged protocol messages.
    """
    messages: list[RecoveredMessage] = []
    bad_offsets: list[int] = []
    i = 0
    n = len(raw)
    while i < n:
        h = raw.find(b'#', i)
        d = raw.find(b'$', i)
        if h < 0 and d < 0:
            break
        if h < 0 or (0 <= d < h):
            start, marker = d, 0x24
        else:
            start, marker = h, 0x23
        plausible = _plausible_name(raw, start)
        if plausible is None:
            i = start + 1
            continue
        _, comma_offset = plausible

        newline = raw.find(b'\n', start)
        if newline < 0:
            line_end = n
            i = n
        else:
            line_end = newline + 1
            i = line_end
        line = raw[start:newline if newline >= 0 else n].rstrip(b'\r')
        star = line.rfind(b'*')
        if star <= 0:
            bad_offsets.append(start)
            continue
        body = line[1:star]
        try:
            if marker == 0x23:
                checksum = crc32b(body)
                expected = int(line[star + 1:star + 9], 16)
                checksum_len = 8
            else:
                checksum = 0
                for b in body:
                    checksum ^= b
                expected = int(line[star + 1:star + 3], 16)
                checksum_len = 2
        except (ValueError, IndexError):
            bad_offsets.append(start)
            continue
        if checksum != expected or len(line) < star + 1 + checksum_len:
            bad_offsets.append(start)
            continue
        try:
            txt = body.decode('ascii')
        except UnicodeDecodeError:
            bad_offsets.append(start)
            continue

        if marker == 0x23:
            head, sep, data_part = txt.partition(';')
            if not sep:
                bad_offsets.append(start)
                continue
            hf = head.split(',')
            bf = data_part.split(',')
            name = hf[0] if hf else ''
            kind = 'hash'
        else:
            hf = None
            bf = txt.split(',')
            name = bf[0] if bf else ''
            kind = 'nmea'
        if not name:
            bad_offsets.append(start)
            continue
        messages.append(RecoveredMessage(
            name=name, hf=hf, bf=bf, kind=kind, start=start, end=line_end,
            at_line_start=(start == 0 or raw[start - 1:start] == b'\n')
        ))
    return RecoveryResult(messages=messages, bad_offsets=bad_offsets)


def message_counts(messages: Iterable[RecoveredMessage]) -> tuple[dict[str, int], dict[str, int]]:
    counts: Counter[str] = Counter()
    other: Counter[str] = Counter()
    for m in messages:
        if m.kind == 'hash':
            counts[m.name] += 1
        else:
            other[m.name] += 1
    return dict(counts), dict(other)


def _valid_station(value: str) -> bool:
    return value not in ('', '0')


def _parse_common_body(bf: list[str], novatel: bool) -> tuple[str, str, str, float, int, int, int]:
    if novatel:
        # BESTPOSA/BESTGNSSPOSA body: sol, pos, ..., stn[10], age[11], svs[13],
        # soln[14], L1[15], multi[16].
        if len(bf) < 21:
            raise ValueError('short NovAtel-style position body')
        return (bf[0], bf[1], bf[10].strip('"'), float(bf[11]),
                int(bf[13]), int(bf[14]), int(bf[16]))
    # GMF BESTPA/RTKPA body: sol, pos, ..., age[11], svs[13], soln[14],
    # multi[15], L1[16], ..., stn[22].
    if len(bf) < 23:
        raise ValueError('short GMF position body')
    return (bf[0], bf[1], bf[22].strip('"'), float(bf[11]),
            int(bf[13]), int(bf[14]), int(bf[15]))


def _novatel_time(hf: list[str]) -> float:
    return int(hf[5]) * 604800.0 + float(hf[6])


def _gmf_time(hf: list[str]) -> float:
    return int(hf[6]) * 604800.0 + float(hf[7]) / 1000.0


def parse_beiyun_positions(messages: Iterable[RecoveredMessage]) -> list[PosFrame]:
    rows: list[PosFrame] = []
    for idx, m in enumerate(messages):
        if m.kind != 'hash' or m.hf is None or m.name != 'BESTGNSSPOSA':
            continue
        try:
            t = _novatel_time(m.hf)
            sol, pt, stn, age, svs, soln, multi = _parse_common_body(m.bf, novatel=True)
        except (ValueError, IndexError):
            continue
        rows.append(PosFrame(t=t, ts=m.hf[4], sol=sol, pt=pt, stn=stn, age=age,
                             svs=svs, soln=soln, multi=multi, source=m.name,
                             message_index=idx, start=m.start, end=m.end))
    rows.sort(key=lambda r: (r.t, r.start))
    return rows


def parse_huace_positions(messages: Iterable[RecoveredMessage]) -> dict[str, list[PosFrame]]:
    candidates: dict[str, list[PosFrame]] = {}
    for idx, m in enumerate(messages):
        if m.kind != 'hash' or m.hf is None:
            continue
        base = m.name.split('_', 1)[0]
        source = ''
        novatel = False
        if base == 'BESTPOSA':
            source, novatel = 'BESTPOSA', True
        elif base in ('BESTPA', 'RTKPA'):
            source, novatel = base, False
        else:
            continue
        try:
            t = _novatel_time(m.hf) if novatel else _gmf_time(m.hf)
            sol, pt, stn, age, svs, soln, multi = _parse_common_body(m.bf, novatel=novatel)
        except (ValueError, IndexError):
            continue
        candidates.setdefault(source, []).append(PosFrame(
            t=t, ts=m.hf[5] if novatel else m.hf[5], sol=sol, pt=pt, stn=stn,
            age=age, svs=svs, soln=soln, multi=multi, source=source,
            message_index=idx, start=m.start, end=m.end
        ))
    for rows in candidates.values():
        rows.sort(key=lambda r: (r.t, r.start))
    return candidates


def positive_median_dt(times: list[float], default: float = 0.0) -> float:
    dts = [b - a for a, b in zip(times, times[1:]) if b > a]
    return float(statistics.median(dts)) if dts else default


def select_huace_positions(candidates: dict[str, list[PosFrame]]) -> tuple[str, list[PosFrame], float]:
    """Select the HUACE position stream dynamically.

    Streams whose coverage is at least 90% of the best observed coverage are
    considered comparable.  Among those, the stream with the most valid frames
    is selected; ties prefer the shorter measured period.  This adapts to logs
    containing continuous GMF BESTPA, NovAtel-style BESTPOSA, or only RTKPA,
    without assuming a fixed recording duration or output rate.
    """
    if not candidates:
        raise ValueError('未恢复到任何华测 BESTPA/BESTPOSA/RTKPA 有效定位帧')
    stats = []
    for source, rows in candidates.items():
        if len(rows) < 2:
            continue
        coverage = rows[-1].t - rows[0].t
        nominal = positive_median_dt([r.t for r in rows])
        stats.append((source, rows, coverage, nominal))
    if not stats:
        source, rows = next(iter(candidates.items()))
        return source, rows, 0.0
    best_coverage = max(x[2] for x in stats)
    comparable = [x for x in stats if x[2] >= 0.90 * best_coverage] if best_coverage > 0 else stats
    source, rows, _, nominal = max(
        comparable,
        key=lambda x: (len(x[1]), -x[3], {'BESTPA': 3, 'BESTPOSA': 2, 'RTKPA': 1}.get(x[0], 0))
    )
    return source, rows, nominal


def detect_product_from_raw(raw: bytes) -> str | None:
    """Detect BY/HUACE from checksum-valid protocol messages in a full file."""
    recovered = recover_ascii_messages(raw)
    names = {m.name.split('_', 1)[0] for m in recovered.messages if m.kind == 'hash'}
    if 'BESTGNSSPOSA' in names or 'TRACKSTATA' in names:
        return 'beiyun'
    if names & {'BESTPA', 'BESTPOSA', 'RTKPA', 'RANGEA', 'ENVSTATUSA'}:
        return 'huace'
    return None


def detect_product(path: str) -> str | None:
    with open(path, 'rb') as fp:
        return detect_product_from_raw(fp.read())
