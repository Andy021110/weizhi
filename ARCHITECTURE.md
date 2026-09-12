# 微知 WeiZhi · 系统架构

> 更新：2026-09-12 · 与代码同步的架构快照

## 1. 总览

单用户、自托管的个人学习系统。Python 标准库实现（不引框架），前端单文件原生 JS，数据落在单个 SQLite 文件，跑在一台 2C2G 的香港轻量服务器上。

五个进程，互不阻塞（reader 常驻，其余由 cron 触发）：

| 进程 | 启动方式 | 频率 |
|---|---|---|
| `produce/pipeline.py` | cron | 6:00 / 18:00 |
| `serve/reader.py` | systemd 常驻 | 监听 127.0.0.1:8000 |
| `ops/daily_check.py` | cron | 3:30 |
| `ops/daily_agent.py` | cron | 3:35 |
| `ops/v2_shadow.py` | cron | 4:00（`--limit 1`）|

**代码与数据是分开的。** `weizhi/` 是代码包，`config.json`、`weizhi.db`、`quality_reports/`、`backups/` 留在部署根目录。`weizhi/core/paths.py` 提供 `data_dir()` 和 `web_dir()` 两个函数，所有路径引用都走它。这样整理代码目录不需要迁移任何数据。

### 数据流

```
抓取（8 源，ETag 增量）
  → 候选池（不再「来几篇产几篇」）
  → 程序筛：时效窗口 + 同题去重 + 按来源级别排序 + 截断
  → 模型筛：一次调用挑出今天读什么，附理由
  → 证据抽取：从原文确定性地抽出可定位的事实（v2_sources / v2_claims）
  → 受约束写作：只喂已定位的事实，逐段标引用编号
  → 写作门禁：长度、结构、信息密度，不过就不入库
  → 桥接成 v1 卡片入库（cards）
  → 阅读 / 间隔复习（SM-2）/ 事件埋点
  → 每日巡检打分 → 只产出「修订候选」，采纳与否由人决定（可回滚）
```

## 2. 目录与分层

```
weizhi/
├── weizhi/
│   ├── core/        基础设施
│   │   ├── db.py            全部 SQL 封装 + 建表 + 增量迁移
│   │   ├── providers.py     模型统一出口：schema 校验 / 重试 / 调用审计
│   │   ├── schema_v2.py     v2 数据的版本化结构与校验
│   │   ├── prompts.py       全部 prompt 模板（6 类卡 + 分类/消歧 + 巡检/管家）
│   │   ├── notifications.py 通知策略：只有三类允许出现在用户面前
│   │   └── paths.py         代码与数据的路径约定
│   ├── produce/     采集与产卡
│   │   ├── pipeline.py      主产线：candidate pool / pretriage / rank / 产卡
│   │   ├── news.py          信源级别、时效档、事件归一
│   │   ├── evidence.py      确定性证据抽取（v2_sources / v2_claims）
│   │   ├── card_writer.py   受证据约束的写作 + 写作门禁
│   │   ├── card_gates.py    卡片质量门禁规则
│   │   ├── assessment.py    题目生成与延迟复习闭环
│   │   ├── visual.py        语义配图（规划 + 校验 + 逐张取舍）
│   │   └── bridge_v1.py     v2 草稿 → v1 卡片（含来源级别与权威度）
│   ├── serve/       阅读与复习
│   │   ├── reader.py        HTTP API + 业务编排（含读写分权）
│   │   ├── review_flow.py   复习流程：到期 → 作答 → 判分 → 掌握度
│   │   ├── mastery.py       掌握度与遗忘风险
│   │   ├── planner.py       最小学习包规划
│   │   ├── goalspec.py      目标诊断
│   │   └── web/             前端资源（reader.html / manifest / sw.js / icon）
│   └── ops/         质量与编排
│       ├── daily_check.py   巡检：规则 + AI 打分 → 修订候选
│       ├── daily_agent.py   编排：LLM 决策 → 白名单校验 → 通知
│       ├── revisions.py     修订候选的审核与落地
│       └── v2_shadow.py     影子链路，用来对比新流程产出
├── tools/           开发与一次性脚本（python -m tools.<name>）
├── deploy/          deploy.py / deploy.sh / systemd / nginx
├── docs/            产品与设计文档 21 篇 + 索引
└── tests/           578 个测试
```

