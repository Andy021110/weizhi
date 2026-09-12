"""SimHash 跨源去重测试。

被测：db.norm_title / db._simhash / db._hamming / db.find_similar_title
      pipeline._article_sim / pipeline.filter_fresh
核心 trade-off：内容指纹只算摘要（标题常被转载改写），跨源转载靠汉明距离 ≤3 识别。
"""
from weizhi.core import db
from conftest import make_card
from weizhi.produce.pipeline import _article_sim, filter_fresh, mark_seen


def test_norm_title_strips_punct():
    assert db.norm_title("量子纠缠：超越时空！") == "量子纠缠超越时空"
    assert db.norm_title("Hello World, AI!") == "helloworldai"


def test_simhash_same_text_zero_distance():
    a = db._simhash("OpenAI 今日发布新模型")
    b = db._simhash("OpenAI 今日发布新模型")
    assert db._hamming(a, b) == 0


def test_simhash_unrelated_text_far():
    a = db._simhash("OpenAI 今日发布新模型，上下文窗口翻倍")
    b = db._simhash("番茄炒蛋的做法是先炒蛋再放番茄")
    assert db._hamming(a, b) > 10


def test_article_sim_cross_source_rewrite():
    """标题被改写但摘要相同的转载 → 内容指纹距离 ≤3（判为重复）。"""
    a1 = {"title": "OpenAI 发布新模型 GPT-5.5 上下文窗口翻倍",
          "summary": "OpenAI 今日发布新模型，上下文窗口从 100 万提升到 200 万 token"}
    a2 = {"title": "重磅！GPT-5.5 来了：上下文窗口竟然翻倍了",
          "summary": "OpenAI 今日发布新模型，上下文窗口从 100 万提升到 200 万 token"}
    assert db._hamming(_article_sim(a1), _article_sim(a2)) <= 3


def test_article_sim_fallback_title():
    """摘要太短时退化为标题指纹（仍可区分无关文章）。
    注：_simhash 对 <8 字符的文本返回 0（防噪声设计），因此用真实长度标题。"""
    a1 = {"title": "自注意力机制的数学直觉与实现要点", "summary": ""}
    a2 = {"title": "贝叶斯定理的先验与后验更新过程", "summary": ""}
    assert db._hamming(_article_sim(a1), _article_sim(a2)) > 3


def test_simhash_short_text_returns_zero():
    """<8 字符的短文本指纹为 0（设计：不建指纹防噪声）。"""
    assert db._simhash("短") == 0


def test_find_similar_title_exact(tmp_db):
    db.save_card(make_card(title="量子纠缠：超越时空的幽灵联系"))
    assert db.find_similar_title("量子纠缠：超越时空的幽灵联系") is not None


def test_find_similar_title_unrelated(tmp_db):
    db.save_card(make_card(title="量子纠缠：超越时空的幽灵联系"))
    assert db.find_similar_title("番茄炒蛋的做法详解") is None


def test_find_similar_title_short_only_exact(tmp_db):
    """短标题（<15 字符）只做完全匹配，避免误杀。"""
    db.save_card(make_card(title="RAG 原理与实践"))
    assert db.find_similar_title("RAG 原理与实") is None  # 长度差 < 阈值但短标题保守


def test_filter_fresh_dedup_cross_source(tmp_db):
    """第一源先到 → 收录；转载（改标题同摘要）→ 拦截。

    注意 filter_fresh 只读不写：指纹要显式 mark_seen。
    这条同时守住新契约——**抓到不等于处理过**。"""
    a1 = [{"title": "OpenAI 发布新模型 GPT-5.5 上下文窗口翻倍",
           "summary": "OpenAI 今日发布新模型，上下文窗口从 100 万提升到 200 万 token"}]
    a2 = [{"title": "重磅！GPT-5.5 来了：上下文窗口竟然翻倍了",
           "summary": "OpenAI 今日发布新模型，上下文窗口从 100 万提升到 200 万 token"}]
    got = filter_fresh(a1)
    assert len(got) == 1                      # 源 A 收录
    mark_seen(got)                            # 有结论了才写指纹
    assert len(filter_fresh(a2)) == 0         # 源 B 转载被拦


def test_filter_fresh_alone_does_not_mark_seen(tmp_db):
    """抓一次不算处理过——否则没被挑中的那些会被静默丢掉。"""
    a = [{"title": "某篇还没被挑中的文章", "summary": "摘要内容足够长，可以用来算 SimHash 指纹"}]
    assert len(filter_fresh(a)) == 1
    assert len(filter_fresh(a)) == 1   # 再抓一次仍在，说明没被写成已见


def test_filter_fresh_unrelated_passes(tmp_db):
    a1 = [{"title": "自注意力机制详解", "summary": "自注意力让每个位置关注全部位置的相关程度"}]
    got = filter_fresh(a1)
    assert len(got) == 1
    mark_seen(got)
    a2 = [{"title": "贝叶斯定理入门", "summary": "贝叶斯定理描述先验概率如何被证据更新为后验"}]
    assert len(filter_fresh(a2)) == 1   # 无关新内容放行
