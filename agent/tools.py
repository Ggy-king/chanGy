"""Agent 工具层：模型可通过 function calling 查询的本地数据接口 + 脱敏收口。

设计要点（双职责，两件事都在这一个文件收口）：
1. 脱敏收口——发给模型的数据口径只在这里定：时间只保留小时（抹掉分钟，
   违规时段/报复性交易检测靠小时聚合）、价格/品种/盈亏/文字保留、
   资金总量 CAPITAL 不发（只发风险占比）。将来改口径只动这个文件。
2. 工具注册表——`build_registry(pack)` 把周数据包变成一组可查询工具，
   返回 (schemas, executors)：schemas 为 OpenAI 兼容 function 定义（供 LLM 选择），
   executors[name](args) 为本地执行器。每个工具输出带稳定证据编号（T/A/Q/D/U 前缀），
   模型的论断据此回链到具体记录，前端可逐条核对。

证据编号规则：
  T01..  交易流水（按平仓/开仓时间排序，与数据包顺序一致）
  A01..  审计条目（含严重级别 sev / 审计维度 type / 品种 / 小时）
  Q01..  体系疑问清单（open 状态）
  D01..  交易日（周一~周五）
  U1     用户输入（三问回答 / 行动项 / 上周教练报告，统一一条）
"""
import json


# ============ 脱敏（server 直接调用，口径收口） ============

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


def redact_audit_items(items):
    """审计条目 → 脱敏数据包条目：维度 / 严重级别 / 品种 / 小时 / 归属交易。
    说明文字保留（是模型理解违规动机的关键素材），分钟抹掉、无资金总量。"""
    out = []
    for it in items:
        out.append({'key': it.get('key'), 'trade': it.get('trade'), 'type': it.get('type'),
                    'sev': it.get('sev'), 'sym': it.get('sym'), 'hour': it.get('hour'),
                    'detail': it.get('detail') or it.get('note') or ''})
    return out


# ============ 工具注册表 ============

def _fmt_trades(rows):
    """交易流水行 → 模型可读文本（紧凑单行，省 token）。"""
    lines = []
    for r in rows:
        pnl = r.get('pnl')
        pnl_s = f"{pnl:+.0f}元" if isinstance(pnl, (int, float)) else '—'
        lines.append(f"[{r['eid']}] {r.get('sym')} {r.get('name')} {r.get('period')} {r.get('entry') or ''}"
                     f" {r.get('open') or '?'}→{r.get('close') or '?'}"
                     f" 信号判定={r.get('verdict')} 盈亏={pnl_s}"
                     f" 裁决={json.dumps(r.get('dispositions') or {}, ensure_ascii=False)}")
    return '\n'.join(lines) or '（无匹配记录）'