## 3. 两条产卡路径

理解这个系统的关键在这里：**入口是 v1，产卡默认走证据链**。

- **证据路径（默认）**：`pipeline.py` 的 `_run_pick` 收集候选 → 两级筛选 → 对选中的每篇调 `evidence.ingest_source()` 抽事实 → `card_writer.write_card_gated()` 受约束写作 → 门禁通过后由 `bridge_v1` 组装成 v1 卡片结构入库，并把来源级别写进 `credibility`。
- **旧路径（回退）**：`_run_legacy` 把整篇正文塞给模型直接出卡，无证据约束。只有加 `--legacy` 才走，用于证据链出问题时的应急。
- **影子链路**：`v2_shadow.py` 每天 4:00 独立产一张，落在 `v2_card_drafts`，不进入用户的推送。它的作用是提供"同一批材料，两条流程各产出什么"的对照。

材料抽不出足够事实（`MIN_CLAIMS_FOR_PACK = 2`）就不产卡——宁可当天少一张。

## 4. 数据模型（SQLite: weizhi.db，18 张表）

**阅读侧**

| 表 | 用途 | 关键字段 |
|---|---|---|
| `cards` | 知识卡主表 | title / body / summary / quiz / review_quiz / template / timeliness / credibility / published / plan_id / `_gen_input` |
| `plans` | 学习计划 | title / template / status / total_cards / batch |
| `progress` | 学习记录 | date / card_source_url / done（日历数据源）|
| `reviews` | 复习记录 | card_source_url / review_date / result / interval_before / interval_after |
| `events` | 行为埋点 | date / event / source_url（画像与兴趣分数据源）|
| `notifications` | 管家通知 | date / type / title / body / level / read |
| `ledger_concepts` | 阅读待学台账 | norm_id / term / one_line / why_matters / depends_on / count / first_seen / last_seen |
| `user_state` | KV 状态 | 学习天数 / 增量指纹 `seen_global` / 各类游标与开关 |

**证据与产卡侧**

| 表 | 用途 | 关键字段 |
|---|---|---|
| `v2_sources` | 被抓取的材料 | url / title / site / content_hash / snapshot_path / clean_text / `meta`（来源 id、级别、主题、发布时间）|
| `v2_claims` | 从材料抽出的事实 | source_id / claim_idx / text / start / end / kind / usable |
| `v2_card_drafts` | v2 草稿 | input_hash / objective / capability_gap / figures / assessment / status / payload / gate_report |
| `v2_model_calls` | 调用审计 | task / provider / model / prompt_version / input_hash / latency_ms / tokens / cost / retries / error / cached |

**学习与治理侧**

| 表 | 用途 | 关键字段 |
|---|---|---|
| `v2_goals` | 目标规格 | goal_key / version / raw_input / spec / status |
| `v2_learning_packs` | 学习包 | goal_key / milestone_id / status / card_ids / entry_count / failed |
| `v2_mastery` | 掌握度 | goal_key / concept / score / attempts / correct / interval_days / next_review_at |
| `v2_review_log` | 复习明细 | goal_key / concept / question_idx / correct / confidence / error_type |
| `card_revisions` | 修订候选 | source_url / status / reason / origin / payload / prev_payload / decided_at |
| `demo_access` | 只读访客流水 | ts / ip / method / path / blocked / ua（最多留 2000 条）|

## 5. 关键设计决策

