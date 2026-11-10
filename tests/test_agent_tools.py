# -*- coding: utf-8 -*-
"""Agent v2 工具层自跑式单测：注册表 / 过滤 / 统计 / 证据编号 / schema 合法性。
用法：.venv/bin/python tests/test_agent_tools.py"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import tools as T, prompts as P

_n = [0]
def T_(name, cond, info=''):
    _n[0] += 1
    print(('  PASS' if cond else '  FAIL') + f' {name}' + (f'  {info}' if info and not cond else ''))
    if not cond:
        _n[0] = -999

# 合成数据包：2 品种 × 2 周期，5 笔交易（含盈亏/裁决），3 条审计，2 条疑问
PACK = {
    'week': '2026-W46',
    'days': {'2026-11-09': {'status': 'traded', 'opens': 3},
             '2026-11-10': {'status': 'watch', 'opens': 1}},
    'compliance': {'compliance': 80, 'total': 3, 'violation': 1, 'certain': 1,
                   'misjudge': 0, 'suspect': 1, 'info': 1, 'vio_syms': {'MA': 1}, 'vio_hours': {'14': 1}},
    'trade_flow': [
        {'sym': 'MA', 'name': '甲醇', 'period': '15m', 'entry': 'A类二买', 'open': '2026-11-09 10时',
         'close': '2026-11-09 14时', 'open_price': 2500, 'lots': 1, 'pnl': 300,
         'verdict': 'confirmed', 'dispositions': {'破位': 'violation'}},
        {'sym': 'MA', 'name': '甲醇', 'period': '3m', 'entry': 'B类三买', 'open': '2026-11-09 13时',
         'close': '2026-11-09 15时', 'open_price': 2510, 'lots': 1, 'pnl': -120,
         'verdict': 'trig', 'dispositions': {}},
        {'sym': 'rb', 'name': '螺纹钢', 'period': '15m', 'entry': 'A类二买', 'open': '2026-11-10 09时',
         'close': '2026-11-10 11时', 'open_price': 3300, 'lots': 2, 'pnl': 500,
         'verdict': 'confirmed', 'dispositions': {}},
        {'sym': 'rb', 'name': '螺纹钢', 'period': '15m', 'entry': 'A类二买', 'open': '2026-11-10 13时',
         'close': '2026-11-10 15时', 'open_price': 3310, 'lots': 1, 'pnl': 80,
         'verdict': 'confirmed', 'dispositions': {'浮盈加仓': 'suspect'}},
        {'sym': 'MA', 'name': '甲醇', 'period': '60m', 'entry': 'B类三买', 'open': '2026-11-10 21时',
         'close': None, 'open_price': 2490, 'lots': 1, 'pnl': None,
         'verdict': 'watch', 'dispositions': None},
        {'sym': 'MA', 'name': '甲醇', 'period': '15m', 'entry': 'A类二买', 'open': '2026-11-10 10时',
         'close': '2026-11-10 13时', 'open_price': 2505, 'lots': 1, 'pnl': 150,
         'verdict': 'confirmed', 'dispositions': {}},
    ],
    'audit_items': [
        {'key': 'k1', 'trade': 't-1', 'type': '止损执行', 'sev': 'certain', 'sym': 'MA', 'hour': 14, 'detail': '并发越界'},
        {'key': 'k2', 'trade': 't-4', 'type': '浮盈加仓', 'sev': 'suspect', 'sym': 'rb', 'hour': 13, 'detail': ''},
        {'key': 'k3', 'trade': None, 'type': '复盘缺失', 'sev': 'info', 'sym': None, 'hour': None, 'detail': '周三未写复盘'},
    ],
    'answers': {'该走没走_该进没进': 'MA 3m 三买追高了'},
    'actions': ['下周只做 15m', 'MA 连错停一天'],
    'last_week_coach': '上周说过：不要在 3m 追信号。' * 3,
    'questions_open': [{'text': 'A类确认后的回撤到底该忍到哪里？', 'created': '2026-11-08'},
                       {'text': '60m 观察仓要不要计入并发？', 'created': '2026-11-05'}],
}

schemas, executors = T.build_registry(PACK)

def main():
    names = [s['function']['name'] for s in schemas]
    T_('注册 6 个工具', set(names) == {'get_week_overview', 'query_trades', 'calc_stats',
                                      'query_audit', 'query_questions', 'get_user_notes'})
    T_('schema 均为 OpenAI function 格式', all(
        s['type'] == 'function' and isinstance(s['function']['parameters'], dict) for s in schemas))

    ov = executors['get_week_overview']({})
    T_('总览：交易数=6', ov['n_trades'] == 6)
    T_('总览：审计计数=3', ov['n_audits'] == 3)
    T_('总览：严重级别聚合', ov['audit_by_severity'] == {'certain': 1, 'suspect': 1, 'info': 1})
    T_('总览：交易日带证据编号', ov['days'][0]['eid'] == 'D01')

    qt = executors['query_trades']({'sym': 'MA'})
    T_('流水过滤：MA 命中 4/6', qt['matched'] == 4 and qt['total'] == 6)
    T_('流水：证据编号 T 前缀', all(r['eid'].startswith('T') for r in qt['rows']))
    qt2 = executors['query_trades']({'verdict': 'confirmed', 'limit': 1})
    T_('流水过滤：verdict+limit=1', qt2['matched'] == 1)

    st = executors['calc_stats']({'group_by': 'sym'})
    ma = next(g for g in st['groups'] if g['group'] == 'MA')
    T_('统计：MA 组 n=4（pnl=None 不计胜率）', ma['n'] == 4)
    T_('统计：MA 组胜率 66.7%（300/-120/150，None 不计）', ma['win_rate'] == 66.7, str(ma))
    T_('统计：MA 组样本≥3 附 t 检验', 'p_val' in ma)
    st2 = executors['calc_stats']({'group_by': 'disposition'})
    T_('统计：处置分组含"未处置"', any(g['group'] == '未处置' for g in st2['groups']))

    qa = executors['query_audit']({'sev': 'certain'})
    T_('审计过滤：certain 命中 1 条', qa['matched'] == 1 and qa['rows'][0]['eid'] == 'A01')
    qa2 = executors['query_audit']({'trade': 't-4'})
    T_('审计过滤：按交易 id', qa2['matched'] == 1 and qa2['rows'][0]['type'] == '浮盈加仓')

    qq = executors['query_questions']({})
    T_('疑问清单：Q 编号 + 文本', qq['rows'][0]['eid'] == 'Q01' and '回撤' in qq['rows'][0]['text'])

    un = executors['get_user_notes']({})
    T_('用户输入：三问/行动项/上周报告齐备', un['eid'] == 'U1' and un['actions'] == PACK['actions']
      and '追信号' in (un['last_week_coach'] or ''))

    err = executors['no_such_tool'] if False else None
    unknown = executors.get('nope')
    T_('未知工具不进注册表（模型侧由 dispatch 报错回灌）', unknown is None)

    T_('prompt v4：研究阶段指令在位', '第一阶段' in P.WEEKLY_SYSTEM and 'get_week_overview' in P.WEEKLY_SYSTEM)
    T_('prompt v4：证据编号要求在位', '证据编号' in P.WEEKLY_SYSTEM)
    T_('prompt v4：教练规矩延续', '600-900 字' in P.WEEKLY_SYSTEM and '反驳或挑战' in P.WEEKLY_SYSTEM)

    # 证据编号回链：模型输出 → citations 提取（weekly_analyst 同款正则）
    import re
    text = '你 [T01] 这笔在 [A01] 记了并发越界，跟你 [U1] 写的"下周只做 15m"矛盾，[Q01] 还挂着。'
    cits = sorted(set(re.findall(r'\b[TAQD]\d{1,2}\b|\bU1\b', text)))
    T_('证据回链提取：4 条编号', cits == ['A01', 'Q01', 'T01', 'U1'])

    print()
    if _n[0] < 0:
        print(f'[agent] {len([1])} 组用例存在 FAIL'); sys.exit(1)
    print(f'[agent] 全部通过（{_n[0]} 项）')

if __name__ == '__main__':
    main()
