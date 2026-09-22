# -*- coding: utf-8 -*-
"""
室内冷启动联合分析（北云 + 华测 同序号段配对，对齐共同 t0，出一份联合对比报告）

口径（用户确认，2026-09-20）：
  * 每次启动，北云与华测各有一份一一对应的段（按启动序号配对：北云 coldNN ↔ 华测 coldNN）。
  * 室内模式 t0：同一根天线、同时上电，但因需移到室外等卫星 ready，
    以"两家模组中较早拿到时标的那一刻"作为该次启动的共同 t0；
    t0 之前的数据（室内移动过程，快慢不一）直接丢弃，不计入。
  * "拿到时标" = 时标首次脱离默认/未知态：
      北云 Time Status 首次 != UNKNOWN；华测 首次 not in {UNKNOWN, APPROXIMATE}。
  * 共同 t0 之后，两家各自统计 拿到时间→单点→浮点→固定 的四阶段过程。

用法：
  python indoor_coldstart.py <输出目录> [--split] <北云文件> <华测文件>
  （--split 表示两份都是"一次性录制 N 次冷启动"，先各自切片再按序号配对）
"""
import os
import sys
import io
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import BY_coldstart as BY
import HUACE_coldstart as HC
from coldstart_split import split_beiyun, split_huace

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def paired_segments(by_path, hc_path, split):
    """按启动序号返回 [(序号, 北云帧, 华测帧, Segment_by, Segment_hc)]。"""
    if split:
        by_rows = BY.split_segments(by_path)
        hc_rows = HC.split_segments(hc_path)
        # Pair by cold-start index and fail explicitly instead of silently
        # truncating to the shorter recording.
        if len(by_rows) != len(hc_rows):
            print(f'错误：北云与华测自动切片数量不一致：北云={len(by_rows)}段，华测={len(hc_rows)}段；'
                  '请复核两侧物理启动次数或日志起止范围。')
            raise SystemExit(1)
        return [(n, bf, hf, bs, hs)
                for (n, bf, bs), (_n2, hf, hs) in zip(by_rows, hc_rows)]
    with open(by_path, 'rb') as fp:
        by_raw = fp.read()
    with open(hc_path, 'rb') as fp:
        hc_raw = fp.read()
    by_segs, _bs, by_frames, _bn, by_rec = split_beiyun(by_raw)
    hc_segs, _hs, hc_frames, _hn, hc_rec = split_huace(hc_raw)
    if not by_segs or not hc_segs:
        raise SystemExit('单次室内模式要求两份日志均能恢复定位帧')
    # 单次模式取首个有效段；若没有有效段，回退完整流并明确不作为多段切割结果。
    bs = next((x for x in by_segs if x.is_coldstart), by_segs[0])
    hs = next((x for x in hc_segs if x.is_coldstart), hc_segs[0])
    by_sel = [r for r in by_frames if bs.start <= r.start < bs.end]
    hc_sel = [r for r in hc_frames if hs.start <= r.start < hs.end]
    return [(1, by_sel, hc_sel, bs, hs)]

def run(by_path, hc_path, out_dir, split=True, open_browser=False):
    os.makedirs(out_dir, exist_ok=True)
    pairs = paired_segments(by_path, hc_path, split)
    if not pairs:
        raise SystemExit('北云与华测没有可配对的冷启动段（请确认两家段数一致）')

    data = []
    for n, by_frames, hc_frames, by_seg, hc_seg in pairs:
        # 共同 t0 = 两家最早"拿到时标"的时刻（各自片段内的相对秒数，取较小者）
        # Use the same elapsed-axis convention as build_raw(). BeiYun can reset
        # from default week 1356 to the true week, so raw timestamp differences
        # would be wrong by about 653.8 million seconds.
        def valid_offset(frames, valid):
            if not frames:
                return None
            elapsed, _gaps, _rebases, _nominal = BY.build_elapsed([f.t for f in frames])
            return next((elapsed[i] for i, frame in enumerate(frames) if valid(frame)), None)

        by_off = valid_offset(by_frames, lambda r: r.ts != 'UNKNOWN')
        hc_off = valid_offset(hc_frames, lambda r: r.ts not in ('UNKNOWN', 'APPROXIMATE'))
        offsets = [x for x in (by_off, hc_off) if x is not None]
        common_offset = min(offsets) if offsets else 0.0
        by_shift = by_off if by_off is not None else common_offset
        hc_shift = hc_off if hc_off is not None else common_offset

        d_by = BY.build_raw(b'', f'北云_cold{n:02d}', by_seg.start, t0_shift=by_shift, frames=by_frames)
        d_hc = HC.build_raw(b'', f'华测_cold{n:02d}', hc_seg.start, t0_shift=hc_shift, frames=hc_frames)
        d_by['product'] = '北云'
        d_hc['product'] = '华测'
        d_by['segment'] = dict(index=n, t0_shift=by_shift, by_off=by_off, hc_off=hc_off,
                               source=by_seg.source, span=by_seg.span,
                               discontinuity_before=by_seg.discontinuity_before)
        d_hc['segment'] = dict(index=n, t0_shift=hc_shift, by_off=by_off, hc_off=hc_off,
                               source=hc_seg.source, span=hc_seg.span,
                               discontinuity_before=hc_seg.discontinuity_before)
        data.extend([d_by, d_hc])

    # 用北云的报告模板出联合报告（产品列由 file 名区分）
    imgs = BY.make_images(data, out_dir, title_prefix='室内冷启动联合（北云+华测）')
    html, md = BY.build_outputs(data, imgs)
    # 联合报告标题通用化（含北云+华测，抬头不得只写一家）
    html = html.replace('北云（UG016）GNSS 冷启动策略分析', '室内冷启动联合分析（北云 UG016 + 华测 M720）')
    html = html.replace('<title>北云GNSS冷启动分析报告</title>', '<title>室内冷启动联合分析报告（北云+华测）</title>')
    md = md.replace('# 北云（UG016）GNSS 冷启动策略分析', '# 室内冷启动联合分析（北云 UG016 + 华测 M720）')
    h_path = os.path.join(out_dir, 'report.html')
    m_path = os.path.join(out_dir, 'report.md')
    with open(h_path, 'w', encoding='utf-8') as fp:
        fp.write(html)
    with open(m_path, 'w', encoding='utf-8') as fp:
        fp.write(md)
    js_path = os.path.join(out_dir, 'summary.json')
    with open(js_path, 'w', encoding='utf-8') as fp:
        json.dump([{k: v for k, v in d.items() if k != 'series'} for d in data],
                  fp, ensure_ascii=False, indent=1)
    print('生成 HTML:', h_path)
    print('生成 MD  :', m_path)
    print('摘要JSON :', js_path)
    if open_browser:
        import webbrowser
        webbrowser.open('file:///' + h_path.replace(chr(92), '/'))
    return h_path, m_path


def main():
    args = [a for a in sys.argv[1:]]
    split = '--split' in args
    args = [a for a in args if a != '--split']
    if len(args) < 3:
        print(__doc__)
        raise SystemExit(2)
    out_dir, by_path, hc_path = args[0], args[1], args[2]
    run(by_path, hc_path, out_dir, split=split)


if __name__ == '__main__':
    main()
