#!/usr/bin/env bash
# 微知 · 一键部署脚本（Ubuntu 22.04）
#
# 用法：
#   1. 先修改下面「配置区」的变量（尤其是 APP_DIR / SERVICE_USER / DEEPSEEK_KEY）
#   2. 把整个项目放到 APP_DIR 下
#   3. 执行：bash deploy.sh
#
# 脚本会自动：装系统依赖 → 建 venv → 装 Python 依赖 → 生成 config.json
#            → 安装并启动 systemd 服务 → 写入 cron 定时任务
#
# 说明：本脚本是 DEPLOY.md 的自动化版，执行前请务必确认变量填写正确。

set -euo pipefail

# ==================== 配置区（务必修改） ====================

# 项目所在绝对路径（不要以 / 结尾）
APP_DIR="/opt/weizhi/content-pipeline"

# 运行服务的系统用户名（whoami 可查看，避免用 root）
SERVICE_USER="www-data"

# DeepSeek API key（sk-...），留空则跳过生成 config.json
DEEPSEEK_KEY=""

# 阅读器端口
PORT="8000"

# cron 每天运行时间（24 小时制，默认 6:00）
CRON_HOUR="6"
CRON_MINUTE="0"

# ==================== 配置区结束 ====================

# 颜色输出
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[-]${NC} $1"; }

# 校验配置
if [ -z "${APP_DIR}" ]; then err "APP_DIR 未设置"; exit 1; fi
if [ ! -d "${APP_DIR}" ]; then
  err "项目目录不存在：${APP_DIR}。请先把代码放到该路径，或修改 APP_DIR。"
  exit 1
fi
if [ -z "${SERVICE_USER}" ] || [ "${SERVICE_USER}" = "www-data" ]; then
  warn "SERVICE_USER 还是默认值 www-data，建议改成你的登录用户名。"
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  warn "用户 ${SERVICE_USER} 不存在，systemd 服务可能无法启动。请改成实际存在的用户。"
fi

# 1. 安装系统依赖
info "安装系统依赖（python3 / venv / pip / nginx / curl）..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl nginx

# 2. 建立虚拟环境并安装 Python 依赖
info "创建虚拟环境并安装依赖..."
cd "${APP_DIR}"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. 生成 config.json（若不存在，且提供了 DEEPSEEK_KEY）
if [ ! -f "${APP_DIR}/config.json" ]; then
  if [ -n "${DEEPSEEK_KEY}" ]; then
    info "生成 config.json 并写入 DeepSeek key..."
    cp config.example.json config.json
    # 用 python 安全地替换 key，避免 sed 转义问题
    .venv/bin/python - <<PY
import json
p = "${APP_DIR}/config.json"
with open(p, encoding="utf-8") as f:
    cfg = json.load(f)
cfg["deepseek_api_key"] = "${DEEPSEEK_KEY}"
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("已写入 config.json")
PY
    chmod 600 config.json
  else
    warn "未提供 DEEPSEEK_KEY，跳过生成 config.json。请稍后手动：cp config.example.json config.json 并填入 key。"
  fi
else
  info "config.json 已存在，跳过。"
fi

# 4. 安装并启动 systemd 服务
info "安装 systemd 服务 weizhi-reader..."
# 用占位符替换生成实际服务文件
sed -e "s#/path/to/content-pipeline#${APP_DIR}#g" \
    -e "s#User=www-data#User=${SERVICE_USER}#g" \
    weizhi-reader.service | sudo tee /etc/systemd/system/weizhi-reader.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now weizhi-reader
info "服务已启动："
sudo systemctl --no-pager status weizhi-reader --lines=5 || true

# 5. 写入 cron 定时任务（每天跑 pipeline.py）
info "配置 cron 定时任务..."
CRON_LINE="${CRON_MINUTE} ${CRON_HOUR} * * * cd ${APP_DIR} && ${APP_DIR}/.venv/bin/python pipeline.py >> ${APP_DIR}/run.log 2>&1"
# 若已存在相同条目则跳过，避免重复写入
( crontab -l 2>/dev/null | grep -Fq "pipeline.py" ) || \
  ( crontab -l 2>/dev/null; echo "${CRON_LINE}" ) | crontab -
info "当前 cron 任务："
crontab -l | grep pipeline.py || warn "未检测到 pipeline.py 的 cron 任务，请检查。"

# 6. 防火墙提示
warn "防火墙未自动配置（避免误锁 SSH）。请按需执行："
warn "  sudo ufw allow OpenSSH && sudo ufw allow ${PORT} && sudo ufw enable"

echo
info "部署完成！请在浏览器打开： http://服务器IP:${PORT}"
info "若访问不通，查看服务日志： sudo journalctl -u weizhi-reader -n 50 --no-pager"
