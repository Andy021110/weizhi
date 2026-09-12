# -*- coding: utf-8 -*-
"""信源分级 + 候选筛选测试。

两条要守住的东西：

1. **级别不能写死**。它决定卡上「权威度」标签，写死会让 arXiv 论文和一条
   二手解读标成同一个值——这件事在 v2 桥接卡上真实发生过（全是「专业博客」）。
2. **筛选失败要降级**。模型挂了不该导致当天一张卡都没有；
   筛选是优化，不能变成单点故障。
"""
import os
from datetime import datetime, timedelta

import pipeline


# ---------- 来源分级 ----------

def test_tier_from_config_wins_over_domain():
    src = {"name": "某源", "rss": "https://openai.com/news/rss.xml", "tier": "media"}
    assert pipeline.resolve_tier(src) == "media"


def test_tier_falls_back_to_domain():
    assert pipeline.resolve_tier({"rss": "https://openai.com/news/rss.xml"}) == "official"
    assert pipeline.resolve_tier({"rss": "https://rss.arxiv.org/rss/cs.LG"}) == "paper"
    assert pipeline.resolve_tier({"rss": "https://www.qbitai.com/feed"}) == "media"
    assert pipeline.resolve_tier({"rss": "https://huggingface.co/blog/feed.xml"}) == "primary"


def test_unknown_domain_is_underestimated():
    """判不出来时宁可低估——把二手标成一手，比反过来危险得多。"""
    assert pipeline.resolve_tier({"rss": "https://unknown.example/feed"}) == "blog"


def test_invalid_tier_in_config_is_ignored():
    src = {"rss": "https://openai.com/news/rss.xml", "tier": "随便写的"}
    assert pipeline.resolve_tier(src) == "official"


def test_tier_of_prefers_candidate_field():
    """主产线的候选在收集时已解析级别，不该再按域名猜一遍。"""
    assert pipeline._tier_of({"source_tier": "paper", "rss": "https://openai.com/f"}) == "paper"
    assert pipeline._tier_of({"rss": "https://openai.com/f"}) == "official"


def test_source_id_uses_domain_not_name():
    """名称会改，域名不会——换名字不该被当成换了一个源。"""
    a = pipeline._source_id({"name": "旧名字", "rss": "https://openai.com/news/rss.xml"})
    b = pipeline._source_id({"name": "改名了", "rss": "https://openai.com/other/feed"})
    assert a == b == "openai-com"


# ---------- 候选收集 ----------

def test_collect_candidates_carries_source_metadata(tmp_db, monkeypatch):
    """候选必须带来源级别——它是排序和卡片权威度标签的依据。"""
    monkeypatch.setattr(pipeline, "fetch_rss", lambda src, limit=None: [
        {"title": "某文", "url": "https://openai.com/x", "summary": "s", "published": None}])
    out = pipeline.collect_candidates(
        {"sources": [{"name": "OpenAI 官方", "rss": "https://openai.com/news/rss.xml"}]})
    assert len(out) == 1
    c = out[0]
    assert c["source_tier"] == "official"
    assert c["source_id"] == "openai-com"
    assert c["source_name"] == "OpenAI 官方"
    assert c["id"]


# ---------- 时效窗口 ----------

def _cand(title, tier="blog", ago_hours=1):
    ts = datetime.now() - timedelta(hours=ago_hours)
    return {"id": title, "title": title, "source_tier": tier,
            "published_at": ts.isoformat(), "url": "https://x/" + title}


def test_pretriage_drops_stale_candidates():
    """168 小时之外不进候选——「过时卡」就是没有时效窗口造成的。"""
    out = pipeline.pretriage([_cand("新的", ago_hours=10),
                              _cand("旧的", ago_hours=200)], hours=168)
    assert [c["title"] for c in out] == ["新的"]


def test_pretriage_keeps_candidate_without_published_at():
    """没有发布时间的不能直接丢（源不提供是常态），但也不能当它很新。"""
    c = {"id": "x", "title": "没有时间", "source_tier": "blog",
         "url": "https://x/a", "published_at": None}
    assert len(pipeline.pretriage([c], hours=168)) == 1


def test_pretriage_dedupes_same_title_across_sources():
    out = pipeline.pretriage([_cand("同一件事", ago_hours=2),
                              _cand("同一件事", ago_hours=1)], hours=168)
    assert len(out) == 1


