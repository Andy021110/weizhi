#!/usr/bin/env python3
"""Import a verified evidence project into WeiZhi v2."""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from weizhi_v2.importer import import_evidence_project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", help="包含 source/ 和 claims/ 的 workflow 项目目录")
    args = parser.parse_args()
    db.init_db()
    print(import_evidence_project(args.project))


if __name__ == "__main__":
    main()
