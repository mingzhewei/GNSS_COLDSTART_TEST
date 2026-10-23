# -*- coding: utf-8 -*-
"""
室内冷启动联合分析（任意 ≥2 个产品同序号段配对，对齐共同 t0，出一份联合对比报告）

口径（用户确认，2026-09-20；2026-10-23 扩展为可扩展多产品接口）：
  * 每次启动，参与产品各有一份一一对应的段（按启动序号配对：各产品 coldNN ↔ coldNN）。
  * 室内模式 t0：同一根天线、同时上电，但因需移到室外等卫星 ready，
    以"各模组中较早拿到时标的那一刻"作为该次启动的共同 t0；
    t0 之前的数据（室内移动过程，快慢不一）直接丢弃，不计入。
  * "拿到时标" = 时标首次脱离默认/未知态：
      北云 Time Status 首次 != UNKNOWN；
      华测 首次 not in {UNKNOWN, APPROXIMATE}；
      北斗 UM980 首次 != UNKNOWN（断电后时标可由内部电池保持，保持时该段偏移为 0）。
  * 共同 t0 之后，各产品各自统计 拿到时间→单点→浮点→固定 的四阶段过程。

产品扩展（可扩展接口）：
  * 产品在 PRODUCTS 注册表登记（key / 报告名 / 分析模块 / "拿到时标"判据）；
  * 文件的产品类型由报文自动识别（gnss_recovery.detect_product），不依赖文件名；
  * 两产品两两对比（如 北斗×北云）与三产品及以上任意组合对比走同一配对接口。

用法：
  python indoor_coldstart.py <输出目录> [--split] <日志1> <日志2> [...]
  （--split 表示各份都是"一次性录制 N 次冷启动"，先各自切片再按序号配对；
    至少 2 个不同产品、每个产品 1 个文件）
"""
import os
import sys
import io
import json
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import BY_coldstart as BY
import HUACE_coldstart as HC
import BEIDOU_coldstart as BD
from coldstart_split import split_beidou, split_beiyun, split_huace
from gnss_recovery import PosFrame, detect_product

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


@dataclass(frozen=True)
class ProductSpec:
    key: str                                   # detect_product() 返回值
    name: str                                  # 报告内产品名（文件名前缀，如 北云_cold01）
    module: object                             # 各产品分析模块（提供 split_segments / build_raw）
    split_raw: Callable                         # 原始字节流 -> (segs, source, frames, nominal, recovery)
    time_valid: Callable[[PosFrame], bool]     # "拿到时标"判据


# 可扩展产品注册表：新增产品时登记一项即可参与任意组合配对。
PRODUCTS: dict[str, ProductSpec] = {
    'beiyun': ProductSpec('beiyun', '北云', BY, split_beiyun,
                          lambda r: r.ts != 'UNKNOWN'),
    'huace': ProductSpec('huace', '华测', HC, split_huace,
                         lambda r: r.ts not in ('UNKNOWN', 'APPROXIMATE')),
    'beidou': ProductSpec('beidou', '北斗', BD, split_beidou,
                          lambda r: r.ts != 'UNKNOWN'),
}

# 报告标题用全称
FULL_NAME = {'北云': '北云 UG016', '华测': '华测 M720', '北斗': '北斗 UM980'}


def detect_specs(paths):
    """按报文内容识别各文件的产品类型，返回 [ProductSpec]。"""
    specs = []
    for p in paths:
        key = detect_product(p)
        if key not in PRODUCTS:
            raise SystemExit(f'错误：无法识别产品类型：{p}（未检测到已知定位报文，'
                             '或文件损坏/校验失败）')
        if any(s.key == key for s in specs):
            raise SystemExit(f'错误：同一产品（{PRODUCTS[key].name}）只允许 1 个文件参与配对：{p}')
        specs.append(PRODUCTS[key])
    return specs


