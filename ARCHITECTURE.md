# 微知 WeiZhi · 系统架构

> 归档时间：2026-08-21 · 与代码同步的架构快照

## 1. 总览

微知是一个「个人学习管家 Agent」：**感知 → 思考 → 行动 → 反馈** 四环自动化的单用户学习系统。所有服务用 Python 标准库实现（零第三方框架），前端单文件原生 JS，数据落在单个 SQLite 文件，部署在单台香港轻量服务器上。

```
┌────────────────────────────── 感知层 ──────────────────────────────┐
│  pipeline.py（6:00/18:00 cron）                                        │
│    RSS 抓取 → ETag 条件请求 → 增量过滤(seen 指纹) → SimHash 跨源去重      │
│    → DeepSeek 生成卡片（6 类模板 + 权威时效四档字段）                     │
└───────────────────────────────────────────────────────────────────┘
                               ▼ 写入 cards 表
┌────────────────────────────── 行动层 ──────────────────────────────┐
│  reader.py（systemd 常驻服务，127.0.0.1:8000）                          │
│    卡片 CRUD / SM-2 复习调度 / 推荐排序(新鲜度×权威×兴趣) / 画像计算      │
│    classify(类型识别) / regen(重生成) / rollback(回滚) / notifications   │
│  reader.html（PWA 前端，浏览器访问）                                     │
│    列表/详情/测验/简答/复习/任务/日历/统计/通知中心/质检报告                │
└───────────────────────────────────────────────────────────────────┘
                               ▼ 每日快照
┌────────────────────────────── 反馈层 ──────────────────────────────┐
│  daily_check.py（3:30 cron）                                          │
│    客观规则(字段/长度/重复/时效) + DeepSeek 4 维打分(准确/相关/深度/结构)  │
│    → badcase 清单 → 自动修复(阈值≤2.0/规则可修，每轮≤3，同卡≤2 次)        │
│    → 备份(backups/日期/md5.json 含旧卡+进度+new_source_url)            │
└───────────────────────────────────────────────────────────────────┘
                               ▼ 报告 + 信号
┌────────────────────────────── 思考层 ──────────────────────────────┐
│  daily_agent.py（3:35 cron）                                          │
│    读报告+规则信号(待拍板/拖欠/断签/下滑/过时/薄弱)                      │
│    → LLM 决策(通知文案/修复清单/荐食 Top3/周报) → 白名单校验              │
│    → 执行(自动修复/写 notifications 表) → LLM 失败降级规则兜底           │
└───────────────────────────────────────────────────────────────────┘
                               ▼ 程序内通知（铃铛+弹窗）
                             用户（只处理需拍板事项）
```

## 2. 数据模型（SQLite: weizhi.db）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `cards` | 知识卡主表 | title/body/summary/think_*/quiz/review_quiz/template/timeliness/credibility/published/author/plan_id/plan_index/_meta/_gen_input |
| `plans` | 学习计划 | title/template/status(active/paused/done)/total_cards/batch |
| `progress` | 学习记录 | date/card_source_url/done（日历打卡数据源）|
| `reviews` | 复习记录 | card_source_url/review_date/result/interval_before/interval_after（SM-2 轨迹）|
| `events` | 行为埋点 | date/event/card_open·think_open·open_submit·calendar_open·search_use/source_url（画像与兴趣分数据源）|
| `notifications` | 管家通知 | date/type/title/body/level(info/warn/action)/read |
| `user_state` | KV 状态 | streak/last_active_date/seen_{source}(增量抓取指纹)/计划生成游标 |

## 3. 核心模块职责

| 模块 | 职责边界 |
|---|---|
| `reader.py` | HTTP API + 业务编排。生成卡时把输入快照写入 `_gen_input`，供 regen 重建输入；regen 保留 plan_id/plan_index 不脱离原计划 |
| `db.py` | 全部 SQL 封装：建表、CRUD、统计（画像/兴趣分/合规率/掌握率）、查重（标题归一化 + SimHash 指纹）|
| `pipeline.py` | 抓取管线。`filter_fresh` 增量（seen 指纹保留 300 条）；`_article_sim` 摘要级 SimHash（只算摘要不算标题，避免误杀）；一手域名优先替换转载 |
| `prompts.py` | 6 类模板 + 权威时效四档 + 类型分类/消歧 prompt。所有模板输出含 published/author/credibility/timeliness 四字段 |
| `daily_check.py` | 巡检。规则+AI 双通道；AI 打分 temperature=0.2、单卡失败跳过；`_backup_count` 限制同卡修复次数 |
| `daily_agent.py` | 编排层。`rule_signals`(确定性信号) + `think`(LLM) + `validate_decision`(白名单校验) + `execute`(行动) + `fallback`(降级) |

## 4. 关键设计决策

1. **推荐排序公式**：`score = 新鲜度 × 源权威权重 × (1 + 兴趣分/10)`，已读卡 ×0.3 沉底。权威权重：官方 1.2 / 权威媒体 1.0 / 专业博客 0.9 / 自媒体 0.7
2. **自动修复护栏**：AI 打分 ≤2.0 或规则可修才自动；2-3 分留给人工拍板；备份文件在 regen 成功后**补写 new_source_url**（否则回滚删不掉新卡——第一版踩过的坑）
3. **类型识别合并消歧**：一次 LLM 调用同时输出类型 + 歧义方向，省一次请求；「AI 自动」默认选中，可手动强制指定
4. **时效规则防误报**：仅「有真实 http 来源」的 fast/event 卡强制要求 published；topic 模式卡（无原文）不强制——第一版误伤后的修正
5. **通知矛盾规避**：修复成功的卡进「已自动修复」，没修的才进「待拍板」；LLM 通知文案走 type/level/长度白名单

## 5. Agent 演进路径

| 阶段 | 状态 |
|---|---|
| 起点：AI 流水线 + 手动决策 | 已超越 |
| 反馈闭环（巡检→修复→回滚）| 已上线（M1-M3）|
| 管家 Agent 初版（LLM 编排 + 程序内通知 + 荐食）| 已上线（当前）|
| 完整版：Web Push 推送 / 学习节奏自适应 / 目标层 | 规划中（待数据验证后决策）|

## 6. 成本模型（实测）

- 内容生成（每天 8-16 张）：¥8-15/月（大头，随量增减）
- 全部智能层（巡检/管家/荐食/打分）：< ¥5/月
- 服务器（香港 2核2G）：¥40-60/月
- **总计 ≈ ¥60-95/月**，其中智能层几乎免费，成本由「每天消化多少新内容」决定
