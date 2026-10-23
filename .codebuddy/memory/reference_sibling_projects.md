---
name: 同族参考项目（kinematic / static）
description: gps_test_kinematic 与 gps_test_static 为同族姊妹项目，是“每产品一个分析器 + 统一 HMI/打包”风格的参照来源
type: reference
---

同目录下有两个同族参考项目，风格与本项目相关讨论的参照：

- `D:\software\gps分析程序\gps_test_kinematic` —— 动态（Kinematic）分析；`gps_kinematic_analyzer_beiyun/huace/beidou.py` 每产品一个自包含大文件 + `gnss_kinematic_hmi.py` 统一 HMI + `kinematic_core/`（criteria.py 小共享核）+ PyInstaller `.spec` 打包。
- `D:\software\gps分析程序\gps_test_static` —— 静态（RTK）分析；`gps_rtk_analyzer_beidou/bynav/huace.py` + `gnss_static_hmi.py`，含 `tests/` 与 `Build/`。

**How to apply:** 需要了解“house style”（产品分析器组织方式、HMI 调度/打包写法、报告风格）时去这两个项目对照；但注意 2026-10-23 已决定本项目**不做**同款单体重构（见“架构决策”记忆），参考限于风格与 HMI/打包写法，不作为本项目结构模板。
