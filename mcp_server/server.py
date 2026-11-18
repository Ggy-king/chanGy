"""MCP server：把 Agent 工具链标准化暴露给任何 MCP 客户端（Claude Desktop / Cursor 等）。

设计文档：docs/MCP标准化设计.md（v0.2 定稿）
原则：跨进程暴露面只开读，不开写；数据一律走 agent/tools 脱敏收口，不得绕过。

运行：
    python -m mcp_server.server    # stdio 传输，阻塞等待 client

接入（Claude Desktop 为例，claude_desktop_config.json）：
    "chanGy": {"command": "/path/to/.venv/bin/python", "args": ["-m", "mcp_server.server"],
               "cwd": "/path/to/chanGy"}
"""
from mcp.server.fastmcp import FastMCP

import agent.tools as agent_tools
import server as gy            # 数据层单一来源；import 安全（uvicorn/引擎仅在 __main__/lifespan 启动）

mcp = FastMCP(
    "chanGy",
    instructions="缠论期货监控系统：复盘统计 / 交易流水 / 审计条目 / 体系疑问 / 缠论知识库。全部工具只读，"
                 "数据经过脱敏（时间到小时、无资金总量）；知识库检索结果带课号出处，检索无命中会明说。")


def _executors(week: str = ''):
    """按周构建工具执行器（与周复盘 Agent 同一数据层、同一脱敏收口）。"""
    d = gy._weekly_data(week or '')
    pack = gy._agent_weekly_pack(d['week'])
    _, executors = agent_tools.build_registry(pack)
    return executors


def _call(name: str, week: str = '', **kwargs):
    return _executors(week)[name](kwargs)


@mcp.tool()
def get_week_overview(week: str = "") -> dict:
    """一周总览：交易日明细、合规率与审计计数、交易笔数、open 疑问数。week 格式如 2026-W45，留空=本周。"""
    return _call('get_week_overview', week)


@mcp.tool()
def query_trades(week: str = "", sym: str = "", period: str = "", verdict: str = "", limit: int = 15) -> dict:
    """查询本周交易流水（每笔带证据编号[Txx]），可按品种/周期/信号判定过滤。"""
    return _call('query_trades', week, sym=sym, period=period, verdict=verdict, limit=limit)


@mcp.tool()
def calc_stats(week: str = "", group_by: str = "all") -> dict:
    """分组统计：胜率/盈亏合计/盈亏比，样本≥3 附 t 检验。group_by: sym/period/verdict/disposition/all。"""
    return _call('calc_stats', week, group_by=group_by)


@mcp.tool()
def query_audit(week: str = "", trade: str = "", sev: str = "", limit: int = 20) -> dict:
    """查询审计条目（每条带证据编号[Axx]）：维度/严重级别/品种/小时/说明。sev: certain/suspect/info。"""
    return _call('query_audit', week, trade=trade, sev=sev, limit=limit)


@mcp.tool()
def query_questions(week: str = "") -> dict:
    """读取体系疑问清单（open 状态，每条带证据编号[Qxx]）。"""
    return _call('query_questions', week)


@mcp.tool()
def get_user_notes(week: str = "") -> dict:
    """读取本人写的字：本周三问回答、行动项、上周教练报告。"""
    return _call('get_user_notes', week)


@mcp.tool()
def kb_search(query: str, top_k: int = 3) -> dict:
    """查询缠论知识库（双层：108 课原文 + 本系统私有口径），每条带证据编号[Kxx]与出处。
    引用原文标注课号；检索无相关内容会如实说明。"""
    return agent_tools._kb_lookup({'query': query, 'top_k': min(max(top_k, 1), 5)})


if __name__ == '__main__':
    mcp.run()   # stdio 传输（v0.2 定稿：一期不做网络暴露）

# 同源断言用：从 FastMCP 实际注册状态推导（非手写清单，防漂移）
MCP_TOOL_NAMES = {t.name for t in mcp._tool_manager.list_tools()}
