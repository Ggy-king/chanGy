"""本地数据工具。脱敏收口（设计文档 §4.1 定稿）：发给模型的数据口径只在这里定——
时间只保留小时（抹掉分钟，违规时段/报复性交易检测靠小时聚合）、价格/品种/盈亏/文字保留、
资金总量 CAPITAL 不发（只发风险占比）。将来改口径只动这个文件。"""


def redact_trades(trades):
    """审计流水 → 脱敏数据包条目。trades 条目需含 dispositions（该笔各审计项的用户裁决）。"""
    out = []
    for t in trades:
        ob, cb = str(t.get('open_bj') or ''), str(t.get('close_bj') or '')
        out.append({'sym': t.get('sym'), 'name': t.get('name'), 'period': t.get('period'),
                    'entry': t.get('entry'),
                    'open': (ob[:10] + ' ' + ob[11:13] + '时') if ob else None,
                    'close': (cb[:10] + ' ' + cb[11:13] + '时') if cb else None,
                    'open_price': t.get('open_price'), 'lots': t.get('lots'),
                    'pnl': t.get('pnl'), 'verdict': t.get('verdict'),
                    'dispositions': t.get('dispositions')})
    return out
