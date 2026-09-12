# -*- coding: utf-8 -*-
"""信源分级 + 候选筛选测试。

两条要守住的东西：

1. **级别不能写死**。它决定卡上「权威度」标签，写死会让 arXiv 论文和一条
   二手解读标成同一个值——这件事在 v2 桥接卡上真实发生过（全是「专业博客」）。
2. **筛选失败要降级**。模型挂了不该导致当天一张卡都没有；
   筛选是优化，不能变成单点故障。
"""
import json
import os
from datetime import datetime, timedelta

from weizhi.core import db
from weizhi.produce import pipeline


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


def test_pretriage_window_is_per_tier():
    """同一篇 20 天前的文章：快讯该丢，深度博客该留。

    窗口如果全局一刀切，「一条 20 天前的深度长文」和「一条 20 天前的快讯」
    就会被同样对待——这正是 7 天窗口下 9 月 2 日的长文在 9 月 12 日
    读不到的原因。
    """
    table = {"official": 168, "blog": 720}
    out = pipeline.pretriage([
        _cand("官方旧闻", tier="official", ago_hours=480),   # 20 天
        _cand("博客长文", tier="blog", ago_hours=480),       # 20 天
    ], hours=168, hours_by_tier=table)
    assert [c["title"] for c in out] == ["博客长文"]


def test_pretriage_tier_window_does_not_rescue_ancient_items():
    """放宽窗口不等于来者不拒：域名表兜底之外还是要拦住真正过期的。

    这条是照着真实场景写的——某源的 feed 中段有一批 4 个月前的旧条目
    从没被处理过，30 天窗口也不该把它们捞出来。
    """
    out = pipeline.pretriage([_cand("四个月前的旧文", tier="blog", ago_hours=123 * 24)],
                             hours=168, hours_by_tier={"blog": 720})
    assert out == []


def test_pretriage_source_window_beats_tier_window():
    """源上显式写的 lookback_hours 优先级最高。"""
    c = _cand("只读一天的源", tier="blog", ago_hours=48)
    c["lookback_hours"] = 24
    out = pipeline.pretriage([c], hours=168, hours_by_tier={"blog": 720})
    assert out == []
    # 不写源级窗口时，同一个候选按 tier 的 720h 应当留下
    c2 = _cand("只读一天的源", tier="blog", ago_hours=48)
    assert len(pipeline.pretriage([c2], hours=168, hours_by_tier={"blog": 720})) == 1


def test_pretriage_unknown_tier_uses_fallback_hours():
    """级别不在表里时退回全局 hours，不能因为表里没有就全放行。"""
    out = pipeline.pretriage([_cand("未知级别旧稿", tier="whatever", ago_hours=200)],
                             hours=168, hours_by_tier={"blog": 720})
    assert out == []


def test_lookback_table_ignores_bad_config():
    """配置写错不该炸掉整轮产线，也不该把窗口变成 0（那会拦掉一切）。"""
    table = pipeline.lookback_table({
        "lookback_hours_by_tier": {"blog": "不是数字", "analyst": None,
                                   "OFFICIAL ": 100}})
    assert table["blog"] == pipeline.LOOKBACK_HOURS_BY_TIER["blog"]
    assert table["analyst"] == pipeline.LOOKBACK_HOURS_BY_TIER["analyst"]
    assert table["official"] == 100          # 大小写与空格要归一
    assert all(isinstance(v, int) for v in table.values())


def test_default_window_policy_keeps_depth_sources_longer():
    """默认策略本身要能守住「深度源不被 7 天窗口掐死」。"""
    t = pipeline.LOOKBACK_HOURS_BY_TIER
    assert t["blog"] > t["media"]
    assert t["analyst"] > t["official"]
    assert t["blog"] >= 24 * 30


def test_collect_candidates_carries_source_lookback(tmp_db, monkeypatch):
    """源级窗口要能随候选带下去，否则筛的时候拿不到。"""
    monkeypatch.setattr(pipeline, "fetch_rss", lambda src, limit=None: [
        {"title": "某文", "url": "https://baoyu.io/x", "summary": "s",
         "published": None}])
    out = pipeline.collect_candidates({"sources": [
        {"name": "慢源", "rss": "https://baoyu.io/feed.xml", "lookback_hours": 2160}]})
    assert out[0]["lookback_hours"] == 2160
    assert out[0]["source_tier"] == "blog"


# ---------- 「见过」≠「产过」 ----------
#
# 这一组守的是一个曾经真实存在的缺陷：filter_fresh 会给它抓到的**全部**新条目
# 写指纹，可后面只有几篇会被挑中产卡。结果是「已见集合有洞」——
# feed 中段一批条目从没被处理过，比它新的和比它旧的却都算见过。
# 眼见的现象是：18:00 那轮从某博客**四个月前的**文章产了卡。

class _Args:
    """_run_pick / _run_legacy 需要的命令行参数。"""

    def __init__(self, limit=None, fetch_only=False, legacy=False):
        self.limit = limit
        self.fetch_only = fetch_only
        self.legacy = legacy


def _with_fp(c):
    c["_fp"] = pipeline._title_fp(c["title"])
    c["_sim"] = pipeline._article_sim(c)
    return c


def test_is_expired_uses_per_tier_window():
    table = pipeline.LOOKBACK_HOURS_BY_TIER
    assert pipeline.is_expired(_cand("快讯旧稿", tier="media", ago_hours=20 * 24),
                               168, table)
    assert not pipeline.is_expired(_cand("博客旧稿", tier="blog", ago_hours=20 * 24),
                                   168, table)


