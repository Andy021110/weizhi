# 微知 · 服务器部署文档（Ubuntu 22.04）

本文档一步步教你在一台 **Ubuntu 22.04** 云服务器上部署「微知」碎片学习产品的管线 + 阅读器。

部署完成后，你会得到：

- 一个常驻的阅读器服务（`reader.py`，监听 `0.0.0.0:8000`），开机自启、崩溃自动重启；
- 一个每天自动抓取并生成卡片的定时任务（`pipeline.py`）；
- 数据存在 SQLite 数据库 `weizhi.db` 中。

> 路径约定：下文统一把项目部署在 `/opt/weizhi/content-pipeline`。你可以换成任意路径，但**所有出现该路径的地方都要一并替换**。

---

## 0. 开始前必读：几个关键提醒

1. **国内服务器绑域名需 ICP 备案**。自用的话，建议直接用 `http://服务器IP:8000` 访问（**不绑域名，免备案**）；或使用香港/海外节点。若必须绑域名，请先完成 ICP 备案。
2. **安全**：
   - SSH 建议改用**密钥登录**、修改默认端口（22），禁止 root 密码登录；
   - 开启防火墙，只放行 `22`（SSH，或你改后的端口）和 `8000`（阅读器）两个端口；
   - 不要对公网暴露其他端口。
3. **DeepSeek key 是敏感信息**。`config.json` 里存着你的 API key，**绝对不要提交到公开的 git 仓库**（建议把它加进 `.gitignore`）。
4. **时政类内容合规**：抓取新闻类 RSS 时注意内容合规，本产品**仅自用**，勿对外传播生成内容。

---

## 1. 服务器准备

### 1.1 登录服务器

```bash
ssh 你的用户名@服务器IP
```

> 建议用非 root 用户操作；下文涉及 `sudo` 的命令需要该用户有 sudo 权限。

### 1.2 更新系统并安装基础依赖

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git curl nginx
```

验证 Python 版本（需 ≥ 3.9，Ubuntu 22.04 自带 3.10，满足要求）：

```bash
python3 --version
```

> 若你不想用 nginx、只用 `IP:8000` 访问，可以省掉 `nginx`，但装了也无妨。

---

## 2. 上传代码到服务器

选下面两种方式之一。

### 方式 A：scp 上传（简单，适合一次性部署）

在**本地电脑**上执行（不是服务器上）：

```bash
# 把整个 content-pipeline 目录上传到服务器的 /opt/weizhi/
scp -r /本地路径/content-pipeline 你的用户名@服务器IP:/opt/weizhi/
```

> 注意：本地目录里的 `.venv`（本地虚拟环境）和 `weizhi.db` 无需上传（`.venv` 在服务器上重建，`weizhi.db` 如需保留旧数据可一并上传）。如需跳过 `.venv`，可用 `rsync`：
>
> ```bash
> rsync -av --exclude '.venv' /本地路径/content-pipeline/ 你的用户名@服务器IP:/opt/weizhi/content-pipeline/
> ```

### 方式 B：git 克隆（适合后续持续更新）

```bash
cd /opt/weizhi
git clone 你的仓库地址 content-pipeline
```

> 再次提醒：**不要**把 `config.json`（含 API key）提交进仓库；只提交 `config.example.json`。

---

## 3. 建立虚拟环境并安装依赖

在服务器上执行：

```bash
cd /opt/weizhi/content-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

安装完成后验证一下：

```bash
.venv/bin/python -c "import feedparser, trafilatura, openai; print('依赖 OK')"
```

> 若出现 `externally-managed-environment` 报错，说明你漏了 venv 直接用了系统 pip，按上面用 `.venv/bin/pip` 即可。
> 若 `trafilatura` 等包安装很慢，可换国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

---

## 4. 配置

### 4.1 创建并填写 config.json

```bash
cd /opt/weizhi/content-pipeline
cp config.example.json config.json
```

编辑 `config.json`：

```bash
nano config.json
```

把 `"deepseek_api_key"` 的值改成你的真实 key（`sk-...` 开头）。申请地址：<https://platform.deepseek.com>

`config.json` 里 `sources` 数组是内容源，可按需增删（注意合规，仅自用）。

> 修改文件权限，避免 key 被其他系统用户读到（可选但推荐）：
>
> ```bash
> chmod 600 config.json
> ```

### 4.2 迁移数据（可选）

如果你之前有 `cards/YYYY-MM-DD.json` 格式的旧数据，想导入到 SQLite，执行：

```bash
cd /opt/weizhi/content-pipeline
.venv/bin/python migrate.py
```

> 可重复执行，`source_url` 去重，不会重复导入。
> 如果你把本地已有的 `weizhi.db` 直接上传到了服务器，则无需迁移。

---

## 5. 用 systemd 让 reader.py 常驻（开机自启 + 崩溃重启）

### 5.1 创建服务文件

项目里已提供模板 `weizhi-reader.service`。把它复制到 systemd 目录并**改成你的实际路径**。

**先确认你的实际路径，下面统一用 `/opt/weizhi/content-pipeline`。**

```bash
cd /opt/weizhi/content-pipeline

# 用 sed 把占位符替换成实际路径，然后写入 /etc/systemd/system/
sed -e 's#/path/to/content-pipeline#/opt/weizhi/content-pipeline#g' \
    -e 's#User=www-data#User=你的用户名#g' \
    weizhi-reader.service | sudo tee /etc/systemd/system/weizhi-reader.service
```

