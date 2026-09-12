#!/usr/bin/env python3
"""Write personalized GoalSpec evaluation candidates into the v2 database."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from weizhi_v2 import storage
from weizhi_v2.goal_specs import candidate_goal_specs, evaluate_goal_spec


def main():
    db.init_db()
    existing = {goal["outcome"]: goal for goal in storage.list_goals()}
    created = 0
    for spec in candidate_goal_specs():
        result = evaluate_goal_spec(spec)
        if result["issues"]:
            raise ValueError("GoalSpec 未通过门禁：%s" % result)
        if spec["outcome"] in existing:
            conn = storage._conn()
            try:
                goal_id = existing[spec["outcome"]]["id"]
                conn.execute("UPDATE v2_goals SET status='draft' WHERE id=?", (goal_id,))
                conn.execute("UPDATE v2_milestones SET status='planned' WHERE goal_id=?", (goal_id,))
                conn.commit()
            finally:
                conn.close()
            continue
        saved = storage.create_goal(spec)
        if saved.get("error"):
            raise ValueError(saved)
        created += 1
    print("候选学习路径：%d 条；本次新增：%d 条" % (len(candidate_goal_specs()), created))


if __name__ == "__main__":
    main()
