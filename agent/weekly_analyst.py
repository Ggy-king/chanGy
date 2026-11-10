"""周复盘分析师（Agent v2，工具调用架构）。
与 v1 的区别：不再是「整包数据塞进单个 prompt 的一次性调用」，而是真正的
研究型 Agent——模型通过 function calling 自主查询本地数据（tools.build_registry
提供的六个工具），多轮「规划 → 查询 → 追问 → 对比」，证据拿够后输出最终深谈。

只读，不写任何交易/审计数据；存档由 server 落到 reviews.json 的 weekly[week].agent
（新增 citations / tools_used / rounds 字段，支撑前端证据链回查）。"""
import re
import time

from . import llm, prompts, tools

_EVIDENCE_RE = re.compile(r'\b[TAQD]\d{1,2}\b|\bU1\b')   # 证据编号：T/A/Q/D 两位序号 + U1


def run(pack):
    """分析一个周数据包，返回 {text, citations, tools_used, rounds, model, created}。
    模型接口失败 / 轮次超限抛 RuntimeError（消息可直接展示给用户）。"""
    schemas, executors = tools.build_registry(pack)
    used = []

    def dispatch(name, args):
        fn = executors.get(name)
        if fn is None:
            return {'error': f'未知工具 {name}，可用：{", ".join(executors)}'}
        used.append(name)
        return fn(args or {})

    user = ('本周复盘数据包已就绪（研究人员请注意：数据不直接给你，全部通过工具查询）。'
            '按系统指令完成两个阶段：先用工具把数据挖透，再输出最终深谈。')
    res = llm.chat_tools(prompts.WEEKLY_SYSTEM, [{'role': 'user', 'content': user}],
                         schemas, dispatch)

    text = res['text']
    citations = sorted(set(_EVIDENCE_RE.findall(text)))
    return {'text': text,
            'citations': citations,
            'tools_used': used,
            'rounds': res['rounds'],
            'model': llm.get_model(),
            'created': time.strftime('%Y-%m-%d %H:%M')}


def _summary(res):
    """落库/日志用的紧凑摘要：模型 + 轮次 + 工具调用数 + 证据数。"""
    return (f"{res.get('model')} / {res.get('rounds')} 轮 / "
            f"{len(res.get('tools_used') or [])} 次工具调用 / "
            f"{len(res.get('citations') or [])} 条证据引用")
