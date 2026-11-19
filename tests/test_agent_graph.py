# -*- coding: utf-8 -*-
"""Agent v3 图编排单测：mock llm.chat_step 脚本化对话，验证路由/硬闸门/工具回灌/轮次上限。
零真实模型调用。用法：.venv/bin/python -m pytest tests/test_agent_graph.py -v"""
import json
import os

import pytest

from agent import graph as G
from uuid import uuid4


def _tid(name):
    """唯一 thread_id：checkpoint 按 thread 落盘，同 id 会从旧断点续跑——测试必须每局新开。"""
    return f't-{name}-{uuid4().hex[:8]}'
from agent.tools import build_registry
from test_agent_tools import PACK

_n = {'seq': 0}


def _calls(*specs):
    """构造一轮 tool_calls（specs = [(name, args), ...]）。"""
    calls = [{'id': f'c{_n["seq"]}-{i}', 'type': 'function',
              'function': {'name': n, 'arguments': json.dumps(a or {})}}
             for i, (n, a) in enumerate(specs)]
    return {'content': '', 'tool_calls': calls,
            'assistant_msg': {'role': 'assistant', 'content': None, 'tool_calls': calls}}


def _text(t):
    return {'content': t, 'tool_calls': [],
            'assistant_msg': {'role': 'assistant', 'content': t}}


def _script(responses):
    """按序回放的假 chat_step（系统提示词/消息原样收下不校验）。"""
    state = {'i': 0}

    def fake(system, messages, tools=None):
        state['i'] += 1
        assert responses, f'脚本耗尽：实际收到第 {state["i"]} 次调用'
        return responses.pop(0)
    return fake, state


def test_graph_happy_path_with_gate(monkeypatch, tmp_path):
    """正常剧本：总览→流水→笔记+统计(2个)→提前收工被闸门打回→再统计→闸门放行→coach 深谈。"""
    script = [
        _calls(('get_week_overview', {})),                       # 1 次调用
        _calls(('query_trades', {'sym': 'MA'})),                 # 2
        _calls(('get_user_notes', {}), ('calc_stats', {'group_by': 'sym'})),  # 4
        _text('我觉得可以直接出结论了'),                           # 闸门：4<5，打回
        _calls(('calc_stats', {'group_by': 'period'})),          # 5 → 闸门过
        _text('数据齐了，先给个摘要'),                             # 闸门过 → 进 coach
        _text('你 [T01] 这笔止损没执行 [A01]，跟你 [U1] 写的"下周只做 15m"矛盾，[Q01] 还挂着。'
              '下周就做一件事：开盘前把止损单挂好。'),             # coach 最终深谈
    ]
    fake, st = _script(script)
    monkeypatch.setattr(G.llm, 'chat_step', fake)

    schemas, executors = build_registry(PACK)   # 工具真执行（本地纯函数）
    res = G.run(PACK, thread_id=_tid('happy'))
    assert res['text'].startswith('你 [T01]')
    assert res['tools_used'] == ['get_week_overview', 'query_trades',
                                 'get_user_notes', 'calc_stats', 'calc_stats']
    assert res['rounds'] == 7                   # 6 次 research + 1 次 coach
    assert '上限' not in res['text']


def test_graph_gate_blocks_early_exit(monkeypatch):
    """硬闸门：模型只调 2 次就想收工 → 打回补查；补够才放行。"""
    script = [
        _calls(('get_week_overview', {})),
        _calls(('get_user_notes', {})),
        _text('直接给结论吧'),                    # 2<5 → 闸门拦下
        _calls(('calc_stats', {'group_by': 'sym'})),
        _calls(('query_trades', {})),
        _calls(('query_audit', {})),              # 5 次 → 放行
        _text('证据够了。你 [A01] 这条审计 [Q01] 跟了三周。'),   # 研究最终输出 → 进 coach
        _text('结论：你 [A01] 这条审计三周没闭环，下周必须处置。'),  # coach 深谈
    ]
    fake, _ = _script(script)
    monkeypatch.setattr(G.llm, 'chat_step', fake)
    res = G.run(PACK, thread_id=_tid('gate'))
    assert res['text'].startswith('结论：')
    assert len(res['tools_used']) == 5           # overview+notes+calc+trades+audit=5


def test_graph_round_limit_fallback(monkeypatch):
    """模型永远只输出正文、闸门永远不满足 → 打回 12 轮后 fallback 报错（可直接展示给用户）。"""
    fake, _ = _script([_text('我就不出工具，我就要收工')] * 13)
    monkeypatch.setattr(G.llm, 'chat_step', fake)
    with pytest.raises(RuntimeError, match='上限'):
        G.run(PACK, thread_id=_tid('limit'))


def test_graph_tool_error_backfeed(monkeypatch):
    """未知工具名 → {'error':...} 回灌（坏结果也是信息），不炸图。"""
    script = [
        _calls(('no_such_tool', {})),            # 未知工具 → 错误回灌
        _calls(('get_week_overview', {})),
        _calls(('get_user_notes', {})),
        _calls(('calc_stats', {'group_by': 'sym'})),
        _calls(('query_trades', {})),
        _text('好，出结论。你 [U1] 的行动项还没勾。'),
        _text('结论：你 [U1] 写了"下周只做 15m"，[T01] 却做了 60m。'),
    ]
    fake, _ = _script(script)
    monkeypatch.setattr(G.llm, 'chat_step', fake)
    res = G.run(PACK, thread_id=_tid('err'))
    assert res['text'].startswith('结论')


def test_checkpoint_persisted():
    """checkpoint 落盘 data/agent_state/weekly.sqlite（v0.2 决定 2）。"""
    from agent.graph import _saver
    assert _saver() is not None
    root = os.path.dirname(os.path.dirname(os.path.abspath(G.__file__)))
    assert os.path.exists(os.path.join(root, 'data', 'agent_state', 'weekly.sqlite'))


def test_weekly_analyst_result_shape(monkeypatch):
    """weekly_analyst 落结果层回归（2026-11-18 E2E 实跑抓出 llm.get_model 不存在——
    mock 测试只打到 graph.run，没走这层，补上）。"""
    import agent.weekly_analyst as WA
    monkeypatch.setattr(WA.graph, 'run', lambda pack, thread_id=None: {
        'text': '你 [T01] 这笔止损没执行。', 'tools_used': ['a', 'b'],
        'rounds': 3, 'thread_id': 'tid-1'})
    res = WA.run({})
    assert res['citations'] == ['T01']
    assert res['thread_id'] == 'tid-1'
    assert res['model']                       # get_model 生效
    assert res['rounds'] == 3
    assert '3 轮' in WA._summary(res)
