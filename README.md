# 微知 WeiZhi

**单用户自托管 MVP** · 为 AI 从业者打造的个人学习管家：自动抓取、生成、推荐、复习、自愈——一条无人值守就能完整运转的学习闭环。非生产级平台，定位是个人知识管理 + 学习自动化。

中文 | [English](README.en.md)

![Python](https://img.shields.io/badge/Python-3.10-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![PWA](https://img.shields.io/badge/PWA-ready-orange) ![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)

## 简介

微知是一个自托管的学习系统，把碎片时间转化为真正的知识沉淀：

- **摄取** — 8 个精选 RSS 源聚合（官方博客、arXiv、深度技术博客）
- **生成** — DeepSeek 产出 6 类结构化知识卡（词汇 / 精读 / 数学 / 通识 / 技能 / 代码）
- **推荐** — 每日 Top 3 荐食（带推荐理由），按新鲜度 × 来源权威 × 个人兴趣排序
- **复习** — SM-2 间隔重复，含随堂测验、思考题、AI 批改的简答题
- **自愈** — 每日质量巡检（规则检查 + AI 打分），坏卡自动重新生成，带备份与回滚

系统以 ReAct 流水线方式运行：观察 → 思考（LLM 决策）→ 行动 → 汇报，仅在需要人工判断时停下。

## 特性

- 建计划时自动识别类型（无需手动选择模板）
- 权威与时效体系：4 档时效（稳定 / 演进 / 快变 / 时点）× 4 级权威（A-D），卡片带过期提示
- 学习画像：兴趣主题、薄弱卡（连续记错 ≥2 次）、复习准确率——回喂推荐与每日荐食
- 程序内通知（铃铛 + 弹窗）：待拍板事项、复习拖欠、断签预警、每周报告
- 学习仪表盘：知识概览 / 质量趋势（7 天通过率）/ 问题类型分布 / 修复记录（可回滚）可视化
- 增量抓取（ETag 条件请求）、跨源 SimHash 去重（一手来源优先）、失败指数退避重试
- PWA 前端（可添加到桌面），单文件原生 JS，零构建
- 全部智能层月成本 < ¥10（DeepSeek API）；整套部署在一台 2C2G 香港轻量服务器

## 架构

```
┌───────────── 感知层 ─────────────┐
│  pipeline.py（cron 6:00/18:00）  │
│  抓取 → 去重 → 生成卡片           │
└─────────────────────────────────┘
                   ▼
┌───────────── 行动层 ─────────────┐
│  reader.py（常驻服务）            │
│  增删改查 / SM-2 / 排序 / 画像     │
│  reader.html（PWA 前端）          │
└─────────────────────────────────┘
                   ▼
┌───────────── 反馈层 ─────────────┐
│  daily_check.py（cron 3:30）     │
│  规则 + AI 打分 → 自动修复+备份    │
└─────────────────────────────────┘
                   ▼
┌───────────── 思考层 ─────────────┐
│  daily_agent.py（cron 3:35）     │
│  LLM 决策：通知 / 修复 / 荐食      │
└─────────────────────────────────┘
```

模块：`reader.py`（HTTP API + 业务逻辑）· `pipeline.py`（抓取管线）· `daily_check.py`（质量巡检）· `daily_agent.py`（编排层）· `db.py`（SQLite 数据层）· `prompts.py`（模板与分类）

数据模型、设计决策与成本模型见 [ARCHITECTURE.md](ARCHITECTURE.md)（[English](ARCHITECTURE.en.md)）。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # 填入 DeepSeek API key
python reader.py                      # 打开 http://localhost:8000/?key=<你的token>

python pipeline.py --limit 2          # 手动抓一轮 RSS 并生成卡片
python daily_check.py --dry-run --limit 3   # 预览质量巡检（不落盘）
python daily_agent.py --dry-run       # 预览管家决策（不执行）
```

## 自动化流水线

| 时间（cron）| 任务 | 作用 |
|---|---|---|
| 6:00 / 18:00 | `pipeline.py` | 抓取 8 源 → 生成新卡 |
| 3:30 | `daily_check.py` | 巡检打分 → 自动修复坏卡（备份可回滚）|
| 3:35 | `daily_agent.py` | 管家决策 → 通知（荐食 / 待拍板 / 周报）|

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 39 个单测，约 1s，不调用任何 LLM API
```

覆盖：SM-2 间隔算法、SimHash 跨源去重、巡检客观规则、管家 LLM 输出白名单校验（用临时 SQLite 库隔离，不污染真实数据）。

## 目录结构

```
content-pipeline/
├── reader.py            # 主服务（HTTP API + 业务逻辑）
├── reader.html          # PWA 前端，单文件
├── prompts.py           # 6 类模板 + 权威时效 + 类型分类
├── db.py                # SQLite 数据层
├── pipeline.py          # RSS 管线：增量 + 去重 + ETag + 重试
├── daily_check.py       # 质量巡检与自动修复
├── daily_agent.py       # ReAct 编排层
├── config.example.json  # 配置模板（config.json 绝不入库）
├── deploy.sh / nginx.conf / weizhi-reader.service / DEPLOY.md
└── docs/                # 产品与设计文档（中文，14 篇）
```

## 安全说明

- `config.json`（API key / access token）已被 `.gitignore` 排除，公开仓库请使用 `config.example.json` 模板
- 生产环境建议：SSH 密钥登录、fail2ban、服务仅监听 127.0.0.1 并由 nginx 反代

## License

MIT
