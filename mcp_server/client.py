"""MCP client：外部 MCP 工具 → 本地注册表格式的桥（client 模式，设计文档 v0.3）。

方向与 server 相反：server.py 是"外部 AI 应用连我们"的门面；本文件是"我们连外部
MCP server"的桥——把外部工具动态翻译成本地注册表格式 (schemas, executors)，
与 agent/tools 的函数工具合并进同一张菜单。模型只看到一张扁平菜单，不感知
也不需要感知某道菜是本地函数还是跨进程协议（JSON-RPC over stdio）。

一期只接一个外部 server：官方 fetch（`uvx mcp-server-fetch`，开源免费无 API key），
补"盘后资讯/网页抓取"能力。不为接而接：memory/filesystem 等不接（设计文档 §3）。

约束：
- 不挂进周复盘 Agent（7 工具封顶纪律）；供留言窗口/主控台 registry 合并使用
- 每次调用独立起会话——fetch 无状态，连接成本 ~1-3s，换零会话状态管理
- 结果按 agent.llm.TOOL_RESULT_LIMIT 截断，防大网页撑爆上下文
- 工具清单从外部 server 动态拉取（与 MCP_TOOL_NAMES 同哲学：防手写清单漂移）
"""
import asyncio
import os
import shutil

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.llm import TOOL_RESULT_LIMIT

_CALL_TIMEOUT = 120   # 单次调用上限：fetch server 内部有站点级超时，这里再兜一层
_LIST_TIMEOUT = 60


def _uvx():
    """uvx 路径：PATH 找不到时兜 ~/.local/bin（uv 默认安装位）。"""
    p = shutil.which('uvx')
    if p:
        return p
    p = os.path.expanduser('~/.local/bin/uvx')
    return p if os.path.exists(p) else None


def _server_params():
    uvx = _uvx()
    if not uvx:
        raise RuntimeError('未找到 uvx（uv 未装？装：curl -LsSf https://astral.sh/uv/install.sh | sh）')
    return StdioServerParameters(command=uvx, args=['mcp-server-fetch'])


def _blocks_text(result):
    out = []
    for b in (result.content or []):
        t = getattr(b, 'text', None)
        if t:
            out.append(t)
    return '\n'.join(out).strip()


async def _acall(tool, args):
    """独立会话调一次外部工具：连接 → initialize → call → 提取文本。"""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await asyncio.wait_for(session.call_tool(tool, args or {}), _CALL_TIMEOUT)
    if getattr(result, 'isError', False):
        raise RuntimeError(_blocks_text(result) or '外部工具返回错误')
    return _blocks_text(result)


def _exec(tool):
    def run(args):
        try:
            text = asyncio.run(_acall(tool, args))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f'fetch 调用失败（网络/站点问题，可稍后重试）: {type(e).__name__}: {e}')
        return text[:TOOL_RESULT_LIMIT] if text else '（外部工具返回空内容）'
    return run


async def _alist():
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await asyncio.wait_for(session.list_tools(), _LIST_TIMEOUT)
    return tools.tools


def fetch_registry():
    """外部 fetch server → 本地注册表格式 (schemas, executors)。
    清单与描述取自外部 server 实际注册状态（动态，不手写）。
    连接失败抛 RuntimeError（消息可直接展示给用户）。"""
    tools = asyncio.run(_alist())
    schemas, executors = [], {}
    for t in tools:
        schemas.append({'type': 'function', 'function': {
            'name': t.name,
            'description': (t.description or '').strip(),
            'parameters': t.inputSchema or {'type': 'object', 'properties': {}, 'required': []}}})
        executors[t.name] = _exec(t.name)
    return schemas, executors


def merge_registry(base_schemas, base_executors, ext_schemas, ext_executors):
    """本地注册表 ⊕ 外部工具：重名即拒——菜单唯一性是模型选工具正确率的前提。"""
    names = {s['function']['name'] for s in base_schemas}
    for s in ext_schemas:
        if s['function']['name'] in names:
            raise RuntimeError(f"工具重名冲突: {s['function']['name']}")
    return list(base_schemas) + list(ext_schemas), {**base_executors, **ext_executors}
