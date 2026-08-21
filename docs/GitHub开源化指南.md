# 微知 WeiZhi · GitHub 开源化指南

> 归档时间：2026-08-21 · 状态：本地已就绪，待创建远端仓库

## 1. 本地准备（已完成 ✅）

| 项 | 状态 |
|---|---|
| README.md 重写至最新能力全景（类型识别/巡检/管家/画像/推荐/时效/抓取增强）| ✅ |
| ARCHITECTURE.md 架构快照（感知-思考-行动-反馈四环 + 数据模型 + 设计决策）| ✅ |
| LICENSE（MIT）| ✅ |
| .gitignore（config.json / *.db / quality_reports/ / backups/ / logs）| ✅ |
| 敏感扫描：当前追踪文件 ✅ 历史提交 ✅ 均无密钥/DB/报告 | ✅ |

## 2. 创建远端仓库

### 方式 A：网页创建（推荐，无需 API token）

1. 浏览器打开 https://github.com/new
2. Owner 选个人账号（Andy021110），Repository name 填 `weizhi`（或你喜欢的名字）
3. 可见性：选 **Private**（自用/求职展示）或 **Public**（开源）
4. **不要**勾选 "Add a README / .gitignore / license"（本地已有，勾了会冲突）
5. 点 Create repository

### 方式 B：API 创建（需 PAT）

```bash
# 先在 GitHub Settings → Developer settings → Personal access tokens 生成 token（repo 权限）
curl -X POST -H "Authorization: token 你的PAT" \
  -d '{"name":"weizhi","private":true}' \
  https://api.github.com/user/repos
```

## 3. 推送（仓库建好后执行）

```bash
cd /Users/minghan/WorkBuddy/2026-08-12-14-09-12/content-pipeline
git remote add origin git@github.com:Andy021110/weizhi.git   # SSH 已认证
git push -u origin main
```

验证：`git remote -v` 显示 origin；浏览器打开仓库页能看到全部文件与 17 个 commit。

## 4. 可选增强（按需）

### 4.1 GitHub Pages 在线演示
前端是纯静态单页 `reader.html`（PWA），可直接发布：

```bash
# 方式：Settings → Pages → Source 选 main 分支 → 指定 docs/ 或根目录
# 注意：演示页的 API 会连不上（无后端），仅展示 UI 静态效果
# 更推荐：部署 docs/卡片案例展示.html 作为「产品截图式」落地页
```

### 4.2 README 徽章（可选）
```markdown
![Python](https://img.shields.io/badge/Python-3.10-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![PWA](https://img.shields.io/badge/PWA-ready-orange)
```

### 4.3 敏感信息复查（每次 push 前）
```bash
git ls-files | grep -iE "config\.json$|\.db$|\.log$|quality_reports|backups" || echo "安全"
```

## 5. 求职展示建议

- 仓库 README 第一屏就放「能力全景表 + 架构图」——面试官 30 秒看懂你的系统
- 把 `docs/产品定位与内容策略.md`、`docs/质量与体验监控体系方案.md` 当作「产品+工程双修的证明」
- 可加一段 Demo 视频/GIF（录浏览器操作：建计划→自动识别类型→学习→复习→看质检报告）
