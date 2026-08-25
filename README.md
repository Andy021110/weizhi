# 微知 WeiZhi · Personal AI Learning Steward

> 给 AI 从业者的「碎片化持续学习 + 记忆系统」：既懂前沿、又懂原理、还不忘。
> A self-driving learning system with quality self-healing — your own AI steward.

微知是一个**个人学习管家 Agent**：自动抓取行业内容 → AI 生成结构化知识卡 → 按你的画像推荐 → 间隔复习巩固记忆 → 每天自动巡检质量并自我修复。感知、思考、行动、反馈四环全自动，只有需要你拍板时才停下。

- **形态**：PWA（加到桌面，类小程序体验），手机 / 电脑浏览器直接用，免备案服务器部署
- **定位**：不是又一个资讯聚合器，而是「会学习的学习系统」——学过的内容不忘（SM-2 间隔复习）、坏的内容自动修（质量自愈）、每天有管家替你挑值得读的（荐食）

---

## 能力全景

| 层 | 能力 | 说明 |
|---|---|---|
| 🧠 **内容生成** | 6 类卡片模板 | T1 词汇 / T2 精读 / T3 数学 / T4 通识 / T5 技能 / T6 代码，DeepSeek JSON 结构化输出 |
| 🎯 **类型自动识别** | 建计划不用选类型 | AI 一次调用判断主题类型 + 方向歧义，识别错可手动纠正 |
| 📡 **资讯式抓取** | RSS 聚合管线 | 8 个高信号源（官方/arXiv/深度博客）、一天两抓、每源限量、增量过滤、跨源 SimHash 去重（一手优先）、ETag 条件请求、失败重试退避 |
| ⏳ **权威时效体系** | 四档半衰期 | stable / evolving / fast / event 四档时效，A/B/C/D 四级权威度，卡片展示「来源 · 时间 · 权威度 · 过时提示」 |
| 💡 **推荐体系** | 画像驱动排序 | 推荐分 = 新鲜度 × 源权威 × (1+兴趣分)，命中画像主题加权、已读沉底；管家每天荐食 Top3 带「为什么值得读」 |
| 🧑🎓 **学习闭环** | 学 → 测 → 想 → 答 → 复习 | 随堂测 + 思考题 AI 回答 + 简答 AI 评分 + SM-2 间隔复习（1→3→7→15→30 天）+ 打卡日历 |
| 🧩 **学习画像** | Duolingo 式画像 | 兴趣主题 / 类型偏好 / 复习准确率 / 薄弱卡（记错≥2 次），画像回喂推荐与荐食，周日自动周报 |
| 🔧 **质量自愈** | 巡检 → 修复 → 回滚 | 每天 3:30 巡检（客观规则 + AI 4 维打分），badcase 自动重生成（阈值≤2.0、每轮≤3、同卡≤2 次、备份可回滚） |
| 🤖 **管家 Agent** | ReAct 编排层 | `daily_agent.py`：观察（读报告+信号）→ 思考（LLM 决策）→ 行动（自动修复/写通知）→ 汇报（程序内通知），LLM 挂了自动降级规则兜底 |
| 📣 **程序内通知** | 铃铛 + 弹窗 | 页面打开自动弹出未读通知（坏卡待拍板 / 复习拖欠 / 断签 / 荐食 / 周报 / 过时提醒），无需 Web Push |

## 系统架构

```
┌────────────────────────────── 感知层 ──────────────────────────────┐
│  pipeline.py（6:00/18:00 cron）  RSS 抓取 → 增量过滤 → 跨源去重 → 卡片生成   │
└───────────────────────────────────────────────────────────────────┘
                               ▼
┌────────────────────────────── 行动层 ──────────────────────────────┐
│  reader.py（常驻服务）  卡片 CRUD / 复习调度(SM-2) / 推荐排序 / 画像统计   │
│  reader.html（PWA 前端） 列表 / 详情 / 测验 / 复习 / 任务 / 日历 / 通知中心   │
└───────────────────────────────────────────────────────────────────┘
                               ▼
┌────────────────────────────── 反馈层 ──────────────────────────────┐
│  daily_check.py（3:30 cron）  客观规则 + AI 打分 → badcase → 自动修复+备份  │
└───────────────────────────────────────────────────────────────────┘
                               ▼
┌────────────────────────────── 思考层 ──────────────────────────────┐
│  daily_agent.py（3:35 cron）  读报告 → LLM 决策（通知/修复/荐食/周报）→ 执行  │
└───────────────────────────────────────────────────────────────────┘
```

