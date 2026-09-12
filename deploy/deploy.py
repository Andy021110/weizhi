#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带守卫的部署：比对服务器与仓库，拒绝覆盖服务器独有的内容。

**为什么需要这个脚本**：本项目已经三次出现「线上领先于仓库」——
服务器的 `db.py` 里有 `ledger_concepts` 台账、`reader.html` 里有整套
「阅读待学」界面、`reader.py` 里有四条台账路由，而仓库里都没有。
每次都是靠人手工 diff 才发现，差一点就 scp 覆盖掉。

这类漂移**不报错、不失败**：功能静悄悄消失，可能过很久才被用户发现。
手工 diff 靠不住（会漏、会忘），所以做成一条命令。

判据：服务器上每一行非空内容，本地文件里都要找得到。
找不到就拒绝上传并列出来——「服务器有、本地没有」就是要覆盖掉的东西。

用法::

    export WZ_PASS='...'          # 或配置 SSH 密钥并留空
    python deploy.py --check                          # 只看漂移，不改任何东西
    python deploy.py --files db.py reader.html        # 比对通过才上传
    python deploy.py --files db.py --force            # 明知要覆盖服务器内容时

环境变量：WZ_HOST / WZ_USER / WZ_PASS / WZ_APP（都有默认值）
"""
import argparse
import base64
import glob
import hashlib
import os
import re
import shlex
import subprocess
import sys

# deploy/ 的上一级是仓库根，也就是服务器上的部署根
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST = os.environ.get("WZ_HOST", "47.76.25.13")
USER = os.environ.get("WZ_USER", "root")
PASS = os.environ.get("WZ_PASS", "")
APP = os.environ.get("WZ_APP", "/opt/weizhi/app")

# 服务器上不参与同步的文件：
# - config.json：生产凭据，线上版本与仓库示例不同，永远不覆盖、也不作为漂移
# - *.db / 日志 / 备份：运行期数据
IGNORE = {"config.json", ".DS_Store", "weizhi.db"}
# 只同步代码与前端资源；文档、测试、工具、部署件不进服务器
SYNC_SUFFIX = (".py", ".html", ".json", ".txt", ".sh", ".js", ".svg", ".png")
SYNC_SKIP_DIRS = {"docs", "tests", "tools", "deploy", ".git", ".venv",
                  "__pycache__", ".pytest_cache", "quality_reports",
                  "backups", "demo_out", "eval_out", "examples"}
IGNORE_SUFFIX = (".db", ".log", ".tgz", ".pyc")

# 运营现场的临时脚本，同步方向是「服务器 → 仓库」，部署时不回传
NEVER_DEPLOY = {"config.json"}

# 这些文件被常驻 HTTP 服务加载。**改了它们必须重启服务**，否则线上跑的
# 还是旧代码——而且不会有任何报错，只会表现为"改动没生效"。
# 这个坑真踩过：部署完验证接口时拿到的是旧版本的响应。
# 包内任何模块都可能被常驻服务加载，改了就得重启。
# 用前缀判断而不是列清单：清单会漏（原来只列了三个文件）。
RESTART_PREFIX = "weizhi/"
SERVICE = os.environ.get("WZ_SERVICE", "weizhi-reader")


def _base(prog):
    """拼出 ssh/scp 的前缀。配了 WZ_PASS 就走 sshpass，否则用密钥。"""
    return ["sshpass", "-p", PASS, prog] if PASS else [prog]


def _common_opts():
    return ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=25",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no", "-o", "LogLevel=ERROR"]


def _target():
    return "%s@%s" % (USER, HOST)


def _ssh(args, stdin=None):
    return subprocess.run(
        _base("ssh") + _common_opts() + [_target(), args],
        input=stdin, capture_output=True, text=True, timeout=180)


def _scp(local, remote):
    return subprocess.run(
        _base("scp") + ["-q"] + _common_opts() + [local, "%s:%s" % (_target(), remote)],
        capture_output=True, text=True, timeout=300)


def _scp_remote(name, dest):
    return subprocess.run(
        _base("scp") + ["-q"] + _common_opts() + ["%s:%s/%s" % (_target(), APP, name), dest],
        capture_output=True, text=True, timeout=300)


def server_hashes():
    """一次拿回服务器上所有候选文件的 md5 与内容行数。"""
    names = _local_candidates()
    script = ("cd %s && for f in %s; do [ -f \"$f\" ] && "
              "printf '%%s %%s\\n' \"$(md5sum \"$f\" | cut -d' ' -f1)\" \"$f\"; done"
              % (APP, " ".join(shlex.quote(n) for n in names)))
    out = _ssh("echo %s | base64 -d | bash"
               % base64.b64encode(script.encode()).decode())
    table = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            table[parts[1]] = parts[0]
    return table


def _lines(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return set(l.strip() for l in fh if l.strip())


def _md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def _is_candidate(name):
    """只收代码与前端资源，路径是相对仓库根的。"""
    if name in IGNORE or name.endswith(IGNORE_SUFFIX):
        return False
    if name.startswith("."):
        return False
    return name.endswith(SYNC_SUFFIX)


def _local_candidates():
    """递归收集要被同步的文件，返回相对仓库根的路径。

    原来的实现只 glob 根目录一层——目录一整理就再也收不到文件，
    而且不报错，只会表现为「部署成功但线上还是旧的」。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(BASE_DIR):
        dirnames[:] = [d for d in dirnames
                       if d not in SYNC_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), BASE_DIR)
            if _is_candidate(rel):
                out.append(rel)
    return sorted(set(out))


