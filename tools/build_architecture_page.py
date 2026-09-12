# -*- coding: utf-8 -*-
"""把架构图 spec 渲染成 GitHub Pages 的首页。

用 `python -m tools.build_architecture_page --archify <archify 目录>`

为什么要有这个脚本：`docs/index.html` 是**生成物**，不是手写页面。
如果直接手改它补 meta，就会又制造一个「改的是生成物、下次重渲就丢、而且不报错」的坑——
和当初手画架构图是同一个毛病。所以补网页头信息的逻辑写在这里，重渲时一起生效。

它做三件事：
  1. 调 archify 把 spec 渲染成自包含 HTML（渲染器会先校验，不通过就不出文件）；
  2. 补上网页该有的头信息：description / canonical / Open Graph 分享卡片 / 图标；
  3. 落到 `docs/index.html`（GitHub Pages 源目录的首页）。

spec 是唯一源头；图片（architecture.png）另由 README 引用，需要一起重出。
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITE_URL = "https://andy021110.github.io/weizhi/"
TITLE = "微知 WeiZhi · 系统结构"
DESCRIPTION = (
    "微知 WeiZhi 的系统结构：一台 2C2G 服务器上跑五个进程，代码分四层，"
    "数据留在部署根目录。可以切换明暗主题，并按「产卡链路 / 读到与记住 / 凌晨自己跑的」"
    "三条路径追踪数据流向。"
)

# 图标从运行时包里取一份到站点目录——Pages 只发布 docs/，取不到包内文件
ICON_SRC = os.path.join(ROOT, "weizhi", "serve", "web", "icon.svg")
ICON_DST = os.path.join(ROOT, "docs", "assets", "icon.svg")

HEAD_INJECT = """<meta name="description" content="{desc}">
  <link rel="canonical" href="{url}">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:url" content="{url}">
  <meta property="og:image" content="{url}assets/readme/architecture.png">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="assets/icon.svg" type="image/svg+xml">
"""


def render(spec, archify_dir, node, out_path):
    """调 archify 渲染。deliver 通过（ok=true）才认为成功。"""
    entry = os.path.join(archify_dir, "bin", "archify.mjs")
    if not os.path.isfile(entry):
        sys.exit("找不到 archify 入口：%s" % entry)
    cmd = [node, entry, "deliver", "architecture", spec, out_path,
           "--quality", "showcase", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit("archify 渲染失败（退出码 %d）：\n%s" % (proc.returncode, proc.stdout[-2000:]))
    if not os.path.isfile(out_path):
        sys.exit("archify 报成功但没有产出文件：%s" % out_path)
    return proc.stdout


def inject_head(html):
    """在 </head> 前补网页头信息。已存在则跳过，重跑幂等。"""
    if 'name="description"' in html:
        return html, False
    block = HEAD_INJECT.format(desc=DESCRIPTION, title=TITLE, url=SITE_URL)
    i = html.find("</head>")
    if i < 0:
        sys.exit("渲染产物里没有 </head>，无法注入头信息")
    return html[:i] + "  " + block + html[i:], True


def main():
    ap = argparse.ArgumentParser(description="把架构图 spec 渲染成 Pages 首页")
    ap.add_argument("--spec", default=os.path.join(ROOT, "docs", "assets", "readme",
                                                  "architecture.spec.json"))
    ap.add_argument("--archify", required=True,
                    help="archify 仓库里含 bin/archify.mjs 的那层目录")
    ap.add_argument("--node", default=shutil.which("node") or "node")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "index.html"))
    args = ap.parse_args()

    # 临时文件也必须以 .html 结尾——archify 会拒绝其它后缀的输出路径
    tmp = args.out[:-5] + ".tmp.html" if args.out.endswith(".html") else args.out + ".tmp.html"
    render(args.spec, args.archify, args.node, tmp)

    html = open(tmp, encoding="utf-8").read()
    html, injected = inject_head(html)

    if os.path.isfile(ICON_SRC):
        os.makedirs(os.path.dirname(ICON_DST), exist_ok=True)
        shutil.copy(ICON_SRC, ICON_DST)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.unlink(tmp)

    print("已写出 %s（%.0f KB）" % (os.path.relpath(args.out, ROOT), len(html) / 1024))
    print("  头信息注入：%s" % ("本次注入" if injected else "已存在，跳过"))
    if os.path.isfile(ICON_DST):
        print("  图标：%s" % os.path.relpath(ICON_DST, ROOT))
    print("  发布地址：%s" % SITE_URL)


if __name__ == "__main__":
    main()
