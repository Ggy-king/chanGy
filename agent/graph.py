"""LangGraph 编排引擎（Agent v3）——chat_tools 手写循环的图化。
设计文档：docs/LangGraph编排升级设计.md（v0.2 定稿：A 引擎 / 落盘 checkpoint / 独立 coach / chat_tools 留活）

图结构：
    START → research ⇄ execute_tools ──(证据够格)──→ coach → END
                 └──(轮次超限)──→ fallback（报错出口，消息可直接展示给用户）

与 chat_tools 手写循环的概念对照（学习锚点，详见 llm.chat_tools 文档注释）：
    手写 for 循环                      → 本文件
    msgs 局部变量                      → State（TypedDict，纯 JSON，checkpoint 可落盘）
    if calls: continue                → route 条件边
    MAX_TOOL_ROUNDS 封顶              → fallback 路由 + recursion_limit 双保险
    工具报错回灌 {'error':...}        → execute_tools 节点容错
    循环里自然发生的"第二阶段"        → coach 独立节点（证据不够硬闸门拦下，prompt 叮嘱变代码约束）
    （无）                             → Checkpoint：SqliteSaver 落盘 data/agent_state/，thread_id 断点续跑
"""
import json
import operator
import os
import sqlite3
import time
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from . import llm, prompts, tools

MAX_TOOL_ROUNDS = 12        # 与 chat_tools 同口径：模型请求轮次上限
RECURSION_LIMIT = 60        # 图流转步数上限（每轮 research+execute 约 2 步，留余量）
GATE_MIN_CALLS = 5          # 研究阶段硬闸门：至少 5 次工具调用（prompt v4 的叮嘱变代码）
GATE_REQUIRED = ('calc_stats', 'get_user_notes')   # 必查工具（同 prompt v4 口径）


class AgentState(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # 对话历史（reducer 追加）
    tools_used: Annotated[list, operator.add] # 工具调用名流水（含重复，闸门数它）
    rounds: int                               # 模型请求轮次
    pending_calls: list                       # 本轮待执行 tool_calls（execute_tools 消费后清空）
    final_text: str                           # coach 产出


def _gate_ok(state):
    """研究阶段放行条件：≥5 次工具调用且 calc_stats / get_user_notes 必查（prompt v4 口径）。"""
    used = state.get('tools_used') or []
    return len(used) >= GATE_MIN_CALLS and all(t in used for t in GATE_REQUIRED)


def _saver():
    """SqliteSaver 单例：checkpoint 落盘 data/agent_state/（v0.2 决定 2）。"""
    global _SAVER
    if _SAVER is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, 'data', 'agent_state')
        os.makedirs(path, exist_ok=True)
        conn = sqlite3.connect(os.path.join(path, 'weekly.sqlite'), check_same_thread=False)
        _SAVER = SqliteSaver(conn)
    return _SAVER


_SAVER = None


def run(pack, thread_id=None):
    """跑一次周复盘分析图。返回 {'text', 'tools_used', 'rounds', 'thread_id'}。
    thread_id 缺省=新起一局；传入已有 thread_id=从 checkpoint 断点续跑（中断恢复）。
    模型接口失败 / 轮次超限抛 RuntimeError（消息可直接展示给用户）。"""
    schemas, executors = tools.build_registry(pack)
    system = prompts.WEEKLY_SYSTEM

    def research(state):
        """研究员节点：一次 LLM 请求。有 tool_calls 交执行节点；正文提前输出且证据不足则打回。"""
        rounds = state.get('rounds', 0) + 1
        res = llm.chat_step(system, state['messages'], schemas)
        out = {'rounds': rounds, 'pending_calls': None}
        if res['tool_calls']:
            out['messages'] = [res['assistant_msg']]
            out['pending_calls'] = res['tool_calls']
            out['tools_used'] = [((c.get('function') or {}).get('name') or '') for c in res['tool_calls']]
        else:
            msgs = [res['assistant_msg']]
            if not _gate_ok(state):   # 硬闸门：想提前收工，先补证据
                msgs.append({'role': 'user', 'content':
                    f'研究阶段证据不足：当前 {len(state.get("tools_used") or [])} 次工具调用，'
                    f'要求至少 {GATE_MIN_CALLS} 次且 calc_stats / get_user_notes 必查。'
                    '请继续用工具补查，暂不要输出最终深谈。'})
            else:
                out['final_text'] = res['content']   # 闸门放行 → 路由进 coach
            out['messages'] = msgs
        return out

    def route(state):
        if state.get('rounds', 0) >= MAX_TOOL_ROUNDS:
            return 'fallback'
        if state.get('pending_calls'):
            return 'execute_tools'
        if _gate_ok(state) and state.get('final_text'):
            return 'coach'
        return 'research'

    def execute_tools(state):
        """工具执行节点：消费 pending_calls，结果（含报错）回灌——坏结果也是信息。"""
        msgs = []
        for tc in (state.get('pending_calls') or []):
            name = ((tc.get('function') or {}).get('name') or '')
            try:
                args = json.loads((tc.get('function') or {}).get('arguments') or '{}')
            except Exception:
                args = {}
            try:
                fn = executors.get(name)
                result = fn(args or {}) if fn else {'error': f'未知工具 {name}，可用：{", ".join(executors)}'}
            except Exception as e:
                result = {'error': f'{type(e).__name__}: {e}'}
            msgs.append({'role': 'tool', 'tool_call_id': tc.get('id') or '',
                         'content': json.dumps(result, ensure_ascii=False)[:llm.TOOL_RESULT_LIMIT]})
        return {'messages': msgs, 'pending_calls': None}

    def coach(state):
        """教练节点：证据够格后的最终深谈（不带工具，杜绝最终轮再查数据）。"""
        msgs = list(state['messages']) + [{'role': 'user', 'content':
            '研究阶段证据已足够。现在进入第二阶段（教练）：按系统指令输出最终深谈——'
            '每条论断带证据编号，600-900 字，一大段连贯的话，直接开讲。'}]
        res = llm.chat_step(system, msgs, tools=None)
        return {'rounds': state.get('rounds', 0) + 1, 'final_text': res['content']}

    def fallback(state):
        raise RuntimeError(f'编排轮次达到上限（{MAX_TOOL_ROUNDS}）仍未收敛出结论，'
                           '可减少数据量或更换模型后重试')

    g = StateGraph(AgentState)
    g.add_node('research', research)
    g.add_node('execute_tools', execute_tools)
    g.add_node('coach', coach)
    g.add_node('fallback', fallback)
    g.add_edge(START, 'research')
    g.add_conditional_edges('research', route,
                            {'execute_tools': 'execute_tools', 'coach': 'coach',
                             'research': 'research', 'fallback': 'fallback'})
    g.add_edge('execute_tools', 'research')
    g.add_edge('coach', END)
    app = g.compile(checkpointer=_saver())

    if not thread_id:
        thread_id = f"weekly_{pack.get('week') or time.strftime('%Y-%W')}_{time.strftime('%Y%m%d_%H%M%S')}"
    user = ('本周复盘数据包已就绪（研究人员请注意：数据不直接给你，全部通过工具查询）。'
            '按系统指令完成两个阶段：先用工具把数据挖透，再输出最终深谈。')
    res = app.invoke({'messages': [{'role': 'user', 'content': user}]},
                     config={'configurable': {'thread_id': thread_id},
                             'recursion_limit': RECURSION_LIMIT})
    return {'text': res['final_text'], 'tools_used': res.get('tools_used') or [],
            'rounds': res.get('rounds', 0), 'thread_id': thread_id}
