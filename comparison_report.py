# -*- coding: utf-8 -*-
"""Generate an outdoor BY/HUACE comparison report from the two product reports.

This is a report-level comparison: it reads each product's ``summary.json``,
pairs segments by ``segment.index``, and emits a standalone comparison report.
It does not re-parse or alter the original logs.
"""
from __future__ import annotations

import json
from pathlib import Path

BY = "".join(chr(x) for x in [21271,20113])
HC = "".join(chr(x) for x in [21326,27979])
DASH = chr(8212)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt



def _load(path: Path) -> list[dict]:
    with path.open(encoding='utf-8') as fp:
        return json.load(fp)


def _pair_index(rows: list[dict]) -> dict[int, dict]:
    return {int((r.get('segment') or {}).get('index', 0)): r for r in rows}


def _v(value, unit: str = 's') -> str:
    return '\u2014' if value is None else f'{value}{unit}'


def _stats(values: list[float | None]) -> str:
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return '\u2014'
    return f'{min(xs):.1f} / {sum(xs) / len(xs):.1f} / {max(xs):.1f} (n={len(xs)})'


def _setup_mpl() -> None:
    names = {'Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'Arial Unicode MS'}
    installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
    for name in ('Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'Arial Unicode MS'):
        if name in installed:
            plt.rcParams['font.sans-serif'] = [name]
            break
    plt.rcParams['axes.unicode_minus'] = False


def _pair_rows(by_map: dict[int, dict], hc_map: dict[int, dict]) -> list[tuple[int, dict, dict]]:
    return [(i, by_map[i], hc_map[i]) for i in sorted(set(by_map) & set(hc_map))]


def _make_images(pairs: list[tuple[int, dict, dict]], out_dir: Path) -> dict[str, Path]:
    _setup_mpl()
    paths: dict[str, Path] = {}
    xs = list(range(len(pairs)))
    labels = [f'cold{i:02d}' for i, _, _ in pairs]
    products = [BY, HC]
    colors = {BY: '#2563eb', HC: '#f97316'}
    offsets = {BY: -0.19, HC: 0.19}
    marks = [
        ('valid', '\u83b7\u5f97\u536b\u661f\u65f6\u95f4', '#0ea5e9'),
        ('single', '\u9996\u5355\u70b9/\u4f2a\u8ddd\u7c7b', '#3b82f6'),
        ('float', '\u9996\u6d6e\u70b9', '#f59e0b'),
        ('fixed', '\u9996\u56fa\u5b9a', '#16a34a'),
    ]

    # \u56fe1\uff1a\u540c\u4e00\u542f\u52a8\u5e8f\u53f7\u4e0b\uff0c\u5317\u4e91/\u534e\u6d4b\u5404\u4e00\u6839\u67f1\u3002
    fig, ax = plt.subplots(figsize=(max(11, 1.1 * len(pairs)), 4.8))
    for prod in products:
        off = [x + offsets[prod] for x in xs]
        ys = [hc['first']['fixed'] if prod == HC else by['first']['fixed'] for _, by, hc in pairs]
        ax.bar(off, [0.0 if y is None else float(y) for y in ys], width=0.34,
               color=colors[prod], label=prod, zorder=3)
        for x, y in zip(off, ys):
            if y is not None:
                ax.text(x, y, f'{y:.1f}s', ha='center', va='bottom', fontsize=7.5,
                        color=colors[prod])
            else:
                ax.text(x, 0, DASH, ha='center', va='bottom', fontsize=7.5, color='#64748b')
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel('\u540c\u4e00\u6b21\u51b7\u542f\u52a8\uff08\u6309\u81ea\u52a8\u5207\u5272\u6bb5\u5e8f\u53f7\u914d\u5bf9\uff09')
    ax.set_ylabel('\u81ea\u8be5\u6bb5 t=0 \u5230\u9996\u56fa\u5b9a\uff08\u79d2\uff09')
    ax.set_title(f'{BY} / {HC} \u51b7\u542f\u52a8\u9996\u56fa\u5b9a\u8017\u65f6\u5bf9\u6bd4', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', fontsize=9)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    paths['fixed'] = out_dir / 'img_\u9996\u56fa\u5b9a\u5bf9\u6bd4.png'
    fig.savefig(paths['fixed'], dpi=150)
    plt.close(fig)

    # \u56fe2\uff1a\u5317\u4e91/\u534e\u6d4b\u7528\u5f62\u72b6\u533a\u5206\uff0c\u56db\u4e2a\u91cc\u7a0b\u7891\u7528\u989c\u8272\u533a\u5206\u3002
    fig, ax = plt.subplots(figsize=(max(11, 1.1 * len(pairs)), 5.0))
    product_shapes = {BY: 'o', HC: '^'}
    for key, event_label, event_color in marks:
        for prod in products:
            xs_plot, ys_plot = [], []
            for x, (_, by, hc) in zip(xs, pairs):
                d = hc if prod == HC else by
                y = d['first'].get(key)
                if y is not None:
                    xs_plot.append(x + offsets[prod])
                    ys_plot.append(float(y))
            ax.scatter(xs_plot, ys_plot, marker=product_shapes[prod], s=48,
                       color=event_color, edgecolors='white', linewidths=0.5,
                       zorder=3, label=f'{prod}\u00b7{event_label}')
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlabel('\u540c\u4e00\u6b21\u51b7\u542f\u52a8\uff08\u6309\u81ea\u52a8\u5207\u5272\u6bb5\u5e8f\u53f7\u914d\u5bf9\uff09')
    ax.set_ylabel('\u76f8\u5bf9\u8be5\u6bb5 t=0 \u7684\u8017\u65f6\uff08\u79d2\uff09')
    ax.set_title(f'{BY} / {HC} \u5173\u952e\u91cc\u7a0b\u7891\u5bf9\u6bd4', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', ncols=4, fontsize=7.5, framealpha=0.95)
    fig.tight_layout()
    paths['keytime'] = out_dir / 'img_\u5173\u952e\u91cc\u7a0b\u7891\u5bf9\u6bd4.png'
    fig.savefig(paths['keytime'], dpi=150)
    plt.close(fig)
    return paths


def _md_table(md_lines: list[str], heading: str) -> str:
    try:
        heading_index = md_lines.index('## ' + heading) + 1
    except ValueError:
        return ''
    start = next((i for i in range(heading_index, len(md_lines)) if md_lines[i].startswith('|')), None)
    if start is None:
        return ''
    rows: list[list[str]] = []
    for line in md_lines[start:]:
        if not line.startswith('|'):
            break
        rows.append([c.strip() for c in line.strip('|').split('|')])
    if len(rows) < 3:
        return ''
    head, _, body = rows[0], rows[1], rows[2:]
    out = ['<table>', '<tr>' + ''.join(f'<th>{c}</th>' for c in head) + '</tr>']
    for r in body:
        out.append('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>')
    out.append('</table>')
    return chr(10).join(out)


def _img64(path: Path) -> str:
    import base64
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def build(by_path: Path, hc_path: Path, out_dir: Path) -> tuple[Path, Path]:
    by_rows = _load(by_path)
    hc_rows = _load(hc_path)
    by_map = _pair_index(by_rows)
    hc_map = _pair_index(hc_rows)
    pairs = _pair_rows(by_map, hc_map)
    if not pairs:
        raise SystemExit('\u5317\u4e91\u4e0e\u534e\u6d4b\u6ca1\u6709\u76f8\u540c\u7684 coldNN \u542f\u52a8\u5e8f\u53f7\uff0c\u65e0\u6cd5\u751f\u6210\u8054\u5408\u5bf9\u6bd4\u62a5\u544a')
    out_dir.mkdir(parents=True, exist_ok=True)
    imgs = _make_images(pairs, out_dir)

    by_sources = sorted({str((r.get('segment') or {}).get('source', '')) for r in by_rows})
    hc_sources = sorted({str((r.get('segment') or {}).get('source', '')) for r in hc_rows})
    TITLE = '\u5ba4\u5916\u51b7\u542f\u52a8\u8054\u5408\u5bf9\u6bd4\u62a5\u544a\uff08\u5317\u4e91 + \u534e\u6d4b\uff09'
    NOTE_TIME = '\u65f6\u95f4\u57fa\u51c6\uff1a\u5404\u4ea7\u54c1\u72ec\u7acb\u62a5\u544a\u5185\u8be5\u6bb5\u81ea\u5df1\u7684 t=0\uff1b\u672c\u62a5\u544a\u53ea\u6bd4\u8f83\u540c\u4e00\u542f\u52a8\u5e8f\u53f7\u7684\u76f8\u5bf9\u8017\u65f6\u3002'
    NOTE_COLD = '\u6ce8\u610f\uff1aGNSS \u72b6\u6001\u63a8\u65ad\u6bb5\u4e0d\u7b49\u4e8e\u5df2\u8bc1\u660e\u7684\u7269\u7406\u65ad\u7535/\u4e0a\u7535\u65f6\u523b\uff1b\u8bbe\u5907\u95f4\u542f\u52a8\u662f\u5426\u540c\u6b65\u9700\u5916\u90e8\u4e8b\u4ef6\u8bb0\u5f55\u786e\u8ba4\u3002'
    H1 = '\u9996\u56fa\u5b9a\u65f6\u95f4\u5bf9\u6bd4'
    H2 = '\u83b7\u5f97\u536b\u661f\u65f6\u95f4\u5bf9\u6bd4'
    H3 = '\u9996\u5355\u70b9/\u9996\u6d6e\u70b9\u5bf9\u6bd4'
    H4 = '\u51b7\u542f\u52a8\u8def\u5f84\u6c47\u603b\u5bf9\u6bd4'
    H5 = '\u6570\u636e\u8d28\u91cf\u6458\u8981'
    HEADER1 = '\u542f\u52a8\u5e8f\u53f7 | \u5317\u4e91\u9996\u56fa\u5b9a | \u534e\u6d4b\u9996\u56fa\u5b9a | \u534e\u6d4b-\u5317\u4e91'
    HEADER2 = '\u542f\u52a8\u5e8f\u53f7 | \u5317\u4e91\u83b7\u5f97\u65f6\u95f4 | \u534e\u6d4b\u83b7\u5f97\u65f6\u95f4 | \u534e\u6d4b-\u5317\u4e91'
    HEADER3 = '\u542f\u52a8\u5e8f\u53f7 | \u5317\u4e91\u9996\u5355\u70b9 | \u534e\u6d4b\u9996\u5355\u70b9 | \u5317\u4e91\u9996\u6d6e\u70b9 | \u534e\u6d4b\u9996\u6d6e\u70b9'
    HEADER4 = '\u542f\u52a8\u5e8f\u53f7 | \u5317\u4e91 \u65e0\u65f6\u95f4 | \u5317\u4e91 \u65f6\u95f4\u2192\u5355\u70b9 | \u5317\u4e91 \u5355\u70b9\u2192\u6d6e\u70b9 | \u5317\u4e91 \u6d6e\u70b9\u2192\u56fa\u5b9a | \u5317\u4e91 \u5355\u70b9\u2192\u56fa\u5b9a | \u534e\u6d4b \u65e0\u65f6\u95f4 | \u534e\u6d4b \u65f6\u95f4\u2192\u5355\u70b9 | \u534e\u6d4b \u5355\u70b9\u2192\u6d6e\u70b9 | \u534e\u6d4b \u6d6e\u70b9\u2192\u56fa\u5b9a | \u534e\u6d4b \u5355\u70b9\u2192\u56fa\u5b9a'
    HEADER5 = '\u4ea7\u54c1 | \u5b9a\u4f4d\u6d41 | \u6709\u6548\u6bb5\u6570 | \u9636\u6bb5\u987a\u5e8f\u5168\u90e8\u6709\u6548 | \u6821\u9a8c\u5931\u8d25\u62a5\u6587'
    NOTE_VALID = '\u534e\u6d4b\u53e3\u5f84\uff1aTime Status \u9996\u6b21\u8131\u79bb UNKNOWN/APPROXIMATE\uff1b\u5317\u4e91\u53e3\u5f84\uff1a\u9996\u6b21\u8131\u79bb UNKNOWN\u3002'
    NOTE_DIRECT = '\u201c\u5355\u70b9\u2192\u56fa\u5b9a\u201d\u8868\u793a\u8be5\u6bb5\u6ca1\u6709\u51fa\u73b0\u6d6e\u70b9\u89e3\uff0c\u76f4\u63a5\u4ece\u5355\u70b9\u7c7b\u8fdb\u5165\u56fa\u5b9a\u89e3\u3002'
    lines: list[str] = [
        '# ' + TITLE,
        '',
        f'- \u5317\u4e91\u6570\u636e\u6e90\uff1a`{by_path.parent}`\uff0c\u6709\u6548\u6bb5\u6570\uff1a{len(by_rows)}\uff0c\u5b9a\u4f4d\u6d41\uff1a{chr(12289).join(by_sources) or DASH}',
        f'- \u534e\u6d4b\u6570\u636e\u6e90\uff1a`{hc_path.parent}`\uff0c\u6709\u6548\u6bb5\u6570\uff1a{len(hc_rows)}\uff0c\u5b9a\u4f4d\u6d41\uff1a{chr(12289).join(hc_sources) or DASH}',
        f'- \u914d\u5bf9\u53e3\u5f84\uff1a\u6309\u81ea\u52a8\u5207\u5272\u6bb5\u5e8f\u53f7 `coldNN` \u4e00\u4e00\u5bf9\u5e94\uff0c\u5171 {len(pairs)} \u5bf9\u3002',
        '- ' + NOTE_TIME,
        '- ' + NOTE_COLD,
        '',
        '![\u9996\u56fa\u5b9a\u5bf9\u6bd4](img_\u9996\u56fa\u5b9a\u5bf9\u6bd4.png)',
        '',
        '## ' + H1,
        '',
        '| ' + HEADER1 + ' |',
        '|---:|---:|---:|---:|',
    ]
    for i, by, hc in pairs:
        by_fixed = by['first']['fixed']
        hc_fixed = hc['first']['fixed']
        diff = None if by_fixed is None or hc_fixed is None else round(hc_fixed - by_fixed, 1)
        lines.append(f'| cold{i:02d} | {_v(by_fixed)} | {_v(hc_fixed)} | {_v(diff)} |')
    lines += [
        '',
        f"**\u9996\u56fa\u5b9a\u7edf\u8ba1\uff08min / \u5747\u503c / max\uff09\uff1a\u5317\u4e91 {_stats([by['first']['fixed'] for _, by, _ in pairs])}\uff1b\u534e\u6d4b {_stats([hc['first']['fixed'] for _, _, hc in pairs])}\u3002**",
        '',
        '## ' + H2,
        '',
        NOTE_VALID,
        '',
        '| ' + HEADER2 + ' |',
        '|---:|---:|---:|---:|',
    ]
    for i, by, hc in pairs:
        by_valid = by['first']['valid']
        hc_valid = hc['first']['valid']
        diff = None if by_valid is None or hc_valid is None else round(hc_valid - by_valid, 1)
        lines.append(f'| cold{i:02d} | {_v(by_valid)} | {_v(hc_valid)} | {_v(diff)} |')
    lines += [
        '',
        '## ' + H3,
        '',
        '| ' + HEADER3 + ' |',
        '|---:|---:|---:|---:|---:|',
    ]
    for i, by, hc in pairs:
        bf = by['first']
        hf = hc['first']
        lines.append(f"| cold{i:02d} | {_v(bf['single'])} | {_v(hf['single'])} | {_v(bf['float'])} | {_v(hf['float'])} |")
    lines += [
        '',
        '## ' + H4,
        '',
        NOTE_DIRECT,
        '',
        '| ' + HEADER4 + ' |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for i, by, hc in pairs:
        bs = by.get('stages_express', by['stages'])
        hs = hc.get('stages_express', hc['stages'])
        lines.append(
            f"| cold{i:02d} | {_v(bs['no_time'])} | {_v(bs['time_to_single'])} | {_v(bs['single_to_float'])} | {_v(bs['float_to_fixed'])} | {_v(bs['single_to_fixed'])} "
            f"| {_v(hs['no_time'])} | {_v(hs['time_to_single'])} | {_v(hs['single_to_float'])} | {_v(hs['float_to_fixed'])} | {_v(hs['single_to_fixed'])} |"
        )
    lines += [
        '',
        '## ' + H5,
        '',
        '| ' + HEADER5 + ' |',
        '|---|---|---:|---|---:|',
    ]
    for name, rows, sources in [(BY, by_rows, by_sources), (HC, hc_rows, hc_sources)]:
        bad = sum(int(r.get('recovery', {}).get('checksum_failed', r.get('crc_bad', 0))) for r in rows)
        ok = all(r.get('stage_order_valid', False) for r in rows)
        ok_text = '\u662f' if ok else '\u5426'
        lines.append(f'| {name} | {chr(47).join(sources) or DASH} | {len(rows)} | {ok_text} | {bad} |')

    md = chr(10).join(lines) + chr(10)
    css = '''body{font-family:"Microsoft YaHei",sans-serif;max-width:1400px;margin:24px auto;padding:0 16px;color:#172033;background:#f8fafc}
h1,h2{color:#173b5c} .card{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:14px;margin:14px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;background:white;font-size:13px}
th,td{border:1px solid #cbd5e1;padding:7px 9px;text-align:right} th{background:#e2e8f0} td:first-child,th:first-child{text-align:left}
.note{background:#eef6ff;border-left:4px solid #2563eb;padding:10px 14px;margin:14px 0;font-size:13px}
.imgcard{background:white;border:1px solid #cbd5e1;border-radius:8px;padding:10px;margin:14px 0;text-align:center}
img{max-width:100%;height:auto}'''
    NOTE_HTML = '\u672c\u62a5\u544a\u7531\u4e24\u4e2a\u4ea7\u54c1\u72ec\u7acb\u62a5\u544a\u7684 summary.json \u81ea\u52a8\u751f\u6210\uff1b\u6309 coldNN \u6bb5\u5e8f\u53f7\u914d\u5bf9\u3002GNSS \u72b6\u6001\u63a8\u65ad\u6bb5\u4e0d\u7b49\u4e8e\u7269\u7406\u65ad\u7535/\u4e0a\u7535\u65f6\u523b\uff1b\u5404\u6bb5 t=0 \u4e3a\u8be5\u4ea7\u54c1\u8be5\u6bb5\u65e5\u5fd7\u8d77\u70b9\uff0c\u4e0d\u662f\u8de8\u8bbe\u5907\u7edf\u4e00\u7269\u7406\u4e0a\u7535\u65f6\u523b\u3002'
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{TITLE}</title>
<style>{css}</style></head><body><div class="wrap">
<h1>{TITLE}</h1>
<div class="note">{NOTE_HTML}</div>
<div class="imgcard"><img src="{_img64(imgs['fixed'])}" alt="\u9996\u56fa\u5b9a\u5bf9\u6bd4"></div>
<h2>\u4e00\u3001{H1}</h2>
<div class="card">{_md_table(lines, H1)}</div>
<div class="imgcard"><img src="{_img64(imgs['keytime'])}" alt="\u5173\u952e\u91cc\u7a0b\u7891\u5bf9\u6bd4"></div>
<h2>\u4e8c\u3001\u5173\u952e\u91cc\u7a0b\u7891\u660e\u7ec6</h2>
<div class="card">{_md_table(lines, H2)}</div>
<div class="card">{_md_table(lines, H3)}</div>
<h2>\u4e09\u3001{H4}</h2>
<div class="card">{_md_table(lines, H4)}</div>
<h2>\u56db\u3001{H5}</h2>
<div class="card">{_md_table(lines, H5)}</div>
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
    parser.add_argument('hc_summary', type=Path)
    parser.add_argument('out_dir', type=Path)
    args = parser.parse_args()
    html_path, md_path = build(args.by_summary, args.hc_summary, args.out_dir)
    print('\u751f\u6210\u5bf9\u6bd4 HTML:', html_path)
    print('\u751f\u6210\u5bf9\u6bd4 MD  :', md_path)


if __name__ == '__main__':
    main()
