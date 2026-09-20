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
from coldstart_split import split_coldstart, first_valid_time_offset

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def paired_segments(by_path, hc_path, split):
    """按启动序号配对两家片段，返回 [(n, by_frag, hc_frag), ...]。"""
    if split:
        by_segs = BY.split_segments(by_path)
        hc_segs = HC.split_segments(hc_path)
    else:
        by_segs = [(1, open(by_path, 'rb').read(), None)]
        hc_segs = [(1, open(hc_path, 'rb').read(), None)]
    by_map = {n: frag for n, frag, _ in by_segs}
    hc_map = {n: frag for n, frag, _ in hc_segs}
    pairs = []
    for n in sorted(set(by_map) & set(hc_map)):
        pairs.append((n, by_map[n], hc_map[n]))
    return pairs


def run(by_path, hc_path, out_dir, split=True, open_browser=False):
    os.makedirs(out_dir, exist_ok=True)
    pairs = paired_segments(by_path, hc_path, split)
    if not pairs:
        raise SystemExit('北云与华测没有可配对的冷启动段（请确认两家段数一致）')

    data = []
    for n, by_frag, hc_frag in pairs:
        # 共同 t0 = 两家最早"拿到时标"的时刻（各自片段内的相对秒数，取较小者）
        by_off = first_valid_time_offset(by_frag, 'BESTGNSSPOSA', 'week_reset')
        hc_off = first_valid_time_offset(hc_frag, 'BESTPOSA', 'time_gap')
        offsets = [x for x in (by_off, hc_off) if x is not None]
        t0 = min(offsets) if offsets else 0.0
        by_shift = t0           # 北云片段左移 t0
        hc_shift = t0           # 华测片段左移 t0

        d_by = BY.build_raw(by_frag, f'北云_cold{n:02d}', 0, t0_shift=by_shift)
        d_hc = HC.build_raw(hc_frag, f'华测_cold{n:02d}', 0, t0_shift=hc_shift)
        d_by['product'] = '北云'
        d_hc['product'] = '华测'
        d_by['segment'] = dict(index=n, t0_shift=by_shift, by_off=by_off, hc_off=hc_off)
        d_hc['segment'] = dict(index=n, t0_shift=hc_shift, by_off=by_off, hc_off=hc_off)
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
