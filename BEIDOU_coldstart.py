# -*- coding: utf-8 -*-
"""
北斗（UM980 / 和芯星通 Nebulas-IV N4）GNSS 冷启动策略分析
=====================================================================
与北云 BY_coldstart.py、华测 HUACE_coldstart.py 同构：指标定义、分析口径、
报告结构一致，仅报文名、报头/数据段字段与枚举值按 UM980（N4 协议）适配。

分析对象：由 HMI 或命令行传入的 UM980 原始日志（混合二进制+ASCII）

口径依据（和芯星通《N4 Products Commands and Logs Reference Book R1.15》）：
  * 主定位流     #BESTNAVA（7.3.19 BESTNAV，Message ID 2118，表 7-70）
  * ASCII 报头   表 7-51：CPUIDle / TimeRef / TimeStatus / Wn / Ms(周内毫秒) /
                 Version / Reserved / 闰秒 / 输出延迟；
                 GPS 时间 = Wn × 604800 + Ms/1000
  * 时标状态      表 7-51 字段 5：仅 UNKNOWN / FINE 两态，
                  UNKNOWN 表明接收机尚未计算出准确的 GPS 时间
  * 解算状态      表 0-6：SOL_COMPUTED=已解出；INSUFFICIENT_OBS=观测数据不足；
                  NO_CONVERGENCE=无法收敛；COV_TRACE=协方差迹超限；
                  另实测出现 VARIANCE（方差超限）
  * 定位类型      表 0-5：NONE=无解；SINGLE=单点；PSRDIFF=伪距差分；SBAS；
                  NARROW_FLOAT=窄巷浮点解；NARROW_INT=窄巷固定解；
                  L1_FLOAT / IONOFREE_FLOAT / L1_INT / WIDE_INT 等
  * 卫星字段      表 7-70 字段 15/16：#SVs=跟踪卫星数，#solnSVs=解算卫星数。
                  表 7-70 无 #solnMultiSVs（字段 17-19 为 Reserved），
                  多频解算维度不适用，一律输出“—”，不解析保留字段。
  * 差分可用      表 7-70 字段 12：Stn ID 非空且 != "0"（缺省 0，实测如 "1793"）
  * CRC           "#" 开头 = 32 位 CRC（表 7-70 字段 32）；"$" 开头 = NMEA 异或

与北云/华测的两个关键差异（用户确认口径，2026-10-23）：
  1. 断电后时标可由 UM980 内部电池保持（FINE/真实周），冷启动以解算状态
     失效（INSUFFICIENT_OBS 等 + NONE/单点 + 无基站）判定，时标不作必要条件；
  2. 时标未丢失的段（段首已持有效时标），“获得卫星时间 / 时标进入 FINE”
     显示“—”，不参与对比。

输出（由 HMI 指定输出目录，或 CLI 指定）：
  report.html / report.md / img_关键时刻.png / img_阶段耗时.png / img_卫星数量时间轴.png / summary.json
运行：
  HMI：python gnss_coldstart_hmi.py（产品选“北斗 UM980”）
  CLI：python BEIDOU_coldstart.py <输出目录> [--split] <日志1> [日志2 ...]
"""
import os
import sys
import io
import json
import base64
import collections
import statistics
from pathlib import Path
import webbrowser

from coldstart_split import split_beidou, split_beiyun
from gnss_recovery import (RecoveryResult, recover_ascii_messages,
                           parse_beidou_positions)

# Paired UM980/BeiYun matching tolerances.  These are engineering thresholds
# for this synchronized cold-start workflow, not vendor protocol constants.
PAIRED_MATCH_S = 30.0
PAIRED_MATCH_EXTENDED_S = 90.0

# ----------------------------------------------------------------------
# 路径与常量（相对脚本所在目录解析，便于迁移）
# ----------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, 'reports')            # 默认输出目录
TAG = '北斗GNSS冷启动分析报告'
MANUAL = '和芯星通《N4 Products Commands and Logs Reference Book R1.15》'

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 定位类型分组（N4 表 0-5）：1=单点类 / 2=浮点类 / 3=固定类，其余归 0=无解
CAT1 = {'SINGLE', 'PSRDIFF', 'SBAS', 'INS_PSRSP', 'INS_PSRDIFF', 'INS_SBAS'}
CAT2 = {'L1_FLOAT', 'IONOFREE_FLOAT', 'NARROW_FLOAT', 'INS_RTKFLOAT',
        'PPP_CONVERGING'}
CAT3 = {'L1_INT', 'WIDE_INT', 'NARROW_INT', 'INS_RTKFIXED',
        'PPP', 'PPP_AR', 'PPP_RTK'}
TS_CODE = {'UNKNOWN': 0, 'FINE': 2}
SOL_ORDER = ['INSUFFICIENT_OBS', 'NO_CONVERGENCE', 'COV_TRACE', 'VARIANCE',
             'SOL_COMPUTED']
PT_CN = {'NONE': '无解', 'SINGLE': '单点', 'PSRDIFF': '伪距差分', 'SBAS': 'SBAS',
         'NARROW_FLOAT': 'RTK浮点', 'NARROW_INT': 'RTK固定'}
CAT_CN = ['无解', '单点', 'RTK浮点', 'RTK固定']

# ----------------------------------------------------------------------
# 行级辅助（与 BY/HUACE 一致）