对照 ReAct 范式：**Action / Observation / Tools / Memory 已建成，Thought 由 daily_agent 的 LLM 决策补上**——这是一个完整跑起来的「管家 Agent 初版」。

## 技术栈

- **后端**：Python 3 标准库 `http.server` 单文件常驻服务 + SQLite（零第三方框架依赖）
- **前端**：单文件 `reader.html`（原生 JS，无构建），PWA（manifest + sw.js + 手绘线性 SVG 图标）
- **AI**：DeepSeek API（JSON 结构化输出，低成本：全部智能层月成本 < ¥10）
- **部署**：香港/新加坡轻量服务器 + nginx 反代 + systemd + cron，免备案

## 目录结构

```
content-pipeline/
├── reader.py          # 主服务：HTTP API + 业务逻辑（含 classify/regen/rollback/profile）
├── reader.html        # PWA 前端单页（原生 JS + 手绘 SVG 图标系统）
├── prompts.py         # 6 类模板 prompt + 权威时效四档 + 消歧/分类 prompt
├── db.py              # SQLite 数据层（cards/plans/reviews/progress/events/notifications）
├── pipeline.py        # RSS 抓取管线：增量过滤 + SimHash 跨源去重 + ETag + 重试退避
├── daily_check.py     # 质量巡检：客观规则 + AI 4 维打分 + 自动修复 + 备份
├── daily_agent.py     # 管家编排层（ReAct）：LLM 决策 + 荐食 + 薄弱提醒 + 周日周报
├── migrate.py         # 数据库迁移
├── deploy.sh / nginx.conf / weizhi-reader.service / DEPLOY.md
├── config.example.json  # 配置模板（复制为 config.json 填入 API key）
├── docs/              # 14 篇产品与方案文档（定位 / 验收 / 质量监控 / 权威时效策略等）
└── examples/          # 卡片案例展示 + golden set
```

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # feedparser / trafilatura / openai

cp config.example.json config.json       # 填入 deepseek_api_key 与 access_token
python reader.py                          # 浏览器打开 http://localhost:8000/?key=你的token

python pipeline.py --limit 2              # 手动抓一轮 RSS 并生成卡片
python daily_check.py --dry-run --limit 3 # 试跑质量巡检（不落盘）
python daily_agent.py --dry-run           # 试跑管家决策（不执行）
```

## 自动化流水线（cron）

| 时间 | 任务 | 作用 |
|---|---|---|
| 6:00 / 18:00 | `pipeline.py` | 抓取 8 源 → 生成新卡（增量 + 跨源去重）|
| 3:30 | `daily_check.py` | 巡检打分 → badcase 自动修复 + 备份 |
| 3:35 | `daily_agent.py` | 管家决策 → 通知（荐食 / 待拍板 / 拖欠 / 周报）|

## API 一览（reader.py）

| 接口 | 作用 |
|---|---|
| `GET /api/cards?date=` | 读卡片（推荐排序）|
| `POST /api/plan/classify` | 类型自动识别 + 歧义判断 |
| `POST /api/plan/outline / create / progress` | 建计划（异步生成）|
| `POST /api/done` / `POST /api/review` | 标记已读 / SM-2 复习判分 |
| `POST /api/create` / `POST /api/regen` | 创建单卡 / badcase 重生成（保留计划归属）|
| `POST /api/rollback` | 回滚自动修复（删新卡、恢复旧卡与进度）|
| `GET /api/report/latest` | 最新质检报告 |
| `GET /api/profile` | 学习画像（主题/薄弱卡/准确率）|
| `GET/POST /api/notifications` | 通知中心（列表 / 标记已读）|
| `POST /api/event` | 行为埋点（card_open/think_open/open_submit/calendar_open/search_use）|

## 文档索引

- [产品定位与内容策略](docs/产品定位与内容策略.md)（北极星文档）
- [质量与体验监控体系方案](docs/质量与体验监控体系方案.md)（M1-M3 里程碑）
- [内容权威与时效性策略](docs/内容权威与时效性策略.md)（四档时效 + 四级权威）
- [类型自动识别与质量巡检M1方案](docs/类型自动识别与质量巡检M1方案.md)
- 另有：主题消歧 / 学习计划与每日配额 / 生产配方与 GoldenSet / 测试与验收方案 / 复盘与检验 等 14 篇

## 安全说明

- `config.json`（含 API key / access_token）已被 `.gitignore` 排除，**绝不入库**；公开仓库使用 `config.example.json` 模板
- 生产建议：SSH 密钥登录、fail2ban、后端仅监听 127.0.0.1 + nginx 反代

## License

MIT
