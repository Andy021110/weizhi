# -*- coding: utf-8 -*-
"""一次性迁移脚本：把 cards/*.json 里的现有卡片导入 SQLite。

可重复运行（source_url 去重）。date 取文件名里的日期。
用法：
    .venv/bin/python migrate.py
"""
import json
import os
import re

from weizhi.core import db

from weizhi.core import paths
CARDS_DIR = os.path.join(paths.data_dir(), "cards")


def main():
    db.init_db()
    if not os.path.isdir(CARDS_DIR):
        print(f"未找到卡片目录：{CARDS_DIR}")
        return

    total = 0
    imported = 0
    files = sorted(
        (f for f in os.listdir(CARDS_DIR) if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", f)),
        reverse=True,
    )

    for name in files:
        date_str = name[:-5]
        path = os.path.join(CARDS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            cards = json.load(f)
        if not isinstance(cards, list):
            cards = [cards]
        for card in cards:
            if not isinstance(card, dict):
                continue
            total += 1
            if db.save_card(card, date=date_str):
                imported += 1

    print(f"迁移完成：扫描 {total} 张卡，新导入 {imported} 张，跳过（已存在）{total - imported} 张。")
    print(f"当前库中共 {len(db.load_cards())} 张卡。")


if __name__ == "__main__":
    main()
