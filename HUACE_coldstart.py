# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
华测（HUACE M720 GNSS 模组）冷启动策略分析
=====================================================================
与北云 BY_coldstart.py 同构：字段含义、单位、分析口径一致，仅报文名与枚举值按华测手册适配。

分析对象：由 HMI 或命令行传入的北云/华测原始日志；华测支持混合二进制+ASCII

口径依据（华测《M7系列模组用户指令及协议手册 V2.7》）：
  * 主定位流     动态选择 #BESTPA / #BESTPOSA / #RTKPA；GMF 头 week=字段7、TOW=字段8(ms)，NovAtel 头 week=字段6、sec=字段7(s)
  * 时标状态      报头 Time Status（表3-19）：UNKNOWN=0 时间有效性未知 / APPROXIMATE=1 近似时间 /
                  COARSE=3 粗略 / COARSESTEERING=4 粗调且调优中 / FINE=7 高精度 /
                  FINESTEERING=9 已正确配置且调优中（本批数据出现 APPROXIMATE/COARSESTEERING/FINESTEERING）
  * 解算状态      表3-39：SOL_COMPUTED=0 已解出；INSUFFICIENT_OBS=1 观测数据不足；
                  NO_CONVERGENCE=2 无法收敛；VARIANCE=5 方差超过限制
  * 定位类型      表3-40：NONE=0 无解；SINGLE=1 单点定位；PSRDIFF（实测为伪距差分，
                  与表3-40 SPPDIFF=2 伪距差分解对应）；NARROW_FLOAT=5 窄巷浮点解；
                  NARROW_INT=4 窄巷固定解；WIDE_INT=13；L1_INT=14；L1_FLOAT=15
  * 卫星字段      #SVs=跟踪到的卫星数；#solnSVs=参与解算的卫星数；
                  #solnMultiSVs=参与解算的多频信号卫星数（含义/单位与北云一致）
  * 差分可用      Stn ID 非空且 != "0"（实测为十进制字符串形式，如 "1793"；单点定位时为空串）
  * CRC           "#" 开头 = 32 位 CRC（表3-19 及报文格式说明）；"$" 开头 = NMEA 异或

输出（由 HMI 指定输出目录，或 CLI 指定）：
  report.html / report.md / img_关键时刻.png / img_阶段耗时.png / img_卫星数量时间轴.png / summary.json
运行：
  HMI：python gnss_coldstart_hmi.py（产品选“华测 HUACE”）
  CLI：python HUACE_coldstart.py <输出目录> <日志1> [日志2 ...]
