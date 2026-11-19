"""周复盘分析师（Agent v3，LangGraph 图编排）。
v2：function calling 手写循环（llm.chat_tools，现仍服务留言问答）。
v3（2026-11-18）：换 agent/graph.py StateGraph——research ⇄ execute_tools 条件边流转、
coach 独立节点（研究证据硬闸门）、checkpoint 落盘断点续跑。
设计文档：docs/LangGraph编排升级设计.md（v0.2 定稿）。

只读，不写任何交易/审计数据；存档由 server 落到 reviews.json 的 weekly[week].agent
（新增 thread_id 字段=checkpoint 断点钥匙，供中断续跑）。"""
import re
import time

from . import graph, llm

_EVIDENCE_RE = re.compile(r'\b[TAQD]\d{1,2}\b|\bU1\b')   # 证据编号：T/A/Q/D 两位序号 + U1


def run(pack, thread_id=None):
    """分析一个周数据包，返回 {text, citations, tools_used, rounds, model, created, thread_id}。
    模型接口失败 / 轮次超限抛 RuntimeError（消息可直接展示给用户）。
    thread_id 传入已有值=从 checkpoint 断点续跑。"""
    res = graph.run(pack, thread_id)
    text = res['text']
    citations = sorted(set(_EVIDENCE_RE.findall(text)))
    return {'text': text,
            'citations': citations,
            'tools_used': res['tools_used'],
            'rounds': res['rounds'],
            'model': llm.get_model(),
            'created': time.strftime('%Y-%m-%d %H:%M'),
            'thread_id': res['thread_id']}


def _summary(res):
    """落库/日志用的紧凑摘要：模型 + 轮次 + 工具调用数 + 证据数。"""
    return (f"{res.get('model')} / {res.get('rounds')} 轮 / "
            f"{len(res.get('tools_used') or [])} 次工具调用 / "
            f"{len(res.get('citations') or [])} 条证据引用")
