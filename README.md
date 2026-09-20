# GNSS_COLDSTART_TEST

GNSS 冷启动（室内 / 室外）定位收敛分析工具，当前支持 **北云 BY（UG016 组合惯导）** 与 **华测 HUACE（M720 模组）** 两类产品，提供统一的 HMI 界面与自包含 HTML 报告。

## 功能

- HMI 图形界面：按产品多选数据文件加入同一分析组，一次性输出 HTML / MD / 图表 / JSON 摘要
- 命令行模式：便于脚本化批量处理
- 报告固定结构：
  1. 数据完整性与报文构成（含"报文完整性与周期"：按报头时间戳逐帧判定实测周期 / 真实间断 / 周重定标）
  2. 四个关键时刻（获得卫星时间 / COARSE→FINE / 差分可用 / 首单点 / 首浮点 / 首固定）
  3. 冷启动耗时分解（无时间 / 时间→单点 / 单点→浮点 / 浮点→固定）
  4. 定位类型分段统计
  5. 附录：口径与手册条款对应

## 文件说明

| 文件 | 说明 |
|---|---|
| `gnss_coldstart_hmi.py` | HMI 界面（tkinter），双击运行；产品下拉选 北云/华测 |
| `BY_coldstart.py` | 北云分析核心（报文 `#BESTGNSSPOSA`，依据 UG016 手册） |
| `HUACE_coldstart.py` | 华测分析核心（报文 `#BESTPOSA`，依据 M7 系列手册 V2.7） |
| `by_data/` `huace_data/` | 示例数据（COM1 ASCII `.log`） |
| `by_manual/` `huace_manual/` | 手册（UG016.md / M7 系列 PDF） |
| `reports/` `reports_huace/` | 最近一次分析产物（report.html / report.md / img_*.png / summary.json） |

## 使用

```bash
# 方式一：HMI（推荐）
python gnss_coldstart_hmi.py

# 方式二：命令行
python BY_coldstart.py    <输出目录> <日志1> [日志2 ...]
python HUACE_coldstart.py <输出目录> <日志1> [日志2 ...]
```

## 字段口径（均核对各自手册原文，非猜测）

- **北云**：报文 `#BESTGNSSPOSA`；Time Status（UNKNOWN/COARSE/FINESTEERING）；定位类型表 4-2（SINGLE=16 / NARROW_FLOAT=34 / NARROW_INT=50）；卫星 #SVs/#solnSVs/#solnMultiSVs（4.2.2 字段 15/16/18）；差分可用 = Stn ID ≠ "0"
- **华测**：报文 `#BESTPOSA`（Message ID 3020，3.2.14）；Time Status 表 3-19（UNKNOWN=0 / APPROXIMATE=1 / COARSE=3 / COARSESTEERING=4 / FINE=7 / FINESTEERING=9）；定位类型表 3-40（SINGLE=1 / NARROW_FLOAT=5 / NARROW_INT=4）；卫星字段 3.2.14 字段 15/16/17；Stn ID 字段 24（单点时为空串）
- 两产品卫星字段含义与单位一致；CRC 均为 "#"=32 位 CRC、"$"=NMEA 异或
- 周期判定：标称周期 = 全部相邻帧报头时间差的中位数（不假设）；>3600s 判定为周重定标（按连续处理），2.5×周期~3600s 判定为真实间断（保留）

> 说明：华测数据中出现的定位类型 `PSRDIFF` 在 M7 手册表 3-40 中无字面量（最近义为 SPPDIFF=2 伪距差分解），实测其 Stn ID 有值、差分龄期>0，代码按行业通用"伪距差分"归入单点类，待华测官方定义确认后修正。

## 依赖

- Python 3.9+，仅标准库 + `matplotlib`