"""
import os
import sys
import io
import json
import base64
import collections
import statistics
import webbrowser

from coldstart_split import split_beiyun, split_huace
from gnss_recovery import (RecoveryResult, recover_ascii_messages, parse_beiyun_positions,
                           parse_huace_positions, select_huace_positions)

# ----------------------------------------------------------------------
# 路径与常量（相对脚本所在目录解析，便于迁移）
# ----------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, 'reports')            # 默认输出目录
TAG = '华测GNSS冷启动分析报告'
MANUAL = '华测《M7系列模组用户指令及协议手册 V2.7》'

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 定位类型分组（华测表3-40；与北云口径一致）：1=单点类 / 2=浮点类 / 3=固定类，其余归 0=无解
CAT1 = {'SINGLE', 'PSRDIFF', 'SPPDIFF', 'WAAS', 'SBAS', 'INS_PSRSP', 'INS_PSRDIFF', 'INS_SBAS', 'INS_SPP'}
CAT2 = {'L1_FLOAT', 'IONOFREE_FLOAT', 'NARROW_FLOAT', 'FLOATCONV', 'WIDELANE',
        'NARROWLANE', 'INS_RTKFLOAT', 'PPP_CONVERGING'}
CAT3 = {'L1_INT', 'WIDE_INT', 'NARROW_INT', 'INS_RTKFIXED', 'RTK_DIRECT_INS',
        'PPP', 'PPP_FIXED', 'INS_PPP'}
TS_CODE = {'UNKNOWN': 0, 'APPROXIMATE': 1, 'COARSE': 1, 'COARSESTEERING': 1, 'FINE': 2, 'FINESTEERING': 2, 'SATTIME': 2}
SOL_ORDER = ['INSUFFICIENT_OBS', 'VARIANCE', 'NO_CONVERGENCE', 'SOL_COMPUTED']
PT_CN = {'NONE': '无解', 'SINGLE': '单点', 'PSRDIFF': '伪距差分', 'NARROW_FLOAT': 'RTK浮点', 'NARROW_INT': 'RTK固定'}
CAT_CN = ['无解', '单点', 'RTK浮点', 'RTK固定']

# ----------------------------------------------------------------------
# Legacy line parser retained only as a reference; active parsing is gnss_recovery.py.

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
            # Sub-frame positive jitter can occur when interleaved streams are
            # merged or receiver output is jittered. Collapse it to the measured
            # nominal period; larger gaps remain visible in the elapsed axis.
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
# 单文件分析：指标定义与 analyze_0909.py / analyze_0909b.py / gen_report_0909.py 完全一致
# ----------------------------------------------------------------------


def first_idx(pred, seq):
    for i, v in enumerate(seq):
        if pred(v):
            return i
    return None


def build(path):
    """分析一份日志文件，返回全部指标。"""
    with open(path, 'rb') as fp:
        return build_raw(fp.read(), os.path.basename(path))


def build_raw(raw, name='<segment>', byte_offset=0, t0_shift=0.0, frames=None, recovery=None):
    """分析一段原始字节流或已恢复的定位帧序列。

    华测日志可能是混合二进制+ASCII。解析按字节锚点恢复完整报文并校验；
    定位流在 BESTPA、BESTPOSA、RTKPA 中动态选择覆盖完整且帧数最多的连续
    流，不把 BESTPOSA 写死为唯一主报文。
    """
    rec_messages: list = []
    if not frames:
        recovery = recover_ascii_messages(raw)
        rec_messages = recovery.messages
        candidates = parse_huace_positions(rec_messages)
        _source, frames, _nominal_hint = select_huace_positions(candidates)
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
        raise ValueError('未恢复到任何有效华测 BESTPA/BESTPOSA/RTKPA 定位帧（%s）：'
                         '请确认该文件/片段是否为华测日志，且含上述定位报文' % name)
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

    f_idx = dict(
        valid=first_idx(lambda r: r['ts'] not in ('UNKNOWN', 'APPROXIMATE'), rows),
        coarse=first_idx(lambda r: r['ts'] in ('COARSE', 'COARSESTEERING'), rows),
        fine=first_idx(lambda r: r['ts'] in ('FINE', 'FINESTEERING'), rows),
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
        events.append([f['valid'], '获得卫星时间（脱离 UNKNOWN/APPROXIMATE）', '#0ea5e9'])
    if f['stn'] is not None:
        i_stn = f_idx['stn']
        events.append([f['stn'], f"差分改正可用（基站 {rows[i_stn]['stn']}）", '#7c3aed'])
    if f['single'] is not None:
        events.append([f['single'], '首次单点/伪距差分解', '#3b82f6'])
    if f['coarse'] is not None:
        events.append([f['coarse'], '时标进入 COARSE系', '#64748b'])
    if f['fine'] is not None:
        events.append([f['fine'], '时标进入 FINE系', '#6b7280'])
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
                         round(statistics.mean([r['svs'] for r in sub]), 1),
                         round(statistics.mean([r['soln'] for r in sub]), 1),
                         round(statistics.mean([r['multi'] for r in sub]), 1)])
        else:
            segs.append([lab, a, b, seg_duration, c, 0, 0, 0])

    return dict(
        file=name, byte_offset=byte_offset, span=round(el[-1], 1), n=len(rows),
        rate=round(1.0 / max(nominal, 1e-9), 1), nominal=round(nominal, 3), crc_bad=bad,
        counts=dict(counts), other=dict(other),
        recovery=dict(valid_messages=len(rec_messages), embedded=recovery.embedded,
                      checksum_failed=recovery.bad, method='byte-anchor+checksum'),
        gaps=gaps, rebases=reb,
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
    """报告排序：同一次启动相邻（北云01、华测01、北云02、华测02……）。
    单产品退化为按文件名排序。键 = (启动序号, 产品序)。"""
    def key(d):
        seg = d.get('segment') or {}
        idx = seg.get('index', 0)
        prod = 0 if '北云' in d['file'] else (1 if '华测' in d['file'] else 2)
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
    ax.set_title((title_prefix or '华测') + '冷启动关键时刻对比（t=0 为日志起点）', fontsize=13)
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
    ax.set_title((title_prefix or ('北云' if 'BY_' in __file__ else '华测')) +
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

# ----------------------------------------------------------------------
# ----------------------------------------------------------------------


def V(v, u='s'):
    return '—' if v is None else f'{v}{u}'


# ----------------------------------------------------------------------
# HTML 模板（与示意表一致的五节结构 + 交互式时间轴）
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
 #controls{position:sticky;top:0;z-index:10;background:#fff;border:1px solid #e5e7eb;border-radius:8px;
  padding:10px 16px;display:flex;align-items:center;gap:14px;margin:10px 0 14px;flex-wrap:wrap;
  box-shadow:0 2px 6px rgba(0,0,0,.06)}
 #btnPlay{width:40px;height:40px;border-radius:50%;border:none;background:#2563eb;color:#fff;font-size:16px;cursor:pointer}
 #slider{flex:1;min-width:220px}
 #tLabel{font-family:Consolas,monospace;font-size:13px;width:420px;text-align:center}
 select{font-size:13px;padding:3px 6px}
 .legend{display:flex;gap:14px;font-size:12px;color:#374151;margin:2px 0 4px 2px;flex-wrap:wrap}
 .legend span::before{content:"■";margin-right:4px}
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
<title>华测GNSS冷启动分析报告</title>
<style>__CSS__</style>
</head>
<body><div class="wrap">
<h1>华测（HUACE M720）GNSS 冷启动策略分析</h1>
<div class="sub">数据源：__SRCLIST__（COM1 ASCII 日志，#BESTPOSA）｜口径依据：__MANUAL__｜分析维度：时标状态 / 解算状态 / 定位类型 / 卫星数｜本报告不含 INS</div>

<h2>一、数据完整性与报文构成</h2>
<div class="card"><table>
<tr><th>文件</th><th>时长</th><th>定位报文</th><th>实测周期</th><th>时标分布(条)</th><th>定位类型分布(条)</th><th>间断/重定标</th></tr>
__OVERVIEW__
</table>
<div class="note">口径（华测 M7 手册）：报文按行做 CRC 校验（"#" 开头为 32 位 CRC，"$" 开头为 NMEA 异或）。卫星维度口径：#SVs=跟踪到的卫星数、#solnSVs=参与解算的卫星数、#solnMultiSVs=参与解算的多频信号卫星数（含义与单位同北云 BESTGNSSPOSA）；若需逐星载噪比（C/N0）与分星座可见星，需另行录制 GSV 或 RANGEA 报文。</div></div>


<h3>报文完整性与周期（按报头时间戳逐帧判定）</h3>
<div class="card"><table>
<tr><th>文件</th><th>帧数</th><th>实测周期</th><th>标称频率</th><th>CRC失败行</th><th>真实间断</th><th>周重定标</th></tr>
__INTEG__
</table>
<div class="note">实测周期 = 全部相邻帧报头时间差的中位数（不假设）；真实间断 = 相邻帧时间差 &gt; 2.5×实测周期（保留在时间轴中）；周重定标 = 相邻帧时间差 &gt; 3600s（按时间连续处理，间隔按实测周期计）。</div></div>

<h2>二、四个关键时刻（秒）</h2>
<div class="card"><table>
<tr><th>文件</th><th>获得卫星时间<br>(脱离UNKNOWN)</th><th>COARSE→FINE</th><th>差分改正可用</th><th>首单点</th><th>首浮点</th><th>首固定</th><th>无解/单点/浮点/固定 占比%</th><th>跟踪星<br>min/均值/max</th></tr>
__KEYTIME__
</table></div>
<div class="imgcard"><img src="__IMG1__" alt="关键时刻对比"></div>

<h2>三、冷启动耗时分解</h2>
__STAGEBAR__
<div class="note">"无时间"= 0 到时标脱离 UNKNOWN；"时间→单点"= 有卫星时间到第一个单点解；"单点→浮点"= 到第一个 RTK 浮点解；"浮点→固定"= 到第一个 RTK 固定解。条宽按时长比例。</div>
<div class="imgcard"><img src="__IMG2__" alt="冷启动耗时分解"></div>

<h2>四、定位类型分段统计</h2>
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
            f"<td>{d['segment']['source'] if 'segment' in d else ''} {d['n']}条</td><td>{d['nominal']}s/{d['rate']}Hz</td>"
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
            f"<td>{V(f['coarse'])} → {V(f['fine'])}</td>"
            f"<td>{V(f['stn'])}</td><td>{V(f['single'])}</td><td>{V(f['float'])}</td>"
            f"<td><b>{V(f['fixed'])}</b></td>"
            f"<td>{pc.get('0', 0)}/{pc.get('1', 0)}/{pc.get('2', 0)}/{pc.get('3', 0)}</td>"
            f"<td>{d['svs']['min']}/{d['svs']['mean']}/{d['svs']['max']}</td></tr>")
    keytime = ''.join(kt_html)

    STAGE_COLORS = ['#dc2626', '#f59e0b', '#3b82f6', '#16a34a']
    bars = []
    for d in data:
        st = d.get('stages_express', d['stages'])
        # 阶段条必须表示完整路径：缺失浮点时，用单点→固定补足；否则旧代码只画无时间/时间→单点。
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
                            f"<td>{svs}</td><td>{soln}</td><td>{multi}</td></tr>")
        samp_html.append(f"<h3>{d['file']}</h3>"
                         f"<div class='card'><table>{''.join(seg_rows)}</table></div>")
    samp = ''.join(samp_html)


    html = (HTML.replace('__CSS__', CSS)
            .replace('__MANUAL__', MANUAL)
            .replace('__OVERVIEW__', overview)
            .replace('__INTEG__', integ)
            .replace('__KEYTIME__', keytime)
            .replace('__STAGEBAR__', stagebar)
            .replace('__SAMP__', samp)
            .replace('__IMG1__', img64(imgs['keytime']))
            .replace('__IMG2__', img64(imgs['stages']))
            .replace('__IMG3__', img64(imgs['svs']))
            .replace('__SRCLIST__', '、'.join(d['file'] for d in data)))

    # ---------- Markdown ----------
    md = []
    md.append('# 华测（HUACE M720）GNSS 冷启动策略分析')
    md.append('')
    md.append('- 数据源：' + '、'.join(f'`{d["file"]}`' for d in data) + '（COM1 ASCII 日志，#BESTPOSA）')
    md.append(f'- 口径依据：{MANUAL}（`huace_manual\\M7系列模组用户指令及协议手册_V2.7.pdf`）')
    md.append('- 分析维度：时标状态 / 解算状态 / 定位类型 / 卫星数；本报告不含 INS')
    md.append('')
    md.append('## 一、数据完整性与报文构成')
    md.append('')
    md.append('| 文件 | 时长 | 定位报文 | 实测周期 | 时标分布(条) | 定位类型分布(条) | 间断/重定标 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        md.append(f"| `{d['file']}` | {d['span']}s | {d.get('segment',{}).get('source','BESTPA/BESTPOSA/RTKPA')} {d['n']}条 | {d['nominal']}s/{d['rate']}Hz | `{d['tstat_dist']}` | `{d['ptype_dist']}` "
                  f"| {len(d['gaps'])} / {len(d['rebases'])} |")
    md.append('')
    md.append('> 口径（华测 M7 手册）：报文按行做 CRC 校验（`#` 开头为 32 位 CRC，`$` 开头为 NMEA 异或）。'
              '卫星维度口径：#SVs=跟踪到的卫星数、#solnSVs=参与解算的卫星数、#solnMultiSVs=参与解算的多频信号卫星数（含义与单位同北云）。')
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
    md.append('| 文件 | 获得卫星时间(脱离UNKNOWN) | COARSE→FINE | 差分改正可用 | 首单点 | 首浮点 | 首固定 | 无解/单点/浮点/固定 占比% | 跟踪星 min/均值/max |')
    md.append('|---|---|---|---|---|---|---|---|---|')
    for d in data:
        f = d['first']
        pc = d['cat_pct']
        md.append(f"| `{d['file']}` | **{V(f['valid'])}** | {V(f['coarse'])} → {V(f['fine'])} "
                  f"| {V(f['stn'])} | {V(f['single'])} | {V(f['float'])} | **{V(f['fixed'])}** "
                  f"| {pc.get('0', 0)}/{pc.get('1', 0)}/{pc.get('2', 0)}/{pc.get('3', 0)} "
                  f"| {d['svs']['min']}/{d['svs']['mean']}/{d['svs']['max']} |")
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
    md.append('## 四、定位类型分段统计')
    md.append('')
    for d in data:
        md.append(f"### `{d['file']}`")
        md.append('')
        md.append('| 定位类型段 | 起–止(s) | 时长(s) | 条数 | 跟踪星 | 解算星 | 多频解算 |')
        md.append('|---|---|---|---|---|---|---|')
        for lab, a, b, dur, c, svs, soln, multi in d['segs']:
            md.append(f"| {lab} | {a}–{b} | {dur} | {c} | {svs} | {soln} | {multi} |")
        md.append('')
    md.append(f"![卫星数量时间轴]({os.path.basename(imgs['svs'])})")
    md.append('')
    md.append('')
    md.append('')
    md.append('## 附录：口径与手册条款对应')
    md.append('')
    md.append('| 报告字段 | 数据来源 | 手册条款 | 说明 |')
    md.append('|---|---|---|---|')
    md.append('| 获得卫星时间 | #BESTPOSA 报头 Time Status 首次脱离 UNKNOWN | 表3-19 | UNKNOWN=0 时间有效性未知；数据中出现的 APPROXIMATE/COARSESTEERING 均属"已脱离 UNKNOWN" |')
    md.append('| COARSE→FINE | Time Status 首次进入 COARSE 系（COARSE/COARSESTEERING）/ FINE 系（FINE/FINESTEERING） | 表3-19 | COARSESTEERING=4 粗调调优中；FINESTEERING=9 已正确配置且调优中 |')
    md.append('| 首单点 / 首浮点 / 首固定 | 定位类型 Pos Type 首次 SINGLE / NARROW_FLOAT / NARROW_INT | 表3-40 | SINGLE=1 单点定位；NARROW_FLOAT=5 窄巷浮点解；NARROW_INT=4 窄巷固定解 |')
    md.append('| 差分改正可用 | Stn ID 非空且 != "0" | 3.2.14 字段24 | 基准站 ID（实测为十进制字符串，如 "1793"）；单点定位时为空串 |')
    md.append('| 无解段解算状态 | Sol Type = INSUFFICIENT_OBS / VARIANCE / NO_CONVERGENCE | 表3-39 | 观测数据不足 / 方差超过限制 / 无法收敛；SOL_COMPUTED=已解出 |')
    md.append('| 跟踪星 / 解算星 / 多频解算 | #SVs / #solnSVs / #solnMultiSVs | 3.2.14 字段15/16/17 | 跟踪到的卫星数 / 参与解算的卫星数 / 参与解算的多频信号卫星数（含义与单位同北云） |')
    md.append('')
    md.append('> 时间轴为录制经过时间（t=0 为各日志首条记录）；若出现冷启动 GPS 周重定标（相邻两条报头时间差 >3600s），按时间连续处理（间隔按标称周期计）。')
    return html, '\n'.join(md)



def split_segments(path, min_frames=50, min_duration_s=20.0):
    """识别动态冷启动段，返回 (序号, 帧子序列, Segment, RecoveryResult)。"""
    with open(path, 'rb') as fp:
        raw = fp.read()
    segs, _source, rows, _nominal, recovery = split_huace(raw, min_frames=min_frames, min_duration_s=min_duration_s)
    cold = [s for s in segs if s.is_coldstart]
    return [(n, [r for r in rows if r.start >= s.start and r.end <= s.end], s)
            for n, s in enumerate(cold, 1)]


def analyze_file(path, split=False, min_frames=50, indoor_t0=None, min_duration_s=20.0):
    """分析一个日志文件。

    split=False：按独立冷启动包逐文件分析；
    split=True：动态识别 N 次冷启动段。边界由“状态丢失+动态间断”给出，
    不依赖固定时长、固定 GPS 周回落或固定 5×周期阈值。
    """
    with open(path, 'rb') as fp:
        raw = fp.read()
    base = os.path.basename(path)
    if not split:
        return [build_raw(raw, base, 0)]

    segs, source, rows, nominal, recovery = split_huace(raw, min_frames=min_frames, min_duration_s=min_duration_s)
    cold = [s for s in segs if s.is_coldstart]
    if not cold:
        raise ValueError(f'{base}: 未识别到有效冷启动段（检查到的分段数={len(segs)}，'
                         f'定位流={source}，有效帧={len(rows)}）。请确认文件确实包含多段冷启动；'
                         '如文件是单次冷启动，请取消勾选自动切片。')

    out = []
    for n, s in enumerate(cold, 1):
        selected = [r for r in rows if r.start >= s.start and r.end <= s.end]
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


def run(paths, out_dir=None, open_browser=False, split=False):
    """分析一组华测 COM1 ASCII 日志（#BESTPOSA），输出 HTML/MD/PNG/JSON 到 out_dir。
    返回 (html_path, md_path)。"""
    if not paths:
        raise SystemExit('未提供日志文件')
    if out_dir is None:
        out_dir = DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    data = []
    for p in sorted(paths):
        data.extend(analyze_file(p, split=split))
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
        json.dump([{k: v for k, v in d.items() if k != 'series'} for d in data],
                  fp, ensure_ascii=False, indent=1)

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
    args = [a for a in args if a != '--split']
    if len(args) < 2:
        print(__doc__)
        print('用法: python HUACE_coldstart.py <输出目录> [--split] <日志1> [日志2 ...]')
        print('  --split  一次性录制了 N 次冷启动：自动识别启动次数并切片后逐段分析')
        raise SystemExit(2)
    out_dir = args[0]
    paths = [a for a in args[1:] if os.path.isfile(a)]
    run(paths, out_dir, open_browser=False, split=split)


if __name__ == '__main__':
    main()