def drift_report():
    """返回 (same, differ, only_server, only_local)。"""
    srv = server_hashes()
    local = _local_candidates()
    same, differ, only_server, only_local = [], [], [], []
    for name in sorted(set(local) | set(srv)):
        if not _is_candidate(name):
            continue
        if name in srv and name in local:
            (same if _md5(os.path.join(BASE_DIR, name)) == srv[name]
             else differ).append(name)
        elif name in srv:
            only_server.append(name)
        else:
            only_local.append(name)
    return same, differ, only_server, only_local


# 定义行：丢了就是真丢了功能。其余独有行多半是重构把某行改写掉了。
_DEF_RE = re.compile(r"\b(?:def|class|function)\s+([A-Za-z_$][\w$]*)")
_ROUTE_RE = re.compile(r"""(?:path\s*==\s*|fetchJSON\(\s*)[\"']([^\"']+)[\"']""")


def classify_losses(remote_path, local_path):
    """把「服务器有、本地没有」的行分成两类。

    - `missing_defs`：服务器上的函数/类/路由在本地找不到 → **真丢功能**，必须人工确认
    - `changed_lines`：其余行 → 多半是重构改写了同一处，仍要看清但不必惊慌

    分这两类是因为之前一律硬拦，结果重构（把某行换个写法）会被误判成
    「覆盖了独有内容」。误报多了人就会习惯性 --force，那守卫就废了。
    """
    srv_lines, loc_lines = _lines(remote_path), _lines(local_path)
    lost = [l for l in srv_lines if l not in loc_lines]
    defs = {m.group(1) for l in lost if (m := _DEF_RE.search(l))}
    local_names = {m.group(1) for l in loc_lines if (m := _DEF_RE.search(l))}
    routes = {m.group(1) for l in lost if (m := _ROUTE_RE.search(l))}
    local_text = "\n".join(loc_lines)
    missing_defs = sorted(
        d for d in defs if d not in local_names and d not in local_text)
    missing_routes = sorted(r for r in routes if r not in local_text)
    changed = [l for l in lost
               if not (_DEF_RE.search(l) or _ROUTE_RE.search(l))]
    return missing_defs, missing_routes, changed