1. **推荐排序**：`score = 新鲜度 × 源权威权重 × (1 + 兴趣分/10)`，已读卡 ×0.3 沉底。权威权重取 `CRED_WEIGHT`：官方 1.2、权威媒体 1.0、专业机构 1.0、专业博客 0.9、自媒体 0.7。
2. **自动修复护栏**：AI 打分 ≤2.0 或规则可修才自动；2–3 分留给人工拍板。备份文件在 regen 成功后**补写 new_source_url**，否则回滚删不掉新卡（第一版踩过的坑）。
3. **类型识别合并消歧**：一次调用同时输出类型和歧义方向，省一次请求；「AI 自动」默认选中，可手动指定。
4. **时效规则防误报**：仅「有真实 http 来源」的 fast / event 卡强制要求 published，topic 模式的卡不强制。
5. **通知矛盾规避**：修复成功的卡进「已自动修复」，没修的才进「待拍板」；通知文案走 type / level / 长度白名单。
6. **证据约束**：事实先确定性抽取、再写作，正文逐段标引用编号。抽不出足够事实就不产卡。代价是慢和浪费材料，换来的是可核对。
7. **候选池 + 两级筛选**：程序筛管能算出来的（时效、去重、来源级别），模型筛管算不出来的（值不值得读，并给理由）。这样各源的更新频率不再直接决定今天读什么。
8. **时效窗口按内容寿命分档**：`LOOKBACK_HOURS_BY_TIER` 里官方公告 / 论文 / 媒体 7 天，博客 / 分析通讯 30 天；单个源可在 config 里写 `lookback_hours` 覆盖。优先级：源级 > 级别 > 全局。
9. **「见过」不等于「产过」**：指纹只在有结论时写（被挑中处理过，或已超出时效窗口）。没被挑中但还在窗口内的留在池里等下一轮。指纹库容量 `SEEN_CAP = 3000`。旧的实现是抓到就写，等于把没轮到的条目永久丢弃。
10. **质量修复只出候选**：巡检可以自动重新生成，但不静默改已发布的卡；每条修复留记录、可回滚。
11. **读写分权**：`access_token` 是主人，`demo_token` 只读。写锁放在 `do_POST` 的统一入口而不是逐个接口里——逐个判的话，新增接口忘一次就是「演示用户也能改数据」，而且不报错。唯一例外是 `/api/verify`（拿口令换角色的入口本身）。

## 6. 成本模型

按 2026-09-11～09-12 的调用审计折算：

| 项 | 数值 |
|---|---|
| 模型调用 | 75 次，输入 19.4 万 token，输出 4.2 万 token |
| 平均延迟 | 3.6 秒 |
| 模型成本 | 约 ¥0.2/天，即 ¥6/月上下 |
| 服务器（香港 2核2G）| ¥40–60/月 |

口径说明：审计只覆盖走 `providers.py` 统一接口的调用，巡检与管家那部分还没接进来，所以是折算值不是账单；其中 09-12 还包含当天的几次手动测试运行，正常日可能更低。早期文档里「内容生成 ¥8–15/月」是接入审计之前的估算，量级一致。

## 7. 演进路径

| 阶段 | 状态 |
|---|---|
| 起点：AI 流水线 + 手动决策 | 已超越 |
| 反馈闭环（巡检 → 修复 → 回滚）| 已上线 |
| 管家 Agent（LLM 编排 + 程序内通知 + 荐读）| 已上线 |
| 证据约束产卡（v1 前端不变，产线换内核）| 已上线，已接管默认路径 |
| 只读演示与读写分权 | 已上线 |
| 学习目标与掌握度（`v2_goals` / `v2_mastery` / `v2_learning_packs`）| 数据层已建，前端未接管 |
| 产品自身的成功标准埋点（读完判断有没有变）| **未开始** |

最后一条是当前最大的缺口：现有的全部质量指标衡量的都是**产出**，不是**效果**。另外两个已知未修的：候选池偏小时排序会系统性挤压深度源（先按来源级别排再截断）；调用审计覆盖不全，成本只能折算。