def paired_segments(specs, paths, split):
    """按启动序号返回 [(序号, {key: 帧子序列}, {key: Segment})]。"""
    if len(specs) < 2:
        raise SystemExit('错误：室内联合模式需要至少 2 个不同产品的日志文件')
    if split:
        per = {spec.key: spec.module.split_segments(path) for spec, path in zip(specs, paths)}
        counts = {k: len(v) for k, v in per.items()}
        # Pair by cold-start index and fail explicitly instead of silently
        # truncating to the shorter recording.
        if len(set(counts.values())) != 1:
            detail = '，'.join(f'{PRODUCTS[k].name}={n}段' for k, n in counts.items())
            print(f'错误：各产品自动切片数量不一致：{detail}；'
                  '请复核各机物理启动次数或日志起止范围。')
            raise SystemExit(1)
        n = next(iter(counts.values()))
        return [(i + 1,
                 {k: per[k][i][1] for k in per},
                 {k: per[k][i][2] for k in per})
                for i in range(n)]

    frames_by, segs_by = {}, {}
    for spec, path in zip(specs, paths):
        with open(path, 'rb') as fp:
            raw = fp.read()
        segs, _source, frames, _nominal, _rec = spec.split_raw(raw)
        if not segs:
            raise SystemExit(f'单次室内模式要求各份日志均能恢复定位帧：{path}')
        # 单次模式取首个有效段；若没有有效段，回退完整流并明确不作为多段切割结果。
        s = next((x for x in segs if x.is_coldstart), segs[0])
        frames_by[spec.key] = [r for r in frames if s.start <= r.start < s.end]
        segs_by[spec.key] = s
    return [(1, frames_by, segs_by)]


def run(paths, out_dir, split=True, open_browser=False):
    os.makedirs(out_dir, exist_ok=True)
    specs = detect_specs(paths)
    pairs = paired_segments(specs, paths, split)
    if not pairs:
        raise SystemExit('各产品没有可配对的冷启动段（请确认各机段数一致）')

    data = []
    for n, frames_by, segs_by in pairs:
        # 共同 t0 = 各机最早"拿到时标"的时刻（各自片段内的相对秒数，取较小者）。
        # Use the same elapsed-axis convention as build_raw(). BeiYun can reset
        # from default week 1356 to the true week, UM980 from default week 1,
        # so raw timestamp differences would be wrong by hundreds of millions
        # of seconds.
        def valid_offset(frames, valid):
            if not frames:
                return None
            elapsed, _gaps, _rebases, _nominal = BY.build_elapsed([f.t for f in frames])
            return next((elapsed[i] for i, frame in enumerate(frames) if valid(frame)), None)

        offsets = {k: valid_offset(frames_by[k], PRODUCTS[k].time_valid) for k in frames_by}
        known = [x for x in offsets.values() if x is not None]
        common_offset = min(known) if known else 0.0
        shifts = {k: (offsets[k] if offsets[k] is not None else common_offset)
                  for k in offsets}

        for spec in sorted(specs, key=lambda s: list(PRODUCTS).index(s.key)):
            k = spec.key
            d = spec.module.build_raw(b'', f'{spec.name}_cold{n:02d}', segs_by[k].start,
                                      t0_shift=shifts[k], frames=frames_by[k])
            d['product'] = spec.name
            d['segment'] = dict(index=n, t0_shift=shifts[k], valid_offsets=offsets,
                                source=segs_by[k].source, span=segs_by[k].span,
                                discontinuity_before=segs_by[k].discontinuity_before)
            data.append(d)

    # 用北云的报告模板出联合报告（产品列由 file 名区分）
    specs = sorted(specs, key=lambda s: list(PRODUCTS).index(s.key))
    names = [s.name for s in specs]
    title = ' + '.join(FULL_NAME.get(n, n) for n in names)
    imgs = BY.make_images(data, out_dir, title_prefix=f'室内冷启动联合（{"+".join(names)}）')
    html, md = BY.build_outputs(data, imgs)
    # 联合报告标题通用化（含全部参与产品，抬头不得只写一家）
    html = html.replace('北云（UG016）GNSS 冷启动策略分析', f'室内冷启动联合分析（{title}）')
    html = html.replace('<title>北云GNSS冷启动分析报告</title>',
                        f'<title>室内冷启动联合分析报告（{"+".join(names)}）</title>')
    md = md.replace('# 北云（UG016）GNSS 冷启动策略分析', f'# 室内冷启动联合分析（{title}）')
    note = ('说明：“获得卫星时间”为“—”表示该产品该次启动时标未丢失'
            '（如北斗 UM980 断电后由内部电池保持 GPS 时间），不参与对比；'
            '“多频解算”为“—”表示该产品报文无 #solnMultiSVs 字段（如北斗 BESTNAV 表 7-70）。')
    html = html.replace('</body></html>', f'<div class="note">{note}</div></body></html>')
    md += '\n> ' + note
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
    out_dir, paths = args[0], args[1:]
    run(paths, out_dir, split=split)


if __name__ == '__main__':
    main()
