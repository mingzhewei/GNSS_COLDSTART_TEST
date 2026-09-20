# GNSS_COLDSTART_TEST

GNSS 冷启动定位收敛分析工具，支持 **北云 BY（UG016 组合惯导）** 与 **华测 HUACE（M720 模组）**，提供 HMI 界面与自包含 HTML 报告。

## 功能

- **HMI 图形界面**：下拉先选产品类型（北云/华测），再多选文件加入分析组；文件列表两列显示（文件名 / 产品类型）；添加时用报文特征自动校验类型（不符跳过并提示）。支持室内/室外冷启动模式与"一次性录制 N 次冷启动"切片模式选择。
- **冷启动切片**（`coldstart_split.py`，静态确定性函数）：连续录制文件自动识别多次冷启动并逐段分析。
  - 北云：以 GPS 周回落（默认周→真实周）识别启动；有效冷启动要求首帧 Time Status = UNKNOWN（时钟全丢）。
  - 华测：以录制间断（>5×标称周期）识别启动；有效冷启动要求首帧 Time Status ∈ {UNKNOWN, APPROXIMATE}。
- **室内 / 室外两种口径**：
  - 室外冷启动：t=0 为启动时刻（日志起点），全程计入评估。
  - 室内冷启动：同序号北云段+华测段配对，取两家最早"拿到时标"的时刻为共同 t0，t0 前数据（室内移动过程）丢弃，各自统计四阶段，出一份联合对比报告（`indoor_coldstart.py`）。
- **报告固定结构**：数据完整性与报文构成（含按报头时间戳判定的实测周期/真实间断/周重定标）/ 四个关键时刻 / 冷启动耗时分解 / 定位类型分段统计 / 附录口径对应表。

## 文件说明

| 文件 | 说明 |
|---|---|
| `gnss_coldstart_hmi.py` | HMI 界面（tkinter），双击运行 |
| `BY_coldstart.py` | 北云分析核心（报文 `#BESTGNSSPOSA`，依据 UG016 手册） |
| `HUACE_coldstart.py` | 华测分析核心（报文 `#BESTPOSA`，依据 M7 系列手册 V2.7） |
| `coldstart_split.py` | 冷启动切片（静态函数，非大模型） |
| `indoor_coldstart.py` | 室内冷启动联合分析（两家配对对齐 t0） |
| `by_data/` `huace_data/` | 示例数据（连续录制，各含 9 次干净冷启动） |
| `by_manual/` `huace_manual/` | 手册（UG016.md / M7 系列 PDF） |
| `reports/` `reports_huace/` | 最近一次分析产物 |

## 使用

```bash
# HMI（推荐）
python gnss_coldstart_hmi.py

# 命令行：室外（各产品独立）
python BY_coldstart.py    <输出目录> [--split] <日志...>
python HUACE_coldstart.py <输出目录> [--split] <日志...>

# 命令行：室内（两家配对，出联合报告）
python indoor_coldstart.py <输出目录> [--split] <北云文件> <华测文件>

# --split：一次性录制了 N 次冷启动（自动切片逐段分析）；不加则按独立冷启动包
```

## 字段口径（均核对各自手册原文，非猜测）

- **北云**：报文 `#BESTGNSSPOSA`；Time Status（UNKNOWN/COARSE/FINESTEERING）；定位类型表 4-2（SINGLE=16 / NARROW_FLOAT=34 / NARROW_INT=50）；卫星 #SVs/#solnSVs/#solnMultiSVs（4.2.2 字段 15/16/18）；差分可用 = Stn ID ≠ "0"
- **华测**：报文 `#BESTPOSA`（Message ID 3020，3.2.14）；Time Status 表 3-19（UNKNOWN=0 / APPROXIMATE=1 / COARSE=3 / COARSESTEERING=4 / FINE=7 / FINESTEERING=9）；定位类型表 3-40（SINGLE=1 / NARROW_FLOAT=5 / NARROW_INT=4）；卫星字段 3.2.14 字段 15/16/17；Stn ID 字段 24（单点时为空串）
- 两产品卫星字段含义与单位一致；CRC 均为 "#"=32 位 CRC、"$"=NMEA 异或
- 周期判定：标称周期 = 全部相邻帧报头时间差的中位数（不假设）；>3600s 判定为周重定标（按连续处理），2.5×周期~3600s 判定为真实间断（保留）
- "拿到时标"（室内模式 t0 判定）：北云 Time Status 首次 ≠ UNKNOWN；华测 首次 ∉ {UNKNOWN, APPROXIMATE}

> 说明：华测数据中出现的定位类型 `PSRDIFF` 在 M7 手册表 3-40 中无字面量（最近义为 SPPDIFF=2 伪距差分解），实测其 Stn ID 有值、差分龄期>0，代码按行业通用"伪距差分"归入单点类，待华测官方定义确认后修正。

## 依赖

- Python 3.9+，仅标准库 + `matplotlib`