def build_elapsed(times, nominal=None):
    """由报头时间构造经过时间轴（t=0 起于首条记录）。周期不假设，按时间戳自判：
    标称周期 = 全部帧间隔的中位数；|dt|>3600s 判定为冷启动 GPS 周重定标（默认周->真实周，
    按时间连续处理，间隔按标称周期计）；nominal*2.5 < dt <= 3600s 记为真实间断（保留在时间轴中）。"""
    if nominal is None:
        dts = sorted(times[k] - times[k - 1] for k in range(1, len(times)))
        nominal = dts[len(dts) // 2] if dts else 0.2
    el = [0.0]
    gaps, rebase = [], []
    for k in range(1, len(times)):
        dt = times[k] - times[k - 1]
        if abs(dt) > 3600.0:
            el.append(el[-1] + nominal)
            rebase.append(dict(idx=k, elapsed=round(el[-1], 1), dt=round(dt, 1)))
        else:
            if dt > nominal * 2.5:
                gaps.append(dict(elapsed=round(el[-1], 1), dt=round(dt, 2)))
            # Preserve the receiver timestamp axis. Positive dt is used directly;
            # non-positive/duplicate timestamps are collapsed by one nominal period.
            el.append(el[-1] + (dt if dt > 0 else nominal))
    return el, gaps, rebase, nominal


def runs(el, labels):
    """状态分段 -> [(label, t_start, t_end, count)]（端点时刻口径）。"""
    out, i, n = [], 0, len(labels)
    while i < n:
        j = i
        while j + 1 < n and labels[j + 1] == labels[i]:
            j += 1
        out.append((labels[i], round(el[i], 1), round(el[j], 1), j - i + 1))
        i = j + 1
    return out


# ----------------------------------------------------------------------
# 单文件分析


def first_idx(pred, seq):
    for i, v in enumerate(seq):
        if pred(v):
            return i
    return None


def _mean(vals):
    """均值；全为 None（如多频解算不适用）时返回 None。"""
    xs = [v for v in vals if v is not None]
    return round(statistics.mean(xs), 1) if xs else None


def build(path):
    """分析一份日志文件，返回全部指标。"""
    with open(path, 'rb') as fp:
        return build_raw(fp.read(), os.path.basename(path))


def build_raw(raw, name='<segment>', byte_offset=0, t0_shift=0.0, frames=None, recovery=None):
    """分析一段原始字节流或已恢复的定位帧序列。

    UM980 日志为混合二进制+ASCII。解析按字节锚点恢复完整报文并按 32 位 CRC
    校验；定位流取 #BESTNAVA（表 7-70）。多频解算字段不存在（表 7-70 无
    #solnMultiSVs），multi 恒为 None，报告输出“—”。
    """
    rec_messages: list = []
    if not frames:
        recovery = recover_ascii_messages(raw)
        rec_messages = recovery.messages
        frames = parse_beidou_positions(rec_messages)
    elif recovery is not None:
        rec_messages = recovery.messages
    if recovery is None:
        recovery = RecoveryResult(messages=[], bad_offsets=[])
    counts = collections.Counter(m.name for m in rec_messages if m.kind == 'hash')
    other = collections.Counter(m.name for m in rec_messages if m.kind == 'nmea')
    bad = recovery.bad

    rows, tl = [], []
    for pf in frames:
        rows.append(dict(ts=pf.ts, sol=pf.sol, pt=pf.pt,
                         lstd=0.0, stn=pf.stn, age=pf.age,
                         svs=pf.svs, soln=pf.soln, multi=pf.multi))
        tl.append(pf.t)
    if not rows:
        raise ValueError('未恢复到任何有效北斗 BESTNAVA 定位帧（%s）：'
                         '请确认该文件/片段是否为 UM980 日志，且含 BESTNAVA 报文' % name)
    # 时标是否丢失以本段首帧判定（室内 t0_shift 裁剪之前）：段首已持有效时标
    # = 断电后时标由内部电池保持，“获得卫星时间”不适用，输出“—”。
    time_retained = rows[0]['ts'] != 'UNKNOWN'
    el, gaps, reb, nominal = build_elapsed(tl)
    if t0_shift > 0:
        el = [x - t0_shift for x in el]
        keep = [k for k in range(len(rows)) if el[k] >= -1e-9]
        rows = [rows[k] for k in keep]
        el = [el[k] for k in keep]
        base = el[0] if el else 0.0
        el = [x - base for x in el]
        gaps = [g for g in gaps if g['elapsed'] - t0_shift - base >= 0]
        reb = [r for r in reb if r['elapsed'] - t0_shift - base >= 0]
        tl = tl[len(tl) - len(rows):] if rows else tl[:0]
    pts = [r['pt'] for r in rows]
    cats = [3 if p in CAT3 else 2 if p in CAT2 else 1 if p in CAT1 else 0 for p in pts]

    if time_retained:
        valid_idx = None
        fine_idx = None
    else:
        valid_idx = first_idx(lambda r: r['ts'] != 'UNKNOWN', rows)
        fine_idx = first_idx(lambda r: r['ts'] == 'FINE', rows)
    f_idx = dict(
        valid=valid_idx,
        coarse=None,   # N4 Time Status 仅 UNKNOWN / FINE 两态（表 7-51）
        fine=fine_idx,
        single=first_idx(lambda p: p in CAT1, pts),
        float=first_idx(lambda p: p in CAT2, pts),
        fixed=first_idx(lambda p: p in CAT3, pts),
        stn=first_idx(lambda r: r['stn'] not in ('', '0'), rows),
        sol=first_idx(lambda r: r['sol'] == 'SOL_COMPUTED', rows),
    )
    f = {k: (None if v is None else round(el[v], 1)) for k, v in f_idx.items()}

    def dur(a, b):
        return None if (a is None or b is None) else round(b - a, 1)

    stages = dict(no_time=f['valid'],
                  time_to_single=(None if f['single'] is None else round(max(0.0, f['single'] - (f['valid'] or 0.0)), 1)),
                  single_to_float=dur(f['single'], f['float']),
                  float_to_fixed=dur(f['float'], f['fixed']),
                  diff_wait=dur(f['valid'], f['stn']),
                  single_to_fixed=dur(f['single'], f['fixed']))
    order_valid = all(v is None or v >= 0 for k, v in stages.items() if k != 'no_time')
    if f['single'] is not None and f['fixed'] is not None:
        order_valid = order_valid and f['single'] <= f['fixed']
    if f['float'] is not None and f['fixed'] is not None:
        order_valid = order_valid and f['float'] <= f['fixed']

    events = []
    if f['valid'] is not None:
        events.append([f['valid'], '获得卫星时间（时标脱离 UNKNOWN）', '#0ea5e9'])
    if f['stn'] is not None:
        i_stn = f_idx['stn']
        events.append([f['stn'], f"差分改正可用（基站 {rows[i_stn]['stn']}）", '#7c3aed'])
    if f['single'] is not None:
        events.append([f['single'], '首次单点/伪距差分解', '#3b82f6'])
    if f['fine'] is not None and f['fine'] != f['valid']:
        events.append([f['fine'], '时标进入 FINE', '#6b7280'])
    if f['float'] is not None:
        events.append([f['float'], '首次 RTK 浮点解', '#f59e0b'])
    if f['fixed'] is not None:
        events.append([f['fixed'], '首次 RTK 固定解', '#16a34a'])
    for r in reb:
        events.append([r['elapsed'], 'GPS 时间轴跳变（按连续处理）', '#dc2626'])
    events.sort()

    segs = []
    for lab, a, b, c in runs(el, pts):
        idx = [k for k in range(len(rows)) if a - 1e-6 <= el[k] <= b + 1e-6]
        sub = [rows[k] for k in idx]
        seg_duration = round(c * nominal, 1)
        if sub:
            segs.append([lab, a, b, seg_duration, c,
                         _mean([r['svs'] for r in sub]),
                         _mean([r['soln'] for r in sub]),
                         _mean([r['multi'] for r in sub])])
        else:
            segs.append([lab, a, b, seg_duration, c, 0, 0, None])

    return dict(
        file=name, byte_offset=byte_offset, span=round(el[-1], 1), n=len(rows),
        rate=round(1.0 / max(nominal, 1e-9), 1), nominal=round(nominal, 3), crc_bad=bad,
        counts=dict(counts), other=dict(other),
        recovery=dict(valid_messages=len(rec_messages), embedded=recovery.embedded,
                      checksum_failed=recovery.bad, method='byte-anchor+checksum'),
        gaps=gaps, rebases=reb,
        time_status_retained=time_retained,
        tstat_dist=dict(collections.Counter(r['ts'] for r in rows)),
        ptype_dist=dict(collections.Counter(pts)),
        cat_pct={str(k): round(100 * v / len(cats), 1) for k, v in collections.Counter(cats).items()},
        first=f, stages=stages, stage_order_valid=order_valid, events=events, segs=segs,
        stages_express=dict(
            no_time=stages['no_time'],
            time_to_single=stages['time_to_single'],
            single_to_float=stages['single_to_float'],
            float_to_fixed=stages['float_to_fixed'],
            single_to_fixed=(None if f['float'] is not None and f['fixed'] is not None
                             else stages['single_to_fixed']),
        ),
        svs=dict(min=min(r['svs'] for r in rows), max=max(r['svs'] for r in rows),
                 mean=round(statistics.mean([r['svs'] for r in rows]), 1)),
        series=dict(el=[round(x, 1) for x in el], svs=[r['svs'] for r in rows],
                    soln=[r['soln'] for r in rows], multi=[r['multi'] for r in rows],
                    cats=cats,
                    tstat=[TS_CODE.get(r['ts'], 0) for r in rows],
                    sol=[SOL_ORDER.index(r['sol']) if r['sol'] in SOL_ORDER else 3 for r in rows]),
    )


def short_label(name, width=16):
    """文件名缩短：保留主体，超长按 width 折行（最多两行），用于图左轴标签。"""
    base = name.replace('.log', '')
    if len(base) <= width:
        return base
    return base[:width] + '\n' + base[width:width * 2 - 3] + '…'


def seg_of(d, lab):
    for s in d['segs']:
        if s[0] == lab:
            return s
    return None

# ----------------------------------------------------------------------
# 图片生成（matplotlib，PNG，嵌入 HTML 引用）
# ----------------------------------------------------------------------


def setup_mpl():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for fam in ('Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'WenQuanYi Zen Hei'):
        try:
            from matplotlib import font_manager
            if any(fam.lower() in f.name.lower() for f in font_manager.fontManager.ttflist):
                plt.rcParams['font.sans-serif'] = [fam] + plt.rcParams.get('font.sans-serif', [])
                break
        except Exception:
            pass
    plt.rcParams['axes.unicode_minus'] = False
    return plt


def sort_for_report(data):
    """报告排序：同一次启动相邻（北云01、华测01、北斗01、北云02……）。
    单产品退化为按文件名排序。键 = (启动序号, 产品序)。"""
    def key(d):
        seg = d.get('segment') or {}
        idx = seg.get('index', 0)
        prod = 0 if '北云' in d['file'] else (1 if '华测' in d['file'] else (2 if '北斗' in d['file'] else 3))
        return (idx, prod, d['file'])
    # 若所有段都无 segment.index，则退回纯文件名排序（单产品兼容）
    if all(not (d.get('segment') or {}).get('index') for d in data):
        return sorted(data, key=lambda d: d['file'])
    return sorted(data, key=key)


def make_images(data, out_dir, title_prefix=None):
    plt = setup_mpl()
    paths = {}

    # 图1：四个关键时刻（按文件名排序，每个文件一根）
    order = sort_for_report(data)
    labels = [short_label(d['file'], 13) for d in order]
    fig, ax = plt.subplots(figsize=(11, 4.6))
    xs = range(len(order))
    marks = [('valid', '获得卫星时间', '#0ea5e9', 'o'),
             ('single', '首单点', '#3b82f6', 's'),
             ('float', '首浮点', '#f59e0b', '^'),
             ('fixed', '首固定', '#16a34a', 'D')]
    for key, lab, col, mk in marks:
        ys = [d['first'][key] if d['first'][key] is not None else float('nan') for d in order]
        ax.scatter(list(xs), ys, marker=mk, s=68, color=col, label=lab, zorder=3)
        for x, y in zip(xs, ys):
            if y == y:  # not NaN
                ax.annotate(f'{y:.1f}s', (x, y), textcoords='offset points', xytext=(6, 4),
                            fontsize=8.5, color=col)
    ytop = max(d['first']['fixed'] or 0 for d in order) * 1.22
    ax.set_ylim(top=ytop)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=8, rotation=14, ha='right')
    ax.set_ylabel('时刻（秒）')
    ax.set_title((title_prefix or '北斗') + '冷启动关键时刻对比（t=0 为日志起点）', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', ncols=4, fontsize=9, framealpha=0.95)
    fig.tight_layout()
    paths['keytime'] = os.path.join(out_dir, 'img_关键时刻.png')
    fig.savefig(paths['keytime'], dpi=150)
    plt.close(fig)

    # 图2：冷启动耗时分解（堆叠条形，每个文件一根）
    fig, ax = plt.subplots(figsize=(14.5, 5.6))
    stage_colors = {'无时间': '#dc2626', '时间→单点': '#f59e0b',
                    '单点→浮点': '#3b82f6', '浮点→固定': '#16a34a',
                    '单点→固定': '#16a34a'}
    for i, d in enumerate(order):
        st = d.get('stages_express', d['stages'])
        seg = [('无时间', st['no_time'] or 0), ('时间→单点', st['time_to_single'] or 0)]
        if st['single_to_float'] is not None:
            seg.append(('单点→浮点', st['single_to_float']))
        if st['float_to_fixed'] is not None:
            seg.append(('浮点→固定', st['float_to_fixed']))
        elif st.get('single_to_fixed') is not None:
            seg.append(('单点→固定', st['single_to_fixed']))
        left = 0.0
        for lab, v in seg:
            if v > 0:
                col = stage_colors[lab]
                ax.barh(i, v, left=left, color=col, edgecolor='white', height=0.55)
                if v >= 14:
                    ax.text(left + v / 2, i, f'{lab} {v:.1f}s', ha='center', va='center',
                            color='white', fontsize=8.5)
                else:
                    ax.text(left + v / 2, i - 0.42, f'{lab} {v:.1f}s', ha='center', va='bottom',
                            color=col, fontsize=7.2)
                left += v
        if d['first']['fixed'] is not None:
            ax.text(left + 3, i, f"首固定 {d['first']['fixed']:.1f}s", va='center', fontsize=9,
                    color='#111827')
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel('时间（秒）')
    ax.set_title((title_prefix or '北斗') +
                 '冷启动耗时分解（无时间 / 时间→单点 / 单点→浮点或单点→固定）', fontsize=13)
    ax.grid(axis='x', ls='--', alpha=0.4)
    legend_items = [('无时间', '#dc2626'), ('时间→单点', '#f59e0b'),
                    ('单点→浮点', '#3b82f6'), ('浮点/直达固定', '#16a34a')]
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, c in legend_items]
    ax.legend(handles, [n for n, _ in legend_items], loc='lower right', ncols=4, fontsize=9)
    fig.tight_layout()
    fig.subplots_adjust(left=0.16)
    paths['stages'] = os.path.join(out_dir, 'img_阶段耗时.png')
    fig.savefig(paths['stages'], dpi=150)
    plt.close(fig)

    # 图3：卫星数量时间轴（每个选中的文件一张子图，含关键事件虚线）
    ncol = 1
    nrow = len(data)
    fig, axes = plt.subplots(nrow, ncol, figsize=(14, 5.4 * nrow), squeeze=False)
    for idx, d in enumerate(sort_for_report(data)):
        ax = axes[idx // ncol][idx % ncol]
        el = d['series']['el']
        ax.plot(el, d['series']['svs'], color='#2563eb', lw=1.4, label='跟踪卫星数 #SVs', drawstyle='steps-post')
        ax.plot(el, d['series']['soln'], color='#16a34a', lw=1.2, label='参与解算 #solnSVs', drawstyle='steps-post')
        # BESTNAV（表 7-70）无 #solnMultiSVs，多频曲线不适用，不绘制
        if any(v is not None for v in d['series']['multi']):
            ax.plot(el, d['series']['multi'], color='#9333ea', lw=1.2, label='多频解算 #solnMultiSVs', drawstyle='steps-post')
        ymax = max(d['series']['svs']) + 2
        ax.set_ylim(0, ymax * 1.42)
        prev_x = -1e9
        prev_slot = -1
        # 事件密集时按时间桶错层：同一 45s 窗口内的事件逐层下移
        for ei, (t, txt, col) in enumerate(d['events']):
            ax.axvline(t, color=col, ls='--', lw=1)
            slot = (prev_slot + 1) % 6 if t - prev_x < 50 else ei % 6
            prev_x = t
            prev_slot = slot
            ay = ymax * (1.30 - 0.062 * slot)
            ax.annotate(txt, (t, ay), textcoords='offset points', xytext=(32, 0), fontsize=6.5,
                        color=col, va='top', annotation_clip=False)
            if ay > ymax:
                ax.annotate('', xy=(t, ymax), xytext=(t, ay - ymax*0.02),
                            arrowprops=dict(arrowstyle='-', color=col, lw=0.6, ls=':'),
                            annotation_clip=False)
        ax.set_title(f"{d['file']}（冷启动，全程 {d['span']}s）", fontsize=10.5)
        ax.set_xlabel('经过时间（秒）')
        ax.set_ylabel('卫星数（颗）')
        ax.grid(ls='--', alpha=0.35)
        ax.legend(loc='lower right', fontsize=8.5)
    for k in range(len(data), nrow * ncol):
        axes[k // ncol][k % ncol].axis('off')
    fig.tight_layout()
    paths['svs'] = os.path.join(out_dir, 'img_卫星数量时间轴.png')
    fig.savefig(paths['svs'], dpi=150)
    plt.close(fig)
    return paths


def img64(path):
    return 'data:image/png;base64,' + base64.b64encode(open(path, 'rb').read()).decode('ascii')


def V(v, u='s'):
    return '—' if v is None else f'{v}{u}'


def MV(v):
    """多频解算单元格：表 7-70 无该字段，恒为 None -> '—'。"""
    return '—' if v is None else str(v)


# ----------------------------------------------------------------------
# HTML 模板（与北云/华测同构的五节结构）
# ----------------------------------------------------------------------

CSS = """
 *{box-sizing:border-box}
 body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;margin:0;background:#f5f7fa;color:#1f2937}
 .wrap{max-width:1360px;margin:0 auto;padding:24px}
 h1{font-size:22px;margin:0 0 4px}
 h2{font-size:16px;margin:28px 0 10px;color:#111827;border-left:4px solid #2563eb;padding-left:8px}
 h3{font-size:14px;margin:16px 0 6px}
 .sub{color:#6b7280;font-size:13px;margin-bottom:14px}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin-bottom:14px}
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th,td{border:1px solid #e5e7eb;padding:5px 7px;text-align:left}
 th{background:#f3f4f6;font-weight:600}
 .mono{font-family:Consolas,monospace;font-size:12px}
 .chart-card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:12px}
 .chart-title{font-size:14px;font-weight:600;margin:0 0 6px 2px}
 canvas{display:block;width:100%}
 .note{font-size:12px;color:#6b7280;margin-top:6px}
 .tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11.5px;color:#fff}
 .t0{background:#9ca3af}.t1{background:#3b82f6}.t2{background:#f59e0b}.t3{background:#16a34a}
 .bar{display:flex;height:26px;border-radius:4px;overflow:hidden;margin:4px 0 2px;font-size:11px;color:#fff}
 .bar div{display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden}
 .barlab{font-size:11.5px;color:#6b7280;margin-bottom:10px}
 .imgcard{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:12px}
 .imgcard img{width:100%;display:block}
 ul.conc{font-size:13px;line-height:1.95;margin:0;padding-left:18px}
"""



HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>北斗GNSS冷启动分析报告</title>
<style>__CSS__</style>
</head>
<body><div class="wrap">
<h1>北斗（UM980）GNSS 冷启动策略分析</h1>
<div class="sub">数据源：__SRCLIST__（混合二进制+ASCII 日志，#BESTNAVA）｜口径依据：__MANUAL__｜分析维度：时标状态 / 解算状态 / 定位类型 / 卫星数｜本报告不含 INS</div>

<h2>一、数据完整性与报文构成</h2>
<div class="card"><table>
<tr><th>文件</th><th>时长</th><th>定位报文</th><th>实测周期</th><th>时标分布(条)</th><th>定位类型分布(条)</th><th>间断/重定标</th></tr>
__OVERVIEW__
</table>
<div class="note">口径（N4 手册）：报文按行做 32 位 CRC 校验（"#" 开头为 32 位 CRC，"$" 开头为 NMEA 异或）；报头结构见表 7-51，BESTNAV 数据结构见表 7-70。卫星维度口径：#SVs=跟踪卫星数、#solnSVs=解算卫星数（表 7-70 字段 15/16）；表 7-70 无 #solnMultiSVs（字段 17-19 为 Reserved），多频解算维度不适用，输出“—”，不解析保留字段。</div></div>


<h3>报文完整性与周期（按报头时间戳逐帧判定）</h3>
<div class="card"><table>
<tr><th>文件</th><th>帧数</th><th>实测周期</th><th>标称频率</th><th>CRC失败行</th><th>真实间断</th><th>周重定标</th></tr>
__INTEG__
</table>
<div class="note">实测周期 = 全部相邻帧报头时间差的中位数（不假设）；真实间断 = 相邻帧时间差 &gt; 2.5×实测周期（保留在时间轴中）；周重定标 = 相邻帧时间差 &gt; 3600s（按时间连续处理，间隔按实测周期计）。UM980 冷启动初期可能输出默认 GPS 周（实测周 1），解出导航电文后跳回真实周。</div></div>

<h2>二、四个关键时刻（秒）</h2>
<div class="card"><table>
<tr><th>文件</th><th>获得卫星时间<br>(脱离UNKNOWN)</th><th>时标进入 FINE</th><th>差分改正可用</th><th>首单点</th><th>首浮点</th><th>首固定</th><th>无解/单点/浮点/固定 占比%</th><th>跟踪星<br>min/均值/max</th></tr>
__KEYTIME__
</table>
<div class="note">“—”规则：若该次启动段首已持有效时标（断电后 GPS 时间由 UM980 内部电池保持，Time Status 未丢失），“获得卫星时间 / 时标进入 FINE”输出“—”，不参与对比。N4 时标状态仅 UNKNOWN / FINE 两态（表 7-51），无 COARSE 状态。</div></div>
<div class="imgcard"><img src="__IMG1__" alt="关键时刻对比"></div>

<h2>三、冷启动耗时分解</h2>
__STAGEBAR__
<div class="note">"无时间"= 0 到时标脱离 UNKNOWN（时标未丢失的段为 0）；"时间→单点"= 有卫星时间到第一个单点解（时标未丢失段自启动起算，即星历捕获时间）；"单点→浮点"= 到第一个 RTK 浮点解；"浮点→固定"= 到第一个 RTK 固定解。条宽按时长比例。</div>
<div class="imgcard"><img src="__IMG2__" alt="冷启动耗时分解"></div>

<h2>四、北斗-北云配对审计</h2>
__PAIRAUDIT__
<h2>五、定位类型分段统计</h2>
__SAMP__
<div class="imgcard"><img src="__IMG3__" alt="卫星数量时间轴"></div>

<div class="note" style="margin:18px 0 30px">时间轴为录制经过时间（t=0 为各日志首条记录）；若出现冷启动 GPS 周重定标（相邻两条报头时间差 &gt;3600s），按时间连续处理（间隔按标称周期计）。各字段口径与手册条款对应关系见 MD 报告附录。</div>
</div>
</body></html>
"""

# ----------------------------------------------------------------------
# MD / HTML 报告组装
# ----------------------------------------------------------------------


def tag_of(p):
    return {'t0': 't0', 't1': 't1', 't2': 't2', 't3': 't3'}.get(p, 't0' if p == 'NONE' else 't1' if p == 'SINGLE' else 't2' if 'FLOAT' in str(p) else 't3')


def build_outputs(data, imgs, src_desc=None):
    # ---------- 表格内容（HTML 与 MD 共用一套数值） ----------
    ov_html = []
    for d in data:
        ov_html.append(
            f"<tr><td class='mono'>{d['file']}</td><td>{d['span']}s</td>"
            f"<td>{(d.get('segment') or {}).get('source', 'BESTNAVA')} {d['n']}条</td>"
            f"<td>{d['nominal']}s/{d['rate']}Hz</td>"
            f"<td class='mono'>{d['tstat_dist']}</td><td class='mono'>{d['ptype_dist']}</td>"
            f"<td>{len(d['gaps'])} / {len(d['rebases'])}</td></tr>")
    overview = ''.join(ov_html)

    integ_rows = []
    for d in data:
        integ_rows.append(
            f"<tr><td class='mono'>{d['file']}</td><td>{d['n']}</td>"
            f"<td>{d['nominal']}s</td><td>{d['rate']}Hz</td><td>{d['crc_bad']}</td>"
            f"<td>{len(d['gaps'])}</td><td>{len(d['rebases'])}</td></tr>")
    integ = ''.join(integ_rows)

    kt_html = []
    for d in data:
        f = d['first']
        pc = d['cat_pct']
        kt_html.append(
            f"<tr><td class='mono'>{d['file']}</td><td><b>{V(f['valid'])}</b></td>"
            f"<td>{V(f['fine'])}</td>"
            f"<td>{V(f['stn'])}</td><td>{V(f['single'])}</td><td>{V(f['float'])}</td>"
            f"<td><b>{V(f['fixed'])}</b></td>"
            f"<td>{pc.get('0', 0)}/{pc.get('1', 0)}/{pc.get('2', 0)}/{pc.get('3', 0)}</td>"
            f"<td>{d['svs']['min']}/{d['svs']['mean']}/{d['svs']['max']}</td></tr>")
    keytime = ''.join(kt_html)

    STAGE_COLORS = ['#dc2626', '#f59e0b', '#3b82f6', '#16a34a']
    bars = []
    for d in data:
        st = d.get('stages_express', d['stages'])
        # 阶段条必须表示完整路径：缺失浮点时，用单点→固定补足。
        seg = [('无时间', st['no_time'] or 0), ('时间→单点', st['time_to_single'] or 0)]
        if st['single_to_float'] is not None:
            seg.append(('单点→浮点', st['single_to_float']))
        if st['float_to_fixed'] is not None:
            seg.append(('浮点→固定', st['float_to_fixed']))
        elif st.get('single_to_fixed') is not None:
            seg.append(('单点→固定', st['single_to_fixed']))
        tot = sum(v for _, v in seg) or 1
        color_map = {'无时间': STAGE_COLORS[0], '时间→单点': STAGE_COLORS[1],
                     '单点→浮点': STAGE_COLORS[2], '浮点→固定': STAGE_COLORS[3],
                     '单点→固定': STAGE_COLORS[3]}
        parts = ''.join(f"<div style='width:{v/tot*100:.2f}%;background:{color_map[n]}'>{n} {v}s</div>"
                        for n, v in seg if v > 0)
        remain = round(d['span'] - (d['first']['fixed'] if d['first']['fixed'] is not None else d['span']), 1)
        tail = f"｜首固定后剩余 {remain}s" if d['first']['fixed'] is not None else "｜全程未固定"
        bars.append(f"<h3>{d['file']}</h3><div class='bar'>{parts}</div>"
                    f"<div class='barlab'>合计到首固定 {round(tot, 1)}s（全程 {d['span']}s）{tail}"
                    f"；差分数据可用 = {V(d['first']['stn'])}</div>")
    stagebar = ''.join(bars)

    samp_html = []
    for d in data:
        seg_rows = ['<tr><th>定位类型段</th><th>起–止(s)</th><th>时长(s)</th><th>条数</th>'
                    '<th>跟踪星</th><th>解算星</th><th>多频解算</th></tr>']
        for lab, a, b, dur, c, svs, soln, multi in d['segs']:
            seg_rows.append(f"<tr><td><span class='tag {tag_of(lab)}'>{lab}</span></td>"
                            f"<td class='mono'>{a}–{b}</td><td>{dur}</td><td>{c}</td>"
                            f"<td>{svs}</td><td>{soln}</td><td>{MV(multi)}</td></tr>")
        samp_html.append(f"<h3>{d['file']}</h3>"
                         f"<div class='card'><table>{''.join(seg_rows)}</table></div>")

    samp = ''.join(samp_html)

    audits = [d['paired_match'] for d in data if 'paired_match' in d]
    if audits:
        pair_rows = ['<tr><th>北云段</th><th>北云首帧时间</th><th>北斗候选段</th>'
                     '<th>北斗首帧时间</th><th>时间差(s)</th><th>匹配类型</th></tr>']
        for a in audits:
            pair_rows.append(
                f"<tr><td>{a['reference_index']}</td><td class='mono'>{a['reference_start_time']}</td>"
                f"<td>{a.get('beidou_segment_index')}</td>"
                f"<td class='mono'>{a.get('beidou_start_time')}</td>"
                f"<td class='mono'>{a.get('time_delta_s')}</td>"
                f"<td>{a['match_type']}</td></tr>")
        pair_note = ('北云 cold 段为主切割/编号基准；±30s 为普通匹配，±90s 为扩展匹配；'
                     '北云时间重置段使用段内首个有效 GPS 时间或后续段有效时间做工程重建。'
                     '边界仍取北斗 BESTNAVA 自身首末帧。卫星数骤降阈值'
                     '（下降≥10颗或首帧≤上一段末尾70%）为工程判据，不是厂商协议常数。')
        pair_audit_html = ('<div class="card"><table>' + ''.join(pair_rows) +
                           '</table></div><div class="note">' + pair_note + '</div>')
    else:
        pair_audit_html = ''

    if audits:
        pair_md = ['| 北云段 | 北云首帧时间 | 北斗候选段 | 北斗首帧时间 | 时间差(s) | 匹配类型 |',
                   '|---:|---:|---:|---:|---:|---|']
        for a in audits:
            pair_md.append(f"| {a['reference_index']} | {a['reference_start_time']} "
                           f"| {a.get('beidou_segment_index')} | {a.get('beidou_start_time')} "
                           f"| {a.get('time_delta_s')} | {a['match_type']} |")
        pair_md.append('')
        pair_md.append('> 北云 cold 段为主切割/编号基准；±30s 为普通匹配，±90s 为扩展匹配；'
                       '北云时间重置段使用段内首个有效 GPS 时间或后续段有效时间做工程重建。'
                       '边界仍取北斗 BESTNAVA 自身首末帧。卫星数骤降阈值'
                       '（下降≥10颗或首帧≤上一段末尾70%）为工程判据，不是厂商协议常数。')
        pair_audit_md = '\n'.join(pair_md)
    else:
        pair_audit_md = ''

    html = (HTML.replace('__CSS__', CSS)
            .replace('__MANUAL__', MANUAL)
            .replace('__OVERVIEW__', overview)
            .replace('__INTEG__', integ)
            .replace('__KEYTIME__', keytime)
            .replace('__STAGEBAR__', stagebar)
            .replace('__SAMP__', samp)
            .replace('__PAIRAUDIT__', pair_audit_html)
            .replace('__IMG1__', img64(imgs['keytime']))
            .replace('__IMG2__', img64(imgs['stages']))
            .replace('__IMG3__', img64(imgs['svs']))
            .replace('__SRCLIST__', '、'.join(d['file'] for d in data)))

    # ---------- Markdown ----------
    md = []
    md.append('# 北斗（UM980）GNSS 冷启动策略分析')
    md.append('')
    md.append('- 数据源：' + '、'.join(f'`{d["file"]}`' for d in data) + '（混合二进制+ASCII 日志，#BESTNAVA）')
    md.append(f'- 口径依据：{MANUAL}（`beidou_manual\\Unicore Reference Commands Manual For N4 High Precision Products_V2_CH_R1.15.pdf`）')
    md.append('- 分析维度：时标状态 / 解算状态 / 定位类型 / 卫星数；本报告不含 INS（UM980 无 IMU）')
    md.append('')
    md.append('## 一、数据完整性与报文构成')
    md.append('')
    md.append('| 文件 | 时长 | 定位报文 | 实测周期 | 时标分布(条) | 定位类型分布(条) | 间断/重定标 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        md.append(f"| `{d['file']}` | {d['span']}s | {(d.get('segment') or {}).get('source', 'BESTNAVA')} {d['n']}条；"
                  f"{d['nominal']}s/{d['rate']}Hz | `{d['tstat_dist']}` | `{d['ptype_dist']}` "
                  f"| {len(d['gaps'])} / {len(d['rebases'])} |")
    md.append('')
    md.append('> 口径（N4 手册）：报文按行做 32 位 CRC 校验（`#` 开头为 32 位 CRC，`$` 开头为 NMEA 异或）；'
              '报头结构见表 7-51，BESTNAV 数据结构见表 7-70。'
              '卫星维度口径：#SVs=跟踪卫星数、#solnSVs=解算卫星数；表 7-70 无 #solnMultiSVs'
              '（字段 17-19 为 Reserved），多频解算维度不适用，输出“—”，不解析保留字段。')
    md.append('')
    md.append('### 报文完整性与周期（按报头时间戳逐帧判定）')
    md.append('')
    md.append('| 文件 | 帧数 | 实测周期 | 标称频率 | CRC失败行 | 真实间断 | 周重定标 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        md.append(f"| `{d['file']}` | {d['n']} | {d['nominal']}s | {d['rate']}Hz "
                  f"| {d['crc_bad']} | {len(d['gaps'])} | {len(d['rebases'])} |")
    md.append('')
    md.append('> 实测周期 = 全部相邻帧报头时间差的中位数（不假设）；真实间断 = 相邻帧时间差 > 2.5×实测周期（保留在时间轴中）；周重定标 = 相邻帧时间差 > 3600s（按时间连续处理）。')
    md.append('')
    md.append('## 二、四个关键时刻（秒）')
    md.append('')
    md.append('| 文件 | 获得卫星时间(脱离UNKNOWN) | 时标进入 FINE | 差分改正可用 | 首单点 | 首浮点 | 首固定 | 无解/单点/浮点/固定 占比% | 跟踪星 min/均值/max |')
    md.append('|---|---|---|---|---|---|---|---|---|')
    for d in data:
        f = d['first']
        pc = d['cat_pct']
        md.append(f"| `{d['file']}` | **{V(f['valid'])}** | {V(f['fine'])} "
                  f"| {V(f['stn'])} | {V(f['single'])} | {V(f['float'])} | **{V(f['fixed'])}** "
                  f"| {pc.get('0', 0)}/{pc.get('1', 0)}/{pc.get('2', 0)}/{pc.get('3', 0)} "
                  f"| {d['svs']['min']}/{d['svs']['mean']}/{d['svs']['max']} |")
    md.append('')
    md.append('> “—”规则：若该次启动段首已持有效时标（断电后 GPS 时间由 UM980 内部电池保持，Time Status 未丢失），'
              '“获得卫星时间 / 时标进入 FINE”输出“—”，不参与对比。N4 时标状态仅 UNKNOWN / FINE 两态（表 7-51）。')
    md.append('')
    md.append(f"![关键时刻对比]({os.path.basename(imgs['keytime'])})")
    md.append('')
    md.append('## 三、冷启动耗时分解')
    md.append('')
    md.append('### 表1：四阶段严格分解')
    md.append('')
    md.append('| 文件 | 无时间 | 时间→单点 | 单点→浮点 | 浮点→固定 | 首固定 | 差分数据可用 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        st = d['stages']
        md.append(f"| `{d['file']}` | {V(st['no_time'])} | {V(st['time_to_single'])} "
                  f"| {V(st['single_to_float'])} | {V(st['float_to_fixed'])} "
                  f"| **{V(d['first']['fixed'])}** | {V(d['first']['stn'])} |")
    md.append('')
    md.append('### 表2：图示使用的路径汇总（支持无浮点直达固定）')
    md.append('')
    md.append('| 文件 | 无时间 | 时间→单点 | 单点→浮点 | 浮点→固定 | 单点→固定 | 合计到首固定 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        st = d.get('stages_express', d['stages'])
        total = round(st['no_time'] or 0, 1) + round(st['time_to_single'] or 0, 1)
        if st['single_to_float'] is not None:
            total += round(st['single_to_float'], 1)
        if st['float_to_fixed'] is not None:
            total += round(st['float_to_fixed'], 1)
        elif st['single_to_fixed'] is not None:
            total += round(st['single_to_fixed'], 1)
        md.append(f"| `{d['file']}` | {V(st['no_time'])} | {V(st['time_to_single'])} "
                  f"| {V(st['single_to_float'])} | {V(st['float_to_fixed'])} "
                  f"| {V(st['single_to_fixed'])} | {total}s |")
    md.append('')
    md.append(f"![冷启动耗时分解]({os.path.basename(imgs['stages'])})")
    md.append('')
    md.append('## 四、北斗-北云配对审计')
    md.append('')
    md.append('__PAIRAUDIT_MD__')
    md.append('')
    md.append('## 五、定位类型分段统计')
    md.append('')
    for d in data:
        md.append(f"### `{d['file']}`")
        md.append('')
        md.append('| 定位类型段 | 起–止(s) | 时长(s) | 条数 | 跟踪星 | 解算星 | 多频解算 |')
        md.append('|---|---|---|---|---|---|---|')
        for lab, a, b, dur, c, svs, soln, multi in d['segs']:
            md.append(f"| {lab} | {a}–{b} | {dur} | {c} | {svs} | {soln} | {MV(multi)} |")
        md.append('')
    md.append(f"![卫星数量时间轴]({os.path.basename(imgs['svs'])})")
    md.append('')
    md.append('')
    md.append('')
    md.append('## 附录：口径与手册条款对应')
    md.append('')
    md.append('| 报告字段 | 数据来源 | 手册条款 | 说明 |')
    md.append('|---|---|---|---|')
    md.append('| 获得卫星时间 | #BESTNAVA 报头 Time Status 首次脱离 UNKNOWN | 表 7-51 字段 5 | UNKNOWN 表明接收机尚未计算出准确的 GPS 时间；仅 UNKNOWN / FINE 两态；时标未丢失的段输出“—” |')
    md.append('| 时标进入 FINE | Time Status 首次 FINE | 表 7-51 字段 5 | 与“获得卫星时间”同刻（无 COARSE 态）；时标未丢失的段输出“—” |')
    md.append('| 首单点 / 首浮点 / 首固定 | 定位类型 Pos Type 首次 SINGLE 系 / NARROW_FLOAT 系 / NARROW_INT 系 | 表 0-5 | SINGLE=16 单点；PSRDIFF=17 伪距差分；NARROW_FLOAT=34 窄巷浮点解；NARROW_INT=50 窄巷固定解 |')
    md.append('| 差分改正可用 | Stn ID 非空且 != "0" | 表 7-70 字段 12 | 基站 ID，缺省值 0（实测如 "1793"）；单点定位时为 0 |')
    md.append('| 无解段解算状态 | Sol Status = INSUFFICIENT_OBS / NO_CONVERGENCE / COV_TRACE / VARIANCE | 表 0-6 | 观测数据不足 / 无法收敛 / 协方差迹超限 / 方差超限；SOL_COMPUTED=已解出 |')
    md.append('| 跟踪星 / 解算星 | #SVs / #solnSVs | 表 7-70 字段 15/16 | 跟踪卫星数 / 解算卫星数 |')
    md.append('| 多频解算 | 不适用（输出“—”） | 表 7-70 字段 17-19 | BESTNAV 无 #solnMultiSVs，17-19 为 Reserved，不解析保留字段 |')
    md.append('| 冷启动段判定 | 无基站 + 定位类型 NONE/单点类 + 差分/固定尾段 + 卫星数骤降 | 表 0-5/0-6/7-70 | UM980 可保持 FINE 时标并很快输出 SINGLE，因此 SOL_COMPUTED 不作为排除条件。卫星数骤降为工程判据；同步北云日志另提供主切割/编号基准 |')
    md.append('')
    md.append('> 时间轴为录制经过时间（t=0 为各日志首条记录）；若出现冷启动 GPS 周重定标（相邻两条报头时间差 >3600s），按时间连续处理（间隔按标称周期计）。')
    md_text = '\n'.join(md).replace('__PAIRAUDIT_MD__', pair_audit_md)
    return html, md_text



def _valid_absolute_time(value: float) -> bool:
    """Reject protocol/default-week timestamps far outside the true GPS epoch."""
    return 1_400_000_000.0 <= value < 1_500_000_000.0


def _segment_frames(rows, segment):
    return [r for r in rows if segment.start <= r.start < segment.end]


def _first_valid_time(frames):
    return next((r.t for r in frames if _valid_absolute_time(r.t)), None)


def _beidou_startup_evidence(frames, previous_frames):
    """Evaluate UM980 restart evidence without changing shared segmentation.

    The evidence is based on N4 tables 0-5, 0-6 and 7-70.  SOL_COMPUTED is
    allowed because UM980 can retain its battery-backed clock and produce a
    SINGLE solution immediately after a power cycle.  The satellite-drop rule
    is an engineering threshold from synchronized 1026 data, not a protocol
    constant.
    """
    if not frames:
        return False, {}
    first = frames[0]
    if first.pt not in ('NONE', 'SINGLE', 'PSRDIFF', 'SPPDIFF'):
        return False, {}
    if first.stn not in ('', '0'):
        return False, {}
    has_differential_tail = any(
        r.pt in ('NARROW_INT', 'WIDE_INT', 'L1_INT', 'NARROW_FLOAT',
                 'L1_FLOAT', 'PSRDIFF', 'SPPDIFF') or r.stn not in ('', '0')
        for r in frames
    )
    prior_svs = previous_frames[-1].svs if previous_frames else None
    svs_drop = None if prior_svs is None else prior_svs - first.svs
    svs_evidence = svs_drop is not None and (
        svs_drop >= 10 or first.svs <= 0.70 * prior_svs
    )
    evidence = dict(
        first_sol=first.sol,
        first_pos_type=first.pt,
        first_stn=first.stn,
        first_svs=first.svs,
        previous_svs=prior_svs,
        svs_drop=svs_drop,
        has_differential_tail=has_differential_tail,
    )
    return (has_differential_tail and svs_evidence), evidence


def _beidou_candidate_segments(segments, rows, min_frames, min_duration_s):
    """Return UM980 restart candidates in recording order."""
    candidates = []
    previous = None
    for seg in segments:
        frames = _segment_frames(rows, seg)
        evidence_ok, evidence = _beidou_startup_evidence(frames, previous)
        if (seg.discontinuity_before > 0 and len(frames) >= min_frames and
                seg.span >= min_duration_s and evidence_ok):
            candidates.append((seg, frames, evidence))
        if frames:
            previous = frames
    return candidates


def _paired_beidou_segments(raw, reference_path, min_frames,
                            min_duration_s, include_unmatched_reference):
    """Pair UM980 candidates to BeiYun cold-start segments."""
    reference_segments, _source, reference_rows, _nominal, _recovery = split_beiyun(
        Path(reference_path).read_bytes(), min_frames=min_frames,
        min_duration_s=min_duration_s
    )
    reference_cold = [s for s in reference_segments if s.is_coldstart]
    segments, source, rows, _nominal, recovery = split_beidou(
        raw, min_frames=min_frames, min_duration_s=min_duration_s
    )
    candidates = _beidou_candidate_segments(
        segments, rows, min_frames, min_duration_s
    )
    used = set()
    matched = []
    audit = []
    for ref_index, ref_seg in enumerate(reference_cold, 1):
        ref_frames = _segment_frames(reference_rows, ref_seg)
        ref_time = _first_valid_time(ref_frames)
        time_reconstructed = ref_time is None
        if time_reconstructed:
            next_times = [
                _first_valid_time(_segment_frames(reference_rows, s))
                for s in reference_cold[ref_index:]
            ]
            next_times = [t for t in next_times if t is not None]
            if next_times:
                ref_time = min(next_times) - 1.0
        if ref_time is None:
            audit.append(dict(
                reference_index=ref_index,
                reference_start_time=None,
                match_status='reference_time_unavailable',
                match_type='unmatched',
            ))
            if include_unmatched_reference:
                matched.append(None)
            continue

        local = []
        for idx, (c_seg, c_frames, c_evidence) in enumerate(candidates):
            if idx in used or not _valid_absolute_time(c_frames[0].t):
                continue
            delta = abs(c_frames[0].t - ref_time)
            local.append((delta, idx, c_seg, c_frames, c_evidence))
        normal = [x for x in local if x[0] <= PAIRED_MATCH_S]
        expanded = [x for x in local if x[0] <= PAIRED_MATCH_EXTENDED_S]
        chosen = None
        if normal:
            chosen = min(normal, key=lambda x: (x[0], x[1]))
            match_type = ('normal' if not time_reconstructed
                          else 'time_reconstructed')
        elif expanded:
            chosen = min(expanded, key=lambda x: (x[0], x[1]))
            match_type = ('expanded' if not time_reconstructed
                          else 'expanded_time_reconstructed')
        if chosen is None:
            audit.append(dict(
                reference_index=ref_index,
                reference_start_time=round(ref_time, 3),
                match_status='beidou_segment_not_found',
                match_type='unmatched',
            ))
            if include_unmatched_reference:
                matched.append(None)
            continue

        _delta, candidate_index, segment, frames, evidence = chosen
        used.add(candidate_index)
        matched.append((ref_index, segment, frames, evidence))
        audit.append(dict(
            reference_index=ref_index,
            reference_start_time=round(ref_time, 3),
            beidou_segment_index=segment.index,
            beidou_start_time=round(frames[0].t, 3),
            time_delta_s=round(frames[0].t - ref_time, 3),
            match_status='matched',
            match_type=match_type,
            beidou_start_evidence=evidence,
        ))
    return matched, audit, segments, source, rows, recovery, reference_segments


def split_segments(path, min_frames=50, min_duration_s=20.0):
    """识别动态冷启动段，返回 (序号, 帧子序列, Segment, RecoveryResult)。"""
    with open(path, 'rb') as fp:
        raw = fp.read()
    segs, _source, rows, _nominal, recovery = split_beidou(raw, min_frames=min_frames, min_duration_s=min_duration_s)
    cold = [s for s in segs if s.is_coldstart]
    return [(n, [r for r in rows if s.start <= r.start < s.end], s)
            for n, s in enumerate(cold, 1)]


def analyze_file(path, split=False, min_frames=50, indoor_t0=None, min_duration_s=20.0,
                         reference_path=None, include_unmatched_reference=False):
    """分析一个日志文件。

    split=False：按独立冷启动包逐文件分析；
    split=True：动态识别 N 次冷启动段。边界由“状态丢失+动态间断”给出，
    不依赖固定时长、固定 GPS 周回落或固定 5×周期阈值。
    reference_path 提供同步北云日志时，以北云 cold 段为主基准匹配北斗候选段。
    """
    with open(path, 'rb') as fp:
        raw = fp.read()
    base = os.path.basename(path)
    if not split:
        return [build_raw(raw, base, 0)]

    if reference_path is not None:
        paired, audit, segs, source, rows, recovery, _reference_segments = _paired_beidou_segments(
            raw, reference_path, min_frames, min_duration_s,
            include_unmatched_reference
        )
        out = []
        for item in paired:
            if item is None:
                continue
            n, s, selected, evidence = item
            shift = 0.0
            if indoor_t0 is not None and n <= len(indoor_t0):
                shift = indoor_t0[n - 1] or 0.0
            d = build_raw(raw, f'{base}_cold{n:02d}', s.start, t0_shift=shift,
                          frames=selected, recovery=recovery)
            d['segment'] = dict(
                index=n, frames=s.frames, start_week=s.start_week,
                start_tstat=s.start_tstat, start_pos_type=s.start_pos_type,
                byte_offset=s.start, end_offset=s.end, t0_shift=shift,
                source=source, span=s.span,
                discontinuity_before=s.discontinuity_before,
                start_elapsed=s.start_elapsed, end_elapsed=s.end_elapsed,
                is_coldstart=True,
                reference_product='北云',
                reference_index=n,
                start_evidence=evidence,
            )
            d['paired_match'] = next(a for a in audit
                                      if a['reference_index'] == n)
            out.append(d)
        return out, audit

    segs, source, rows, nominal, recovery = split_beidou(raw, min_frames=min_frames, min_duration_s=min_duration_s)
    cold = [s for s in segs if s.is_coldstart]
    if not cold:
        raise ValueError(f'{base}: 未识别到有效冷启动段（检查到的分段数={len(segs)}，'
                         f'定位流={source}，有效帧={len(rows)}）。请确认文件确实包含多段冷启动；'
                         '如文件是单次冷启动，请取消勾选自动切片。')

    out = []
    for n, s in enumerate(cold, 1):
        selected = [r for r in rows if s.start <= r.start < s.end]
        shift = 0.0
        if indoor_t0 is not None and n <= len(indoor_t0):
            shift = indoor_t0[n - 1] or 0.0
        d = build_raw(raw, f'{base}_cold{n:02d}', s.start, t0_shift=shift, frames=selected,
                      recovery=recovery)
        d['segment'] = dict(index=n, frames=s.frames, start_week=s.start_week,
                            start_tstat=s.start_tstat, start_pos_type=s.start_pos_type,
                            byte_offset=s.start, end_offset=s.end, t0_shift=shift,
                            source=source, span=s.span,
                            discontinuity_before=s.discontinuity_before,
                            start_elapsed=s.start_elapsed, end_elapsed=s.end_elapsed,
                            is_coldstart=s.is_coldstart)
        out.append(d)
    return out


def run(paths, out_dir=None, open_browser=False, split=False, reference_path=None):
    """分析一组北斗 UM980 日志（#BESTNAVA），输出 HTML/MD/PNG/JSON 到 out_dir。
    返回 (html_path, md_path)。"""
    if not paths:
        raise SystemExit('未提供日志文件')
    if out_dir is None:
        out_dir = DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    data = []
    paired_audits = {}
    for p in sorted(paths):
        result = analyze_file(p, split=split, reference_path=reference_path)
        if reference_path is None:
            data.extend(result)
        else:
            rows, audit = result
            data.extend(rows)
            paired_audits[os.path.basename(p)] = audit
    imgs = make_images(data, out_dir)
    html, md = build_outputs(data, imgs)

    h_path = os.path.join(out_dir, 'report.html')
    m_path = os.path.join(out_dir, 'report.md')
    with open(h_path, 'w', encoding='utf-8') as fp:
        fp.write(html)
    with open(m_path, 'w', encoding='utf-8') as fp:
        fp.write(md)
    js_path = os.path.join(out_dir, 'summary.json')
    with open(js_path, 'w', encoding='utf-8') as fp:
        payload = [{k: v for k, v in d.items() if k != 'series'} for d in data]
        if paired_audits:
            payload.append({'paired_matching_audit': paired_audits})
        json.dump(payload, fp, ensure_ascii=False, indent=1)

    print('生成 HTML:', h_path, f'{len(html)/1024:.0f} KB')
    print('生成 MD  :', m_path)
    print('生成图片 :', imgs['keytime'], '|', imgs['stages'], '|', imgs['svs'])
    print('摘要JSON :', js_path)
    if open_browser:
        webbrowser.open('file:///' + h_path.replace(chr(92), '/'))
    return h_path, m_path


def main():
    args = [a for a in sys.argv[1:]]
    split = '--split' in args
    reference_path = None
    if '--reference-beiyun' in args:
        pos = args.index('--reference-beiyun')
        if pos + 1 >= len(args):
            raise SystemExit('--reference-beiyun 需要一个北云日志路径')
        reference_path = args[pos + 1]
        args = args[:pos] + args[pos + 2:]
    args = [a for a in args if a != '--split']
    if len(args) < 2:
        print(__doc__)
        print('用法: python BEIDOU_coldstart.py <输出目录> [--split] <日志1> [日志2 ...]')
        print('  --split  一次性录制了 N 次冷启动：自动识别启动次数并切片后逐段分析')
        print('  --reference-beiyun <file>  同步北云基准日志；北云 cold 段作为主切割/编号基准')
        raise SystemExit(2)
    out_dir = args[0]
    paths = [a for a in args[1:] if os.path.isfile(a)]
    run(paths, out_dir, open_browser=False, split=split, reference_path=reference_path)


if __name__ == '__main__':
    main()