def test_mark_seen_is_the_only_writer(tmp_db):
    """filter_fresh 只读；mark_seen 才写。抓一次不等于处理过。"""
    c = _cand("一篇还没结论的文章", ago_hours=1)
    assert pipeline.mark_seen([c]) == 1
    seen = {i["t"] for i in pipeline._load_seen()}
    assert pipeline._title_fp(c["title"]) in seen
    assert pipeline.mark_seen([c]) == 0     # 重复写没有副作用


def test_seen_store_holds_more_than_one_round(tmp_db):
    """指纹库要能记住不止一轮。

    容量太小时，被挤掉的条目会重新变成「没见过」而复活；
    「已见」就不是可靠记录，而 mark_seen 的整套语义都建立在它可靠之上。
    """
    pipeline._save_seen([{"t": "fp%05d" % i, "s": 0} for i in range(5000)])
    assert len(pipeline._load_seen()) == pipeline.SEEN_CAP
    assert pipeline.SEEN_CAP >= 1000, "一轮就写百来条，容量必须远大于一轮"


def test_unpicked_candidate_stays_for_next_round(tmp_db, monkeypatch):
    """被挑中处理过的记成已见；没轮到的留在池子里，过期的是终态。

    如果抓一次就全标已见，模型只是在「这 12 小时新到的几条」里挑，
    慢源的好文章会被快源的噪音挤掉——那正是加候选池要解决的问题。
    """
    picked = _with_fp(_cand("被挑中的", tier="blog", ago_hours=2))
    waiting = _with_fp(_cand("没轮到的", tier="blog", ago_hours=3))
    stale = _with_fp(_cand("过期的", tier="media", ago_hours=24 * 20))
    monkeypatch.setattr(pipeline, "collect_candidates",
                        lambda cfg, per_source=50: [picked, waiting, stale])
    monkeypatch.setattr(pipeline, "rank_candidates",
                        lambda provider, pool, limit=5: ([picked], "model"))
    monkeypatch.setattr(pipeline, "generate_card_evidenced",
                        lambda provider, art, cand, cfg: (None, "测试：故意不成卡"))

    gen, skipped = pipeline._run_pick(
        {"candidate_limit": 30, "daily_pick_limit": 5, "lookback_hours": 168},
        None, _Args(), 15)

    assert gen == 0 and skipped
    seen = {i["t"] for i in pipeline._load_seen()}
    assert pipeline._title_fp("被挑中的") in seen       # 有结论了
    assert pipeline._title_fp("过期的") in seen         # 过期是终态
    assert pipeline._title_fp("没轮到的") not in seen   # 还在池子里等下一轮


def test_legacy_marks_only_processed(tmp_db, monkeypatch):
    """应急的旧路径同样只记处理过的，否则会每轮重复产同一批。"""
    arts = [{"title": "甲篇足够长的标题", "url": "https://x/a",
             "summary": "甲篇摘要内容足够长，用于计算 SimHash 内容指纹"},
            {"title": "乙篇足够长的标题", "url": "https://x/b",
             "summary": "乙篇摘要内容足够长，用于计算 SimHash 内容指纹"}]
    monkeypatch.setattr(pipeline, "fetch_rss", lambda src, limit=None: list(arts))
    monkeypatch.setattr(pipeline, "generate_card", lambda *a, **k: None)
    cfg = {"sources": [{"name": "某源", "rss": "https://x/feed", "category": "前沿"}]}
    pipeline._run_legacy(cfg, None, _Args(limit=1), 15)

    seen = {i["t"] for i in pipeline._load_seen()}
    assert pipeline._title_fp("甲篇足够长的标题") in seen
    assert pipeline._title_fp("乙篇足够长的标题") not in seen   # 留到下一轮


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
                        "weizhi", "produce", "pipeline.py")
    body = open(path, encoding="utf-8").read().split("def main(")[1]
    assert "if args.legacy:" in body
    # 取 else 分支（默认路径），到 main 结束为止
    default_branch = (body.split("if args.legacy:")[1]
                          .split("else:")[1]
                          .split("\ndef _run_legacy")[0])
    assert "_run_pick" in default_branch
    assert "_run_legacy(" not in default_branch


# ---------- 来源属性要记在材料上 ----------

def test_ingest_source_records_source_meta(tmp_db):
    """来源等级要记在材料上，不能只留一个卡上的标签——
    「这条来自哪个源的哪一级」是判断可信度的原始依据，只留结论事后无法复核。"""
    from weizhi.produce import evidence
    sid, _ = evidence.ingest_source(
        "https://openai.com/x", "某段足够长的正文内容，用来触发证据抽取。" * 12,
        title="标题", site="OpenAI 官方",
        meta={"source_id": "openai-com", "source_tier": "official",
              "topics": ["models"], "published_at": "2026-09-12T00:00:00"})
    row = db.get_v2_source_by_id(sid)
    meta = json.loads(row["meta"] or "{}")
    assert meta["source_tier"] == "official"
    assert meta["source_id"] == "openai-com"
    assert meta["topics"] == ["models"]


def test_ingest_source_meta_is_optional(tmp_db):
    """老调用点（v2_shadow）不传 meta 也必须能跑。"""
    from weizhi.produce import evidence
    sid, _ = evidence.ingest_source(
        "https://x/y", "另一段够长的正文内容，同样用来触发抽取。" * 12)
    assert sid
    assert db.get_v2_source_by_id(sid)["meta"] in (None, "", "null")
