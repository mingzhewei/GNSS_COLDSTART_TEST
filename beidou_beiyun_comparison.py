# -*- coding: utf-8 -*-
"""Outdoor BeiYun/BeiDou comparison report.

This report-level comparison reads each product's ``summary.json`` and pairs
segments by the BeiYun reference index used by BEIDOU_coldstart.py.  It does
not re-parse logs and does not alter either product's independent report.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BY = "北云"
BD = "".join(chr(x) for x in [21271, 26007])
DASH = chr(8212)


def _load(path: Path) -> list[dict]:
    with path.open(encoding='utf-8') as fp:
        rows = json.load(fp)
    return [r for r in rows if 'paired_matching_audit' not in r]


def _pair_index(rows: list[dict]) -> dict[int, dict]:
    return {int((r.get('segment') or {}).get('index', 0)): r for r in rows}


def _v(value, unit: str = 's') -> str:
    return DASH if value is None else f'{value}{unit}'


def _stats(values: list[float | None]) -> str:
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return DASH
    return f'{min(xs):.1f} / {sum(xs) / len(xs):.1f} / {max(xs):.1f} (n={len(xs)})'


def _setup_mpl() -> None:
    installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
    for name in ('Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'Arial Unicode MS'):
        if name in installed:
            plt.rcParams['font.sans-serif'] = [name]
            break
    plt.rcParams['axes.unicode_minus'] = False


def _pairs(by_rows: list[dict], bd_rows: list[dict]) -> list[tuple[int, dict, dict]]:
    by_map = _pair_index(by_rows)
    bd_map = _pair_index(bd_rows)
    return [(i, by_map[i], bd_map[i]) for i in sorted(set(by_map) & set(bd_map))]


def _make_images(pairs: list[tuple[int, dict, dict]], out_dir: Path) -> dict[str, Path]:
    _setup_mpl()
    xs = list(range(len(pairs)))
    labels = [f'cold{i:02d}' for i, _, _ in pairs]
    products = [BY, BD]
    colors = {BY: '#2563eb', BD: '#16a34a'}
    offsets = {BY: -0.19, BD: 0.19}

    fig, ax = plt.subplots(figsize=(max(11, 1.1 * len(pairs)), 4.8))
    for prod in products:
        off = [x + offsets[prod] for x in xs]
        ys = [bd['first']['fixed'] if prod == BD else by['first']['fixed']
              for _, by, bd in pairs]
        ax.bar(off, [0.0 if y is None else float(y) for y in ys], width=0.34,
               color=colors[prod], label=prod, zorder=3)
        for x, y in zip(off, ys):
            if y is not None:
                ax.text(x, y, f'{y:.1f}s', ha='center', va='bottom',
                        fontsize=7.5, color=colors[prod])
            else:
                ax.text(x, 0, DASH, ha='center', va='bottom', fontsize=7.5,
                        color='#64748b')
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel('同一启动序号（以北云 cold 段为编号基准）')
    ax.set_ylabel('自该段 t=0 到首固定（秒）')
    ax.set_title(f'{BY} / {BD} 冷启动首固定耗时对比', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', fontsize=9)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fixed_path = out_dir / 'img_首固定对比.png'
    fig.savefig(fixed_path, dpi=150)
    plt.close(fig)

    marks = [
        ('valid', '获得卫星时间', '#0ea5e9'),
        ('single', '首单点/伪距类', '#3b82f6'),
        ('float', '首浮点', '#f59e0b'),
        ('fixed', '首固定', '#16a34a'),
    ]
    fig, ax = plt.subplots(figsize=(max(11, 1.1 * len(pairs)), 5.0))
    shapes = {BY: 'o', BD: '^'}
    for key, label, color in marks:
        for prod in products:
            xs_plot, ys_plot = [], []
            for x, by, bd in zip(xs, pairs, pairs):
                row = bd[2] if prod == BD else by[1]
                value = row['first'].get(key)
                if value is not None:
                    xs_plot.append(x + offsets[prod])
                    ys_plot.append(float(value))
            if xs_plot:
                ax.scatter(xs_plot, ys_plot, marker=shapes[prod], s=34,
                           color=color, label=f'{prod} {label}')
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel('同一启动序号（以北云 cold 段为编号基准）')
    ax.set_ylabel('相对该产品段内 t=0（秒）')
    ax.set_title(f'{BY} / {BD} 关键里程碑对比', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', fontsize=8, ncol=2)
    fig.tight_layout()
    key_path = out_dir / 'img_关键里程碑对比.png'
    fig.savefig(key_path, dpi=150)
    plt.close(fig)
    return {'fixed': fixed_path, 'keytime': key_path}


def _img64(path: Path) -> str:
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def _md_table(md_lines: list[str], heading: str) -> str:
    inside = False
    rows: list[str] = []
    for line in md_lines:
        if line == '## ' + heading:
            inside = True
            continue
        if inside and line.startswith('## '):
            break
        if inside and line.startswith('|'):
            rows.append(line)
    if not rows:
        return '<p>无数据</p>'
    html_rows = []
    for r, line in enumerate(rows):
        cells = [c.strip() for c in line.strip('|').split('|')]
        tag = 'th' if r == 0 else 'td'
        html_rows.append('<tr>' + ''.join(f'<{tag}>{c}</{tag}>' for c in cells) + '</tr>')
    return '<table>' + ''.join(html_rows) + '</table>'


def build(by_path: Path, bd_path: Path, out_dir: Path) -> tuple[Path, Path]:
    by_rows = _load(by_path)
    bd_rows = _load(bd_path)
    paired = _pairs(by_rows, bd_rows)
    if not paired:
        raise SystemExit('北云与北斗没有相同的 coldNN 启动序号，无法生成联合对比报告')
    out_dir.mkdir(parents=True, exist_ok=True)
    imgs = _make_images(paired, out_dir)

    title = f'室外冷启动联合对比报告（{BY} + {BD}）'
    h1 = '首固定时间对比'
    h2 = '关键里程碑明细'
    h3 = '冷启动路径汇总对比'
    h4 = '数据质量摘要'
    by_sources = sorted({str((r.get('segment') or {}).get('source', '')) for r in by_rows})
    bd_sources = sorted({str((r.get('segment') or {}).get('source', '')) for r in bd_rows})

    lines = [
        '# ' + title,
        '',
        f'- 北云数据源：`{by_path.parent}`，有效段数：{len(by_rows)}，定位流：{chr(12289).join(by_sources) or DASH}',
        f'- 北斗数据源：`{bd_path.parent}`，有效段数：{len(bd_rows)}，定位流：{chr(12289).join(bd_sources) or DASH}',
        f'- 配对口径：北云 cold 段为主切割/编号基准；北斗在时间窗口内匹配后仍使用自身 BESTNAVA 边界。共 {len(paired)} 对。',
        '- 时间基准：各产品独立报告内该段自己的 t=0；本报告只比较同一启动序号的相对耗时。',
        '- 注意：GNSS 状态推断段不等于已证明的物理断电/上电时刻；设备间启动是否同步需外部事件记录确认。',
        '',
        '![首固定对比](img_首固定对比.png)',
        '',
        '## ' + h1,
        '',
        '| 启动序号 | 北云首固定 | 北斗首固定 | 北斗-北云 |',
        '|---:|---:|---:|---:|',
    ]
    for i, by, bd in paired:
        by_fixed = by['first']['fixed']
        bd_fixed = bd['first']['fixed']
        diff = None if by_fixed is None or bd_fixed is None else round(bd_fixed - by_fixed, 1)
        lines.append(f'| cold{i:02d} | {_v(by_fixed)} | {_v(bd_fixed)} | {_v(diff)} |')
    lines += [
        '',
        f"**首固定统计（min / 均值 / max）：北云 {_stats([by['first']['fixed'] for _, by, _ in paired])}；北斗 {_stats([bd['first']['fixed'] for _, _, bd in paired])}。**",
        '',
        '## ' + h2,
        '',
        '| 启动序号 | 北云获得时间 | 北斗获得时间 | 北云首单点 | 北斗首单点 | 北云首浮点 | 北斗首浮点 | 北云首固定 | 北斗首固定 |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for i, by, bd in paired:
        bf, df = by['first'], bd['first']
        lines.append(
            f"| cold{i:02d} | {_v(bf['valid'])} | {_v(df['valid'])} "
            f"| {_v(bf['single'])} | {_v(df['single'])} "
            f"| {_v(bf['float'])} | {_v(df['float'])} "
            f"| {_v(bf['fixed'])} | {_v(df['fixed'])} |"
        )
    lines += [
        '',
        '> 北斗 UM980 可由内部电池保持 GPS 时间，段首已持有效时标时“获得卫星时间”输出“—”。',
        '',
        '## ' + h3,
        '',
        '| 启动序号 | 北云 无时间 | 北云 时间→单点 | 北云 单点→浮点 | 北云 浮点→固定 | 北云 单点→固定 | 北斗 无时间 | 北斗 时间→单点 | 北斗 单点→浮点 | 北斗 浮点→固定 | 北斗 单点→固定 |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for i, by, bd in paired:
        bs = by.get('stages_express', by['stages'])
        ds = bd.get('stages_express', bd['stages'])
        lines.append(
            f"| cold{i:02d} | {_v(bs['no_time'])} | {_v(bs['time_to_single'])} | {_v(bs['single_to_float'])} | {_v(bs['float_to_fixed'])} | {_v(bs['single_to_fixed'])} "
            f"| {_v(ds['no_time'])} | {_v(ds['time_to_single'])} | {_v(ds['single_to_float'])} | {_v(ds['float_to_fixed'])} | {_v(ds['single_to_fixed'])} |"
        )
    lines += [
        '',
        '## ' + h4,
        '',
        '| 产品 | 定位流 | 有效段数 | 阶段顺序全部有效 | 校验失败报文 |',
        '|---|---|---:|---|---:|',
    ]
    for name, rows, sources in [(BY, by_rows, by_sources), (BD, bd_rows, bd_sources)]:
        bad = sum(int(r.get('recovery', {}).get('checksum_failed', r.get('crc_bad', 0)))
                  for r in rows)
        ok = all(r.get('stage_order_valid', False) for r in rows)
        lines.append(f'| {name} | {chr(47).join(sources) or DASH} | {len(rows)} | {"是" if ok else "否"} | {bad} |')

    md = '\n'.join(lines) + '\n'
    css = '''body{font-family:"Microsoft YaHei",sans-serif;max-width:1400px;margin:24px auto;padding:0 16px;color:#172033;background:#f8fafc}
h1,h2{color:#173b5c}.card{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:14px;margin:14px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{border:1px solid #cbd5e1;padding:7px 9px;text-align:right}
th{background:#e2e8f0}td:first-child,th:first-child{text-align:left}.note{background:#eef6ff;border-left:4px solid #2563eb;padding:10px 14px;margin:14px 0;font-size:13px}
.imgcard{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:10px;margin:14px 0;text-align:center}img{max-width:100%;height:auto}'''
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title><style>{css}</style></head><body><div class="wrap">
<h1>{title}</h1>
<div class="imgcard"><img src="{_img64(imgs['fixed'])}" alt="首固定对比"></div>
<h2>一、{h1}</h2><div class="card">{_md_table(lines, h1)}</div>
<div class="imgcard"><img src="{_img64(imgs['keytime'])}" alt="关键里程碑对比"></div>
<h2>二、{h2}</h2><div class="card">{_md_table(lines, h2)}</div>
<h2>三、{h3}</h2><div class="card">{_md_table(lines, h3)}</div>
<h2>四、{h4}</h2><div class="card">{_md_table(lines, h4)}</div>
</div></body></html>'''

    md_path = out_dir / 'comparison.md'
    html_path = out_dir / 'comparison.html'
    md_path.write_text(md, encoding='utf-8')
    html_path.write_text(html, encoding='utf-8')
    return html_path, md_path


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('by_summary', type=Path)
    parser.add_argument('bd_summary', type=Path)
    parser.add_argument('out_dir', type=Path)
    args = parser.parse_args()
    html_path, md_path = build(args.by_summary, args.bd_summary, args.out_dir)
    print('生成对比 HTML:', html_path)
    print('生成对比 MD  :', md_path)


if __name__ == '__main__':
    main()