def build_registry(pack):
    """把周数据包装配成工具注册表。返回 (schemas, executors)：
    schemas 给 LLM 选工具；executors[name](args) 本地执行并返回带证据编号的结果。"""
    trades = []
    for i, t in enumerate(pack.get('trade_flow') or []):
        trades.append(dict(t, eid=f"T{i + 1:02d}"))
    audits = [{'eid': f"A{i + 1:02d}", **a} for i, a in enumerate(pack.get('audit_items') or [])]
    questions = [{'eid': f"Q{i + 1:02d}", **q} for i, q in enumerate(pack.get('questions_open') or [])]
    days = [{'eid': f"D{i + 1:02d}", 'date': d, **v}
            for i, (d, v) in enumerate(sorted((pack.get('days') or {}).items()))]

    def overview(_args):
        sev = {}
        for a in audits:
            sev[a.get('sev') or '未知'] = sev.get(a.get('sev') or '未知', 0) + 1
        return {'week': pack.get('week'),
                'days': [{'eid': d['eid'], 'date': d['date'], 'status': d.get('status'),
                          'opens': d.get('opens')} for d in days],
                'compliance': pack.get('compliance'),
                'n_trades': len(trades), 'n_audits': len(audits),
                'audit_by_severity': sev or None,
                'n_questions_open': len(questions),
                'has_user_input': bool(pack.get('answers') or pack.get('actions'))}

    def query_trades(args):
        sym = (args.get('sym') or '').strip() or None
        period = (args.get('period') or '').strip() or None
        verdict = (args.get('verdict') or '').strip() or None
        limit = min(int(args.get('limit') or 15), 40)
        rows = [t for t in trades
                if (not sym or t.get('sym') == sym)
                and (not period or str(t.get('period')) == period)
                and (not verdict or t.get('verdict') == verdict)]
        rows = rows[:limit]
        return {'matched': len(rows), 'total': len(trades), 'rows': rows, 'text': _fmt_trades(rows)}

    def calc_stats(args):
        group_by = args.get('group_by') or 'all'
        key = {'sym': 'sym', 'period': 'period', 'verdict': 'verdict',
               'disposition': '_disp'}.get(group_by)
        groups = {}
        for t in trades:
            if key == '_disp':
                disps = [v for v in (t.get('dispositions') or {}).values() if v]
                gk = ','.join(sorted(set(disps))) if disps else '未处置'
            else:
                gk = str(t.get(key) or '未知') if key else '全部'
            groups.setdefault(gk, []).append(t)
        out = []
        for gk, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            pnls = [t.get('pnl') for t in rows if isinstance(t.get('pnl'), (int, float))]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            entry = {'group': gk, 'n': len(rows),
                     'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else None,
                     'sum_pnl': round(sum(pnls), 0) if pnls else None,
                     'avg_pnl': round(sum(pnls) / len(pnls), 0) if pnls else None,
                     'payoff': round((sum(wins) / len(wins)) / (abs(sum(losses)) / len(losses)), 2)
                     if wins and losses and sum(losses) else None}
            if len(pnls) >= 3:   # 单组样本够时给显著性检验：盈亏均值是否显著≠0
                try:
                    from scipy import stats as sps
                    t_stat, p_val = sps.ttest_1samp(pnls, 0.0)
                    entry['t_stat'], entry['p_val'] = round(float(t_stat), 2), round(float(p_val), 4)
                except Exception:
                    pass
            out.append(entry)
        return {'group_by': group_by, 'groups': out,
                'note': 'payoff=平均盈利/平均亏损；p_val<0.05 表示该组盈亏显著偏离 0'}

    def query_audit(args):
        trade = (args.get('trade') or '').strip() or None
        sev = (args.get('sev') or '').strip() or None
        limit = min(int(args.get('limit') or 20), 60)
        rows = [a for a in audits
                if (not trade or a.get('trade') == trade)
                and (not sev or a.get('sev') == sev)][:limit]
        return {'matched': len(rows), 'total': len(audits),
                'rows': [{'eid': a['eid'], 'trade': a.get('trade'), 'type': a.get('type'),
                          'sev': a.get('sev'), 'sym': a.get('sym'), 'hour': a.get('hour'),
                          'detail': (a.get('detail') or '')[:200]} for a in rows]}

    def query_questions(_args):
        return {'rows': questions or [], 'text': '\n'.join(
            f"[{q['eid']}] {q.get('text')}（{q.get('created')}）" for q in questions) or '（本周无 open 疑问）'}

    def get_user_notes(_args):
        coach = pack.get('last_week_coach') or ''
        return {'answers': pack.get('answers'),
                'actions': pack.get('actions'),
                'last_week_coach': ('【上周教练报告（节选）】' + coach[:1200] + '…（已截断）')
                if len(coach) > 1200 else (coach or None),
                'eid': 'U1'}

    executors = {'get_week_overview': overview, 'query_trades': query_trades,
                 'calc_stats': calc_stats, 'query_audit': query_audit,
                 'query_questions': query_questions, 'get_user_notes': get_user_notes}

    schemas = [
        {'type': 'function', 'function': {
            'name': 'get_week_overview',
            'description': '一周总览：交易日明细、合规率与审计计数、交易笔数、open 疑问数。研究第一步先调这个。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
        {'type': 'function', 'function': {
            'name': 'query_trades',
            'description': '查询本周交易流水（每笔带证据编号[Txx]，可按品种/周期/信号判定过滤）。'
                           '审计一个具体行为时先用它定位交易，再拿 trade 字段去查 query_audit。',
            'parameters': {'type': 'object', 'properties': {
                'sym': {'type': 'string', 'description': '品种代码，如 MA、rb；留空=全部'},
                'period': {'type': 'string', 'description': '周期，如 15m、3m、60m'},
                'verdict': {'type': 'string', 'description': '信号判定值，如 confirmed / trig'},
                'limit': {'type': 'integer', 'description': '返回条数上限，默认 15，最大 40'}},
                'required': []}}},
        {'type': 'function', 'function': {
            'name': 'calc_stats',
            'description': '分组统计：n/胜率/盈亏合计/均值/盈亏比，样本≥3 时附单样本 t 检验'
                           '（盈亏均值是否显著≠0）。支持按品种/周期/信号判定/处置分组。',
            'parameters': {'type': 'object', 'properties': {
                'group_by': {'type': 'string', 'enum': ['sym', 'period', 'verdict', 'disposition', 'all']}},
                'required': ['group_by']}}},
        {'type': 'function', 'function': {
            'name': 'query_audit',
            'description': '查询审计条目（每条带证据编号[Axx]）：审计维度、严重级别 certain/suspect/info、'
                           '品种、小时、说明文字。可按交易 id 或严重级别过滤。',
            'parameters': {'type': 'object', 'properties': {
                'trade': {'type': 'string', 'description': '交易 id（来自 query_trades 行的 trade 字段）'},
                'sev': {'type': 'string', 'enum': ['certain', 'suspect', 'info']},
                'limit': {'type': 'integer', 'description': '上限，默认 20'}},
                'required': []}}},
        {'type': 'function', 'function': {
            'name': 'query_questions',
            'description': '读取体系疑问清单（open 状态，每条带证据编号[Qxx]）——他悬而未决的问题。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
        {'type': 'function', 'function': {
            'name': 'get_user_notes',
            'description': '读取他本人写的字：本周三问回答、行动项、上周教练报告。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    ]
    return schemas, executors
