# -*- coding: utf-8 -*-
"""路径约定：代码与数据分开。

为什么要单独有这么一个文件：仓库原来把 config.json（里面有 API key）、
weizhi.db（真实数据）、前端资源和 23 个代码模块平铺在同一个目录里。
后果有两个——部署时「同步代码」和「保留数据」全靠人守纪律；
而且只要想整理目录，就必然要顺带动数据库路径。

现在 weizhi/ 里只有代码，配置、数据库、质量报告、备份留在仓库根
（也就是部署根）。整理代码不再需要迁任何数据。
"""
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))     # weizhi/core
CODE_DIR = os.path.dirname(PKG_DIR)                      # weizhi
ROOT_DIR = os.path.dirname(CODE_DIR)                     # 仓库根 = 部署根
WEB_DIR = os.path.join(CODE_DIR, "serve", "web")         # 前端静态资源


def data_dir():
    """数据目录：默认仓库根，可用环境变量 WEIZHI_DATA 覆盖。

    做成函数而不是常量，是为了测试和多实例能改；模块级的 DB_PATH
    在 import 时算一次就够了。
    """
    return os.path.abspath(os.environ.get("WEIZHI_DATA") or ROOT_DIR)


def code_dir():
    return CODE_DIR


def web_dir():
    return WEB_DIR