def test_pretriage_orders_by_tier_then_recency():
    """一手来源优先；同级按时间倒序。"""
    out = pipeline.pretriage([
        _cand("二手新稿", tier="blog", ago_hours=1),
        _cand("官方旧稿", tier="official", ago_hours=50),
        _cand("官方新稿", tier="official", ago_hours=10),
    ], hours=168)
    assert [c["title"] for c in out] == ["官方新稿", "官方旧稿", "二手新稿"]


def test_pretriage_respects_cap():
    cands = [_cand("c%d" % i, ago_hours=i + 1) for i in range(50)]
    assert len(pipeline.pretriage(cands, hours=168, cap=30)) == 30


# ---------- 模型筛（含降级） ----------

class _FakeProvider:
    def __init__(self, payload=None, boom=False):
        self.payload = payload
        self.boom = boom

    def generate_json(self, task, schema, inputs):
        assert task == "candidate_rank", "任务名必须已在 V2_PROMPTS 注册"
        assert inputs["n"] > 0 and "{" not in inputs["block"]
        if self.boom:
            raise RuntimeError("模型挂了")
        assert schema(self.payload) == [], schema(self.payload)
        return self.payload


def test_rank_candidates_uses_model_order_and_reason():
    pool = [_cand("甲"), _cand("乙"), _cand("丙")]
    p = _FakeProvider({"picks": [{"id": "丙", "why": "值得读"}]})
    picked, mode = pipeline.rank_candidates(p, pool, limit=2)
    assert mode == "model"
    assert picked[0]["title"] == "丙"
    assert picked[0]["why"] == "值得读"


def test_rank_candidates_falls_back_when_model_fails():
    """模型挂了不该导致当天一张卡都没有。"""
    pool = [_cand("甲", tier="official"), _cand("乙")]
    picked, mode = pipeline.rank_candidates(_FakeProvider(boom=True), pool, limit=2)
    assert mode == "fallback"
    assert len(picked) == 2
    assert picked[0]["title"] == "甲"          # 退化成级别排序


def test_rank_candidates_falls_back_on_empty_picks():
    pool = [_cand("甲")]
    picked, mode = pipeline.rank_candidates(_FakeProvider({"picks": []}), pool, limit=2)
    assert mode == "fallback" and len(picked) == 1


def test_rank_candidates_handles_no_provider():
    picked, mode = pipeline.rank_candidates(None, [_cand("甲")], limit=2)
    assert mode == "no_provider" and len(picked) == 1


def test_rank_candidates_empty_pool():
    picked, mode = pipeline.rank_candidates(_FakeProvider({"picks": []}), [], limit=5)
    assert picked == [] and mode == "empty"


def test_rank_ignores_hallucinated_ids():
    """模型可能编一个不在候选里的 id，不能被写进产出。"""
    pool = [_cand("甲")]
    p = _FakeProvider({"picks": [{"id": "不存在的id", "why": "x"},
                                 {"id": "甲", "why": "y"}]})
    picked, mode = pipeline.rank_candidates(p, pool, limit=5)
    assert mode == "model"
    assert [c["title"] for c in picked] == ["甲"]


def test_validate_rank_rejects_bad_shapes():
    assert pipeline._validate_rank([])                          # 不是对象
    assert pipeline._validate_rank({})                          # 缺 picks
    assert pipeline._validate_rank({"picks": [{"why": "无 id"}]})
    assert pipeline._validate_rank({"picks": [{"id": "a"}]}) == []


# ---------- 默认路径 ----------

def test_default_path_is_pick_not_legacy():
    """默认必须走候选筛选。接线错了不会让任何功能测试变红，
    只会让线上悄悄退回「来几篇产几篇」——那正是这次要改掉的东西。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pipeline.py")
    body = open(path, encoding="utf-8").read().split("def main(")[1]
    assert "if args.legacy:" in body
    # 取 else 分支（默认路径），到 main 结束为止
    default_branch = (body.split("if args.legacy:")[1]
                          .split("else:")[1]
                          .split("\ndef _run_legacy")[0])
    assert "_run_pick" in default_branch
    assert "_run_legacy(" not in default_branch
