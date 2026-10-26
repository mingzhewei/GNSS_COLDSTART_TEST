# MEMORY.md

- [北斗 UM980 冷启动分析口径决策](project_beidou_um980.md) — 已实现并验证；断电时标保持判据、两处“—”规则、室内多产品配对、1023 验证基线
- [北斗×北云室外配对与仓库数据归档决策](project_outdoor_beidou_beiyun.md) — 室外北云基准、HMI 三报告、华测不变、报告可再生、原始数据全量入库
- [架构决策：暂不重构 + 判据脆弱点](project_no_refactor.md) — 重构已否决勿再提议；time_required 错标漏段、瞬态失锁误切为已知风险
- [不要猜测，先确认再动手](feedback_no_guessing.md) — 疑问以选择题确认；重大改动先利弊分析等确认，禁止猜测口径
- [同族参考项目 kinematic/static](reference_sibling_projects.md) — house style 参照（每产品一分析器+统一HMI+打包），但不作本项目结构模板
- [用户与项目背景](user_profile.md) — GNSS 冷启动对比测试（北云/华测/北斗），重手册依据与口径正确
