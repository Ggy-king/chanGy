"""MCP client（fetch 桥）实测：真实拉起 uvx mcp-server-fetch 走完整链路。
无 uvx 环境自动跳过（不假装通过）；example.com 为最小稳定目标。
"""
import os
import shutil

import pytest

from mcp_server.client import fetch_registry, merge_registry

pytestmark = pytest.mark.skipif(
    not (shutil.which('uvx') or os.path.exists(os.path.expanduser('~/.local/bin/uvx'))),
    reason='uvx 未安装')


def test_registry_discovers_fetch_tool():
    schemas, executors = fetch_registry()
    names = [s['function']['name'] for s in schemas]
    assert 'fetch' in names
    assert 'fetch' in executors
    schema = next(s for s in schemas if s['function']['name'] == 'fetch')
    assert 'url' in schema['function']['parameters'].get('properties', {})


def test_fetch_example_com_end_to_end():
    _, executors = fetch_registry()
    res = executors['fetch']({'url': 'https://example.com'})
    assert 'Example Domain' in res
    assert len(res) <= 6000   # 截断纪律生效


def test_merge_registry_detects_collision():
    base_s = [{'type': 'function', 'function': {'name': 'kb_search'}}]
    ext_s = [{'type': 'function', 'function': {'name': 'kb_search'}}]
    with pytest.raises(RuntimeError, match='重名'):
        merge_registry(base_s, {}, ext_s, {})
