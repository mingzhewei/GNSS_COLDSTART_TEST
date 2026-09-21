# -*- coding: utf-8 -*-
"""Generate an outdoor BY/HUACE comparison report from the two product reports.

This is a report-level comparison: it reads each product's ``summary.json``,
pairs segments by ``segment.index``, and emits a standalone comparison report.
It does not re-parse or alter the original logs.
"""
from __future__ import annotations

import json
from pathlib import Path


def _load(path: Path) -> list[dict]:
    with path.open(encoding='utf-8') as fp:
        return json.load(fp)


def _pair_index(rows: list[dict]) -> dict[int, dict]:
    return {int((r.get('segment') or {}).get('index', 0)): r for r in rows}


def _v(value, unit: str = 's') -> str:
    return '—' if value is None else f'{value}{unit}'


def build(by_path: Path, hc_path: Path, out_dir: Path) -> tuple[Path, Path]:
    by_rows = _load(by_path)
    hc_rows = _load(hc_path)
    by_map = _pair_index(by_rows)
    hc_map = _pair_index(hc_rows)
    indexes = sorted(set(by_map) & set(hc_map))

    lines: list[str] = []
    lines += [
        '# 室外冷启动联合对比报告（北云 + 华测）',
        '',
        f'- 北云数据源：`{by_path.parent}`，有效段数：{len(by_rows)}',
        f'- 华测数据源：`{hc_path.parent}`，有效段数：{len(hc_rows)}',
        f'- 配对口径：按自动切割段序号 `coldNN` 一一对应，共 {len(indexes)} 对。',
        '- 时间基准：各产品独立报告内该段自己的 t=0；本报告只比较同一启动序号的相对耗时。',
        '- 注意：GNSS 状态推断段不等于已证明的物理断电/上电时刻；设备间启动是否同步需外部事件记录确认。',
        '',
        '## 一、首固定时间对比',
        '',
        '| 启动序号 | 北云首固定 | 华测首固定 | 华测-北云 |',
        '|---:|---:|---:|---:|',
    ]
    for i in indexes:
        by = by_map[i]
        hc = hc_map[i]
        by_fixed = by['first']['fixed']
        hc_fixed = hc['first']['fixed']
        diff = None if by_fixed is None or hc_fixed is None else round(hc_fixed - by_fixed, 1)
        lines.append(f'| cold{i:02d} | {_v(by_fixed)} | {_v(hc_fixed)} | {_v(diff)} |')

    lines += [
        '',
        '## 二、获得卫星时间对比',
        '',
        '华测口径：Time Status 首次脱离 UNKNOWN/APPROXIMATE；北云口径：首次脱离 UNKNOWN。',
        '',
        '| 启动序号 | 北云获得时间 | 华测获得时间 | 华测-北云 |',
        '|---:|---:|---:|---:|',
    ]
    for i in indexes:
        by = by_map[i]['first']['valid']
        hc = hc_map[i]['first']['valid']
        diff = None if by is None or hc is None else round(hc - by, 1)
        lines.append(f'| cold{i:02d} | {_v(by)} | {_v(hc)} | {_v(diff)} |')

    lines += [
        '',
        '## 三、首单点/首浮点对比',
        '',
        '| 启动序号 | 北云首单点 | 华测首单点 | 北云首浮点 | 华测首浮点 |',
        '|---:|---:|---:|---:|---:|',
    ]
    for i in indexes:
        bf = by_map[i]['first']
        hf = hc_map[i]['first']
        lines.append(
            f"| cold{i:02d} | {_v(bf['single'])} | {_v(hf['single'])} "
            f"| {_v(bf['float'])} | {_v(hf['float'])} |"
        )

    lines += [
        '',
        '## 四、冷启动路径汇总对比',
        '',
        '“单点→固定”表示该段没有出现浮点解，直接从单点类进入固定解。',
        '',
        '| 启动序号 | 北云 无时间 | 北云 时间→单点 | 北云 单点→浮点 | 北云 浮点→固定 | 北云 单点→固定 | 华测 无时间 | 华测 时间→单点 | 华测 单点→浮点 | 华测 浮点→固定 | 华测 单点→固定 |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for i in indexes:
        bs = by_map[i].get('stages_express', by_map[i]['stages'])
        hs = hc_map[i].get('stages_express', hc_map[i]['stages'])
        lines.append(
            f"| cold{i:02d} | {_v(bs['no_time'])} | {_v(bs['time_to_single'])} "
            f"| {_v(bs['single_to_float'])} | {_v(bs['float_to_fixed'])} | {_v(bs['single_to_fixed'])} "
            f"| {_v(hs['no_time'])} | {_v(hs['time_to_single'])} "
            f"| {_v(hs['single_to_float'])} | {_v(hs['float_to_fixed'])} | {_v(hs['single_to_fixed'])} |"
        )

    lines += [
        '',
        '## 五、数据质量摘要',
        '',
        '| 产品 | 定位流 | 有效段数 | 阶段顺序全部有效 | 校验失败报文 |',
        '|---|---|---:|---|---:|',
    ]
    for name, rows in [('北云', by_rows), ('华测', hc_rows)]:
        bad = sum(int(r.get('recovery', {}).get('checksum_failed', r.get('crc_bad', 0))) for r in rows)
        source = sorted({str((r.get('segment') or {}).get('source', '')) for r in rows})
        ok = all(r.get('stage_order_valid', False) for r in rows)
        lines.append(f'| {name} | {"/".join(source) or "—"} | {len(rows)} | {"是" if ok else "否"} | {bad} |')

    md = '\n'.join(lines) + '\n'
    html = f'''<!doctype html><html><head><meta charset="utf-8">
<title>室外冷启动联合对比报告（北云+华测）</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;max-width:1400px;margin:24px auto;color:#172033;background:#f8fafc}}
h1,h2{{color:#173b5c}} table{{border-collapse:collapse;width:100%;background:white;font-size:13px}}
th,td{{border:1px solid #cbd5e1;padding:7px 9px;text-align:right}} th{{background:#e2e8f0}} td:first-child,th:first-child{{text-align:left}}
.note{{background:#eef6ff;border-left:4px solid #2563eb;padding:10px 14px;margin:14px 0}}
</style></head><body>
<h1>室外冷启动联合对比报告（北云 + 华测）</h1>
<div class="note">本报告由两个产品独立报告的 summary.json 自动生成；按 coldNN 段序号配对。GNSS 状态推断段不等于物理断电/上电时刻。</div>
<pre style="white-space:pre-wrap;background:white;padding:16px;border:1px solid #cbd5e1">{md}</pre>
</body></html>'''

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / 'comparison.md'
    html_path = out_dir / 'comparison.html'
    md_path.write_text(md, encoding='utf-8')
    html_path.write_text(html, encoding='utf-8')
    return html_path, md_path


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('by_summary', type=Path)
    parser.add_argument('hc_summary', type=Path)
    parser.add_argument('out_dir', type=Path)
    args = parser.parse_args()
    html_path, md_path = build(args.by_summary, args.hc_summary, args.out_dir)
    print('生成对比 HTML:', html_path)
    print('生成对比 MD  :', md_path)


if __name__ == '__main__':
    main()
