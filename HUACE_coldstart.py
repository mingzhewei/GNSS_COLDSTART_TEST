# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
华测（HUACE M720 GNSS 模组）冷启动策略分析
=====================================================================
与北云 BY_coldstart.py 同构：字段含义、单位、分析口径一致，仅报文名与枚举值按华测手册适配。

分析对象：由 gnss_coldstart_hmi.py（HMI）传入的一组 COM1 ASCII 日志（#BESTPOSA）

口径依据（华测《M7系列模组用户指令及协议手册 V2.7》）：
  * 报文          #BESTPOSA（BESTP 最佳位置信息，Message ID 3020；3.2.14）
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
# 报文解析（与 analyze_0909.py 一致：按行校验 CRC，失败行丢弃）
# ----------------------------------------------------------------------
HDR_MSGS = ('BESTPOSA',)

def crc32b(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc & 0xFFFFFFFF


def load(path):
    """读取 COM1 ASCII 日志；返回 (raw, {报文名:[(报头字段, 数据字段)]}, 其他报文计数, CRC失败行数)。
    # 开头 '#' 用 32 位 CRC（华测手册报文格式说明），'$' 用 NMEA 异或；校验失败行在此计数。"""
    raw = open(path, 'rb').read()
    H = collections.defaultdict(list)
    other = collections.Counter()
    bad = 0
    for line in raw.split(b'\n'):
        line = line.rstrip(b'\r')
        if not line or line[0] not in (0x23, 0x24):
            continue
        star = line.rfind(b'*')
        if star <= 0:
            bad += 1
            continue
        body = line[1:star]
        try:
            if line[0] == 0x23:
                ok = crc32b(body) == int(line[star + 1:star + 9], 16)
            else:
                c = 0
                for bb in body:
                    c ^= bb
                ok = c == int(line[star + 1:star + 3], 16)
        except ValueError:
            ok = False
        if not ok:
            bad += 1
            continue
        txt = body.decode('ascii', errors='ignore')
        if line[0] == 0x23:
            head, _, bpart = txt.partition(';')
            hf = head.split(',')
            if hf[0] in HDR_MSGS:
                H[hf[0]].append((hf, bpart.split(',')))
            else:
                other[hf[0]] += 1
        else:
            other[txt.split(',')[0]] += 1
    return raw, H, other, bad


def htime(hf):
    """报头 GPS 时间（周, 周内秒）-> 绝对秒。hf[5]=Week, hf[6]=Seconds。"""
    try:
        return int(hf[5]) * 604800 + float(hf[6])
    except Exception:
        return None


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
            el.append(el[-1] + max(dt, 0.0))
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
    """分析一份日志，返回全部指标（字段命名沿用原 gen_report_0909.py 的 JSON 结构）。"""
    name = os.path.basename(path)
    raw, H, other, bad = load(path)
    rows, tl = [], []
    for hf, bf in H['BESTPOSA']:
        t = htime(hf)
        if t is None or len(bf) < 21:
            continue
        try:
            rows.append(dict(ts=hf[4], sol=bf[0], pt=bf[1],
                             lstd=float(bf[7]), stn=bf[10].strip('"'),
                             age=float(bf[11]), svs=int(bf[13]), soln=int(bf[14]),
                             l1=int(bf[15]), multi=int(bf[16])))
            tl.append(t)
        except Exception:
            continue
    el, gaps, reb, nominal = build_elapsed(tl)
    pts = [r['pt'] for r in rows]
    cats = [3 if p in CAT3 else 2 if p in CAT2 else 1 if p in CAT1 else 0 for p in pts]

    f = dict(
        valid=first_idx(lambda r: r['ts'] != 'UNKNOWN', rows),      # 获得卫星时间（脱离 UNKNOWN）
        coarse=first_idx(lambda r: r['ts'] == 'COARSE', rows),
        fine=first_idx(lambda r: r['ts'] == 'FINESTEERING', rows),
        single=first_idx(lambda p: p in CAT1, pts),                 # 首个单点解
        float=first_idx(lambda p: p in CAT2, pts),                  # 首个 RTK 浮点解
        fixed=first_idx(lambda p: p in CAT3, pts),                  # 首个 RTK 固定解
        stn=first_idx(lambda r: r['stn'] not in ('', '0'), rows),   # 差分改正可用（Stn ID 非空且 != '0'）
        sol=first_idx(lambda r: r['sol'] == 'SOL_COMPUTED', rows),
    )
    f = {k: (None if v is None else round(el[v], 1)) for k, v in f.items()}

    def dur(a, b):
        return None if (a is None or b is None) else round(b - a, 1)

    # 冷启动耗时四阶段分解
    stages = dict(no_time=f['valid'],
                  time_to_single=dur(f['valid'], f['single']),
                  single_to_float=dur(f['single'], f['float']),
                  float_to_fixed=dur(f['float'], f['fixed']),
                  diff_wait=dur(f['valid'], f['stn']),
                  single_to_fixed=dur(f['single'], f['fixed']))

    events = []
    if f['valid'] is not None:
        events.append([f['valid'], '获得卫星时间（时标脱离UNKNOWN→COARSE）', '#0ea5e9'])
    if f['stn'] is not None:
        i_stn = first_idx(lambda r: r['stn'] not in ('', '0'), rows)
        events.append([f['stn'], f"差分改正可用（基站 {rows[i_stn]['stn']}）", '#7c3aed'])
    if f['single'] is not None:
        events.append([f['single'], '首次单点解 SINGLE', '#3b82f6'])
    if f['fine'] is not None:
        events.append([f['fine'], '时标进入 FINESTEERING', '#6b7280'])
    if f['float'] is not None:
        events.append([f['float'], '首次 RTK 浮点解 NARROW_FLOAT', '#f59e0b'])
    if f['fixed'] is not None:
        events.append([f['fixed'], '首次 RTK 固定解 NARROW_INT', '#16a34a'])
    for r in reb:
        events.append([r['elapsed'], '冷启动 GPS 周重定标（按连续处理）', '#dc2626'])
    events.sort()

    # 定位类型分段统计
    segs = []
    for lab, a, b, c in runs(el, pts):
        idx = [k for k in range(len(rows)) if a - 1e-6 <= el[k] <= b + 1e-6]
        sub = [rows[k] for k in idx]
        segs.append([lab, a, b, round(b - a, 1), c,
                     round(statistics.mean([r['svs'] for r in sub]), 1),
                     round(statistics.mean([r['soln'] for r in sub]), 1),
                     round(statistics.mean([r['multi'] for r in sub]), 1)])

    return dict(
        file=name, span=round(el[-1], 1), n=len(rows),
        rate=round(1.0 / nominal, 1), nominal=round(nominal, 3), crc_bad=bad,
        counts={k: len(v) for k, v in H.items()}, other=dict(other),
        gaps=gaps, rebases=reb,
        tstat_dist=dict(collections.Counter(r['ts'] for r in rows)),
        ptype_dist=dict(collections.Counter(pts)),
        cat_pct={str(k): round(100 * v / len(cats), 1) for k, v in collections.Counter(cats).items()},
        first=f, stages=stages, events=events, segs=segs,
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


def env_cn(d):
    return '室内' if 'indoor' in d['file'].lower() else '室外'


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


def make_images(data, out_dir):
    plt = setup_mpl()
    paths = {}

    # 图1：四个关键时刻（按文件名排序，每个文件一根）
    order = sorted(data, key=lambda d: d['file'])
    labels = [short_label(d['file'], 14) for d in order]
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
    ax.set_title('华测冷启动关键时刻对比（t=0 为日志起点）', fontsize=13)
    ax.grid(axis='y', ls='--', alpha=0.4)
    ax.legend(loc='upper left', ncols=4, fontsize=9, framealpha=0.95)
    fig.tight_layout()
    paths['keytime'] = os.path.join(out_dir, 'img_关键时刻.png')
    fig.savefig(paths['keytime'], dpi=150)
    plt.close(fig)

    # 图2：冷启动耗时分解（堆叠条形，每个文件一根）
    fig, ax = plt.subplots(figsize=(13.5, 5.2))
    stage_def = [('no_time', '无时间(UNKNOWN)', '#dc2626'),
                 ('time_to_single', '时间→单点', '#f59e0b'),
                 ('single_to_float', '单点→浮点', '#3b82f6'),
                 ('float_to_fixed', '浮点→固定', '#16a34a')]
    for i, d in enumerate(order):
        left = 0.0
        for key, lab, col in stage_def:
            v = d['stages'][key] or 0
            if v > 0:
                ax.barh(i, v, left=left, color=col, edgecolor='white', height=0.55)
                if v >= 14:
                    ax.text(left + v / 2, i, f'{lab} {v:.1f}s', ha='center', va='center',
                            color='white', fontsize=8.5)
                elif v > 0:
                    ax.text(left + v / 2, i - 0.42, f'{v:.1f}s', ha='center', va='bottom',
                            color=col, fontsize=7.5)
                left += v
        if d['first']['fixed'] is not None:
            ax.text(left + 3, i, f"首固定 {d['first']['fixed']:.1f}s", va='center', fontsize=9,
                    color='#111827')
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel('时间（秒）')
    ax.set_title('华测冷启动耗时分解（无时间 / 时间→单点 / 单点→浮点 / 浮点→固定）', fontsize=13)
    ax.grid(axis='x', ls='--', alpha=0.4)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in stage_def]
    ax.legend(handles, [l for _, l, _ in stage_def], loc='lower right', ncols=4, fontsize=9)
    fig.tight_layout()
    paths['stages'] = os.path.join(out_dir, 'img_阶段耗时.png')
    fig.savefig(paths['stages'], dpi=150)
    plt.close(fig)

    # 图3：卫星数量时间轴（每个选中的文件一张子图，含关键事件虚线）
    ncol = 1
    nrow = len(data)
    fig, axes = plt.subplots(nrow, ncol, figsize=(14, 5.4 * nrow), squeeze=False)
    for idx, d in enumerate(sorted(data, key=lambda d: d['file'])):
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
        ax.set_title(f"{d['file']}（{env_cn(d)}冷启动，全程 {d['span']}s）", fontsize=10.5)
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
# 结论（数据驱动，覆盖 indoor/outdoor 两种场景）
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
<h1>华测（HUACE M720）GNSS 冷启动策略分析（室内 / 室外冷启动）</h1>
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
            f"<td>BESTPOSA {d['n']}条</td><td>{d['nominal']}s/{d['rate']}Hz</td>"
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
        st = d['stages']
        seg = [('无时间', st['no_time'] or 0), ('时间→单点', st['time_to_single'] or 0),
               ('单点→浮点', st['single_to_float'] or 0), ('浮点→固定', st['float_to_fixed'] or 0)]
        tot = sum(v for _, v in seg) or 1
        parts = ''.join(f"<div style='width:{v/tot*100:.2f}%;background:{STAGE_COLORS[i]}'>{n} {v}s</div>"
                        for i, (n, v) in enumerate(seg) if v > 0)
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
    md.append('# 华测（HUACE M720）GNSS 冷启动策略分析（室内 / 室外冷启动）')
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
        md.append(f"| `{d['file']}` | {d['span']}s | BESTPOSA {d['n']}条 | {d['nominal']}s/{d['rate']}Hz | `{d['tstat_dist']}` | `{d['ptype_dist']}` "
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
    md.append('| 文件 | 无时间 | 时间→单点 | 单点→浮点 | 浮点→固定 | 合计到首固定 | 差分数据可用 |')
    md.append('|---|---|---|---|---|---|---|')
    for d in data:
        st = d['stages']
        md.append(f"| `{d['file']}` | {V(st['no_time'])} | {V(st['time_to_single'])} "
                  f"| {V(st['single_to_float'])} | {V(st['float_to_fixed'])} "
                  f"| **{V(d['first']['fixed'])}** | {V(d['first']['stn'])} |")
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


def run(paths, out_dir=None, open_browser=False):
    """分析一组华测 COM1 ASCII 日志（#BESTPOSA），输出 HTML/MD/PNG/JSON 到 out_dir。
    返回 (html_path, md_path)。"""
    if not paths:
        raise SystemExit('未提供日志文件')
    if out_dir is None:
        out_dir = DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    data = [build(p) for p in sorted(paths)]
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
    if len(sys.argv) < 3:
        print(__doc__)
        print('用法: python BY_coldstart.py <输出目录> <日志1> [日志2 ...]')
        raise SystemExit(2)
    out_dir = sys.argv[1]
    paths = [a for a in sys.argv[2:] if os.path.isfile(a)]
    run(paths, out_dir, open_browser=False)


if __name__ == '__main__':
    main()