def guard(name, tmpdir):
    """下载服务器版本并与本地比对。返回 (missing_defs, missing_routes, changed)。"""
    remote = os.path.join(tmpdir, name)
    r = _scp_remote(name, remote)
    if r.returncode != 0 or not os.path.exists(remote):
        return [], [], []        # 服务器上没有这个文件，不算覆盖
    if _md5(remote) == _md5(os.path.join(BASE_DIR, name)):
        return [], [], []        # 内容相同，无需上传
    return classify_losses(remote, os.path.join(BASE_DIR, name))


def main(argv=None):
    ap = argparse.ArgumentParser(description="带守卫的部署：拒绝覆盖服务器独有内容")
    ap.add_argument("--files", nargs="*", default=None, help="要部署的文件")
    ap.add_argument("--check", action="store_true", help="只报告漂移，不做任何改动")
    ap.add_argument("--force", action="store_true", help="明知要覆盖服务器内容时使用")
    ap.add_argument("--no-restart", action="store_true",
                    help="部署后不重启常驻服务（默认会重启）")
    args = ap.parse_args(argv)

    if args.check or not args.files:
        same, differ, only_server, only_local = drift_report()
        print("一致      : %d" % len(same))
        print("有差异    : %s" % (", ".join(differ) or "无"))
        print("仅服务器有: %s" % (", ".join(only_server) or "无"))
        print("仅本地有  : %s" % (", ".join(only_local) or "无"))
        if only_server:
            print("\n⚠️ 仅服务器有 = 仓库落后于线上。先同步回来，再部署，"
                  "否则下次覆盖会静默删掉它们。")
        return 0

    import tempfile
    blocked = False
    touched_service = False
    with tempfile.TemporaryDirectory() as tmp:
        for name in args.files:
            if name in NEVER_DEPLOY:
                print("跳过（永不部署）: %s" % name)
                continue
            local = os.path.join(BASE_DIR, name)
            if not os.path.exists(local):
                print("❌ 本地不存在: %s" % name)
                blocked = True
                continue
            missing_defs, missing_routes, changed = guard(name, tmp)
            hard = missing_defs + missing_routes
            if hard and not args.force:
                print("⛔ 拒绝部署 %s：服务器上这些定义在本地找不到（真丢功能）" % name)
                for d in missing_defs:
                    print("      函数/类: %s" % d)
                for r in missing_routes:
                    print("      路由: %s" % r)
                blocked = True
                continue
            if changed and not args.force:
                print("⚠️  %s 有 %d 行服务器内容被改写（多为重构，仍需确认后才 --force）："
                      % (name, len(changed)))
                for l in changed[:10]:
                    print("      %s" % l[:110])
                blocked = True
                continue
            if hard or changed:
                print("（--force 已确认：%s 的 %d 处改写为有意重构）"
                      % (name, len(hard) + len(changed)))
            remote_dir = os.path.dirname(name)
            if remote_dir:
                _ssh("mkdir -p %s/%s" % (APP, shlex.quote(remote_dir)))
            r = _scp(local, "%s/%s" % (APP, name))
            if r.returncode != 0:
                print("❌ 上传失败 %s: %s" % (name, r.stderr[:200]))
                blocked = True
            else:
                print("✅ 已部署 %s" % name)
                if name.startswith(RESTART_PREFIX):
                    touched_service = True
    if blocked:
        print("\n有文件被拦下或失败。**先比对服务器与仓库**，不要直接 --force。")
        return 1

    if touched_service and not args.no_restart:
        r = _ssh("systemctl restart %s && sleep 2 && systemctl is-active %s"
                 % (SERVICE, SERVICE))
        state = (r.stdout or "").strip().splitlines()[-1:] or ["(无输出)"]
        print("\n常驻服务已重启（改了它加载的文件，不重启跑的仍是旧代码）：%s"
              % state[0])
        if r.returncode != 0:
            print("⚠️ 重启可能失败，请手工确认: systemctl status %s" % SERVICE)
            return 1
    elif touched_service:
        print("\n⚠️ 提醒：本次改了常驻服务加载的文件但按 --no-restart 跳过了重启，"
              "线上仍跑旧代码。需执行：systemctl restart %s" % SERVICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
