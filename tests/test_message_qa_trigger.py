"""留言问答触发路由回归：资讯类问句能进 QA 分支；设线/取消指令不被新触发词劫持。"""
import server as gy


def test_news_query_triggers_qa():
    for t in ('帮我查下今天的行业新闻', '有什么期货资讯', '搜一下焦煤公告', '查一下 https://example.com'):
        assert gy._KB_QA_RE.search(t), t


def test_alert_command_not_hijacked():
    # 设线指令（品种+价格）走预警解析，不进问答分支
    assert gy._parse_alert_text('甲醇3402') is not None
    assert gy._parse_alert_text('查一下甲醇3402') is not None   # 先命中设线，轮不到 QA
    assert not gy._KB_QA_RE.search('甲醇3402')


def test_cancel_command_not_hijacked():
    assert gy._parse_alert_symbol('取消甲醇') is not None
    assert not gy._KB_QA_RE.search('取消甲醇')


def test_chanlun_terms_still_trigger():
    assert gy._KB_QA_RE.search('三类买卖点怎么定义')
