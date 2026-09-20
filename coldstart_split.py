# -*- coding: utf-8 -*-
"""
冷启动切片：把一段连续录制的 COM1 ASCII 字节流按"启动"切成多段（静态确定性函数，非大模型）。

切片判据（均依据报头自带字段，不依赖任何猜测）：
  * 北云（UG016）：冷启动后 Time Status=UNKNOWN、Week 回到默认周（UG016 手册：UNKNOWN 表明尚未
    计算出准确的 GPS 时间；实测默认周 1356，获得真实时间后跳回真实周）。因此以
    "Week 较前一次回落" 作为一次新启动的标志。
  * 华测（M7 手册）：连续录制、手动停/启的场景下，相邻两条报头时间差远大于标称周期的位置
    即一次停/启边界（数据实测为 141~228s 的录制间断，而文件内无周回落现象——华测时标用
    APPROXIMATE 起步而非 UNKNOWN，因此不能用周回落判据）。

对外接口：
    split_coldstart(raw, msg_name, mode, gap_factor=5.0, min_frames=50)
        raw       字节流
        msg_name  定位报文名（北云 'BESTGNSSPOSA'，华测 'BESTPOSA'）
        mode      'week_reset'（北云）或 'time_gap'（华测）
        返回 [Segment(...)]，每个 Segment 含 start/end 字节区间、帧数、起始周/时标、时长
"""
from dataclasses import dataclass


@dataclass
class Segment:
    index: int          # 第几次启动（从 1 开始，含 lead-in 段）
    start: int          # 起始字节（含）
    end: int            # 结束字节（不含）
    frames: int         # 定位报文帧数
    start_week: int     # 首帧 GPS 周
    start_tstat: str    # 首帧时标
    span: float         # 片段内报头时间跨度（秒；北云含周跳变，仅作参考）
    is_coldstart: bool  # 是否为一次有效冷启动（False=上次运行的尾部段）


def _iter_pos_lines(raw, msg_name):
    """逐条定位报文，产出 (line_start, line_end, week, seconds, tstat, pos_type)。只认带 CRC 的 # 报文。"""
    prefix = b'#' + msg_name.encode('ascii') + b','
    for m_start in _find_lines(raw, prefix):
        m_end = raw.find(b'\n', m_start)
        if m_end < 0:
            m_end = len(raw)
        line = raw[m_start:m_end].rstrip(b'\r')
        star = line.rfind(b'*')
        if star <= 0:
            continue
        head, _, bpart = line[1:star].decode('ascii', errors='ignore').partition(';')
        hf = head.split(',')
        bf = bpart.split(',')
        try:
            week = int(hf[5]); sec = float(hf[6]); tstat = hf[4]
            pos_type = bf[1] if len(bf) > 1 else ''
        except Exception:
            continue
        yield m_start, m_end, week, sec, tstat, pos_type


def _find_lines(raw, prefix):
    i = 0
    n = len(raw)
    while True:
        j = raw.find(prefix, i)
        if j < 0:
            return
        # 必须是行首（前一个字符是 \n 或在文件开头）
        if j == 0 or raw[j - 1:j] == b'\n':
            yield j
        i = j + 1


def split_coldstart(raw, msg_name, mode, gap_factor=5.0, min_frames=50):
    """把字节流切成多次启动片段。返回 (segments, nominal_period)。"""
    rows = list(_iter_pos_lines(raw, msg_name))
    if not rows:
        return [], 0.0
    # 标称周期（中位帧间隔）
    dts = sorted((rows[k][2] * 604800 + rows[k][3]) - (rows[k - 1][2] * 604800 + rows[k - 1][3])
                 for k in range(1, len(rows)))
    nominal = dts[len(dts) // 2] if dts else 0.2

    # 找边界（帧索引）
    bounds = [0]
    for k in range(1, len(rows)):
        t_prev = rows[k - 1][2] * 604800 + rows[k - 1][3]
        t_cur = rows[k][2] * 604800 + rows[k][3]
        if mode == 'week_reset':
            if rows[k][2] < rows[k - 1][2]:            # 周回落 = 新一次启动
                bounds.append(k)
        elif mode == 'time_gap':
            if t_cur - t_prev > nominal * gap_factor:  # 录制间断 = 新一次启动
                bounds.append(k)
        else:
            raise ValueError(f'未知切片模式: {mode}')
    bounds.append(len(rows))

    segments = []
    for idx in range(len(bounds) - 1):
        a, b = bounds[idx], bounds[idx + 1]
        start = rows[a][0]
        end = rows[b - 1][1]
        span = (rows[b - 1][2] * 604800 + rows[b - 1][3]) - (rows[a][2] * 604800 + rows[a][3])
        # 有效冷启动（干净冷启动）：断电无电池、时钟全丢 → 首帧必须处于默认时间/未知态。
        # 北云：UNKNOWN（此时 Week 为默认周）；FREEWHEELING 带真实时间，不算。
        # 华测：UNKNOWN 或 APPROXIMATE（近似时间，未收敛的起始态）；FINESTEERING/COARSESTEERING 不算。
        first_tstat = rows[a][4]
        if mode == 'week_reset':
            is_cold = (first_tstat == 'UNKNOWN') and (b - a) >= min_frames
        else:
            is_cold = (first_tstat in ('UNKNOWN', 'APPROXIMATE')) and (b - a) >= min_frames
        segments.append(Segment(index=idx + 1, start=start, end=end, frames=b - a,
                                start_week=rows[a][2], start_tstat=first_tstat,
                                span=round(span, 1), is_coldstart=is_cold))
    return segments, nominal

def first_valid_time_offset(raw, msg_name, mode):
    """片段内"拿到时标"距片段起点的秒数（用于室内模式对齐共同 t0）。
    拿到时标 = 时标首次脱离默认/未知态：
      北云 week_reset：Time Status 首次 != UNKNOWN；
      华测 time_gap  ：Time Status 首次 not in {UNKNOWN, APPROXIMATE}。
    返回相对秒数；找不到返回 None。"""
    rows = list(_iter_pos_lines(raw, msg_name))
    if not rows:
        return None
    t0 = rows[0][2] * 604800 + rows[0][3]
    for (_, _, week, sec, tstat, _) in rows:
        if mode == 'week_reset':
            valid = (tstat != 'UNKNOWN')
        else:
            valid = (tstat not in ('UNKNOWN', 'APPROXIMATE'))
        if valid:
            return (week * 604800 + sec) - t0
    return None