> 上面命令里的 `你的用户名` 请换成你登录服务器的用户名（`whoami` 可查看）。也可以手动编辑：
>
> ```bash
> sudo cp weizhi-reader.service /etc/systemd/system/weizhi-reader.service
> sudo nano /etc/systemd/system/weizhi-reader.service
> # 把 /path/to/content-pipeline 全部替换成实际路径，把 User= 改成你的用户名
> ```

### 5.2 启动并设为开机自启

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weizhi-reader
```

### 5.3 检查运行状态

```bash
sudo systemctl status weizhi-reader
# 看到 active (running) 即为成功
```

查看日志（若启动失败，从这里排查）：

```bash
sudo journalctl -u weizhi-reader -n 50 --no-pager
```

常用管理命令：

```bash
sudo systemctl restart weizhi-reader   # 重启
sudo systemctl stop weizhi-reader      # 停止
sudo systemctl status weizhi-reader    # 查看状态
```

---

## 6. 定时任务：cron 每天跑 pipeline.py

### 6.1 先手动跑一轮测试（确保能生成）

```bash
cd /opt/weizhi/content-pipeline

# 只抓不调 AI（不花钱），验证 RSS 源可用：
.venv/bin/python pipeline.py --fetch-only --limit 1

# 跑真实流程（调 AI，每个源限 2 篇）：
.venv/bin/python pipeline.py --limit 2
```

看到 `🎉 本轮完成，共生成 N 张卡片` 即成功。

### 6.2 添加 cron 定时任务

```bash
crontab -e
```

在文件末尾加一行（每天早上 6:00 自动跑，日志追加到 `run.log`）：

```
0 6 * * * cd /opt/weizhi/content-pipeline && /opt/weizhi/content-pipeline/.venv/bin/python pipeline.py >> /opt/weizhi/content-pipeline/run.log 2>&1
```

> 关键点：
> - 必须用 `.venv/bin/python` 的**绝对路径**，否则 cron 环境找不到依赖；
> - `cd` 到项目目录，保证 `config.json`、`weizhi.db` 等相对路径能正确找到；
> - `>> run.log 2>&1` 把输出和错误都写进日志，方便排查。

保存退出后验证：

```bash
crontab -l
```

> 其他常用：删除定时任务 `crontab -r`；查看最近日志 `tail -f /opt/weizhi/content-pipeline/run.log`

---

## 7. Nginx 反向代理（可选，可跳过）

**如果只用 `http://服务器IP:8000` 访问，本步完全可跳过。**

如果想用 80 端口直接访问（即 `http://服务器IP` 不写端口），用 nginx 把 80 转发到 `127.0.0.1:8000`。

项目里提供了 `nginx.conf` 示例：

```bash
cd /opt/weizhi/content-pipeline
sudo cp nginx.conf /etc/nginx/sites-available/weizhi
# 如示例里用了域名，把 server_name 改成你的 IP 或域名
sudo nano /etc/nginx/sites-available/weizhi

# 启用站点并重载 nginx
sudo ln -sf /etc/nginx/sites-available/weizhi /etc/nginx/sites-enabled/weizhi
sudo nginx -t          # 检查配置语法
sudo systemctl reload nginx
```

之后访问 `http://服务器IP` 即可（80 端口）。

> 注意：若启用 nginx 并想关闭 8000 直连，可在防火墙里不放行 8000、只放行 80。

---

## 8. 验证部署

1. 在**浏览器**打开：

   ```
   http://服务器IP:8000
   ```

   应能看到「微知」阅读器界面。

2. 接口自测（可选）：

   ```bash
   curl http://127.0.0.1:8000/api/dates
   curl "http://127.0.0.1:8000/api/cards?date=今天日期"
   ```

   返回 JSON 即说明服务和数据库都正常。

3. 若访问不通，按顺序排查：
   - 服务是否在跑：`sudo systemctl status weizhi-reader`
   - 端口是否监听：`ss -tlnp | grep 8000`
   - 防火墙是否放行 8000：`sudo ufw status`（见下一节）

---

## 9. 防火墙与安全加固（强烈建议）

### 9.1 开启 UFW，只放行必要端口

```bash
sudo ufw allow OpenSSH     # 放行 SSH（22，或你改后的端口）
sudo ufw allow 8000        # 放行阅读器端口（若用 nginx 代理则放行 80 代替）
sudo ufw enable
sudo ufw status            # 确认规则
```

> 千万别在放行 SSH 之前 `enable`，否则可能把自己锁在门外。

### 9.2 SSH 加固（可选但推荐）

```bash
sudo nano /etc/ssh/sshd_config
```

建议修改：

```
Port 2222                     # 改成非默认端口（同时防火墙也要放行该端口）
PermitRootLogin no            # 禁止 root 直接登录
PasswordAuthentication no     # 关闭密码登录，改用密钥
```

改完重启 SSH：

```bash
sudo systemctl restart ssh
```

> **务必先确认能用密钥登录、且新端口已放行，再断开当前会话**，否则可能无法再登录。

### 9.3 不要把 key 泄露到 git

本地项目若用 git 管理，确认 `config.json` 在 `.gitignore` 里：

```bash
echo "config.json" >> .gitignore
```

---

## 附录：一键部署脚本

项目里提供了 `deploy.sh`，可自动完成上述「装依赖 → 建 venv → 启动 systemd → 配 cron」。用法：

```bash
cd /opt/weizhi/content-pipeline
nano deploy.sh    # 先改开头的 APP_DIR、SERVICE_USER、DEEPSEEK_KEY 三个变量
bash deploy.sh
```

> 脚本会创建 venv、装依赖、生成 config.json、安装并启动 systemd 服务、写入 cron 任务。执行前务必先填好变量。
