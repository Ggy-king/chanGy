"""模型接入层（Agent 唯一的联网出口）——供应商中立，OpenAI 兼容协议。

设计：URL / 模型名 / key 全部走 secret.py 配置，代码里不绑定任何供应商；
换供应商（DeepSeek / OpenAI / 本地 Ollama 等）只改 secret.py 三行，上层模块不动。

secret.py 配置项：
    LLM_API_URL   chat/completions 完整地址
    LLM_MODEL     模型名
    LLM_API_KEY   API key

兼容旧配置（其他机器 secret.py 还是旧字段名时仍可用，新机器请用上面三个）：
    ZHIPU_API_KEY / ZHIPU_MODEL
"""
import json
import time
import requests

TIMEOUT = 150   # 深度思考模型跑大数据包很慢（实测 >60s），放宽到 150s


def _cfg():
    """读取配置：新字段优先，旧字段名兜底（多机同步过渡期）。"""
    try:
        import secret
    except Exception:
        return None
    c = {
        'url': getattr(secret, 'LLM_API_URL', '') or None,
        'model': getattr(secret, 'LLM_MODEL', '') or None,
        'key': getattr(secret, 'LLM_API_KEY', '') or None,
        # 旧字段名兜底：key 在则推断为默认 OpenAI 兼容端点（多机过渡期，公开仓库版会移除）
        'old_key': getattr(secret, 'ZHIPU_API_KEY', '') or None,
        'old_model': getattr(secret, 'ZHIPU_MODEL', '') or None,
    }
    if not c['key'] and c['old_key']:
        c['key'] = c['old_key']
        c['url'] = c['url'] or 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        c['model'] = c['model'] or 'glm-4.7-flash'
    if not c['model']:
        c['model'] = c['old_model']
    return c if (c['url'] and c['key'] and c['model']) else None


def get_key():
    """未配置返回 None。"""
    c = _cfg()
    return c['key'] if c else None


def _missing_msg():
    return ('LLM 未配置：在 secret.py 加三行（任何 OpenAI 兼容服务均可：'
            '智谱 bigmodel.cn / DeepSeek / OpenAI / 本地 Ollama 等）：\n'
            '  LLM_API_URL = "chat/completions 完整地址"\n'
            '  LLM_MODEL = "模型名"\n'
            '  LLM_API_KEY = "你的key"\n（改完需重启服务）')


def _post(body):
    """统一请求出口：无配置抛错；429/5xx 指数退避重试 3 次（免费模型高峰限流常态）；
    其余非 200 直接抛 RuntimeError（消息可直接展示给用户）。"""
    c = _cfg()
    if not c:
        raise RuntimeError(_missing_msg())
    last = None
    for wait in (0, 8, 20, 45):
        if wait:
            time.sleep(wait)
        r = requests.post(c['url'], headers={'Authorization': 'Bearer ' + c['key']},
                          json=body, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        last = f'模型接口返回 {r.status_code}: {r.text[:200]}'
        if r.status_code not in (429, 500, 502, 503, 504):
            raise RuntimeError(last)
    raise RuntimeError(last)


def chat(system, user):
    """单轮对话，返回回复文本。无配置 / 接口失败抛 RuntimeError（消息可直接展示给用户）。

    深度思考模型默认开思考（实测一句话回复也烧近千个思考 token），
    思考+正文共享 max_tokens 预算——2048 会被思考烧光导致 content 返回空串
    （2026-10-21 周复盘空白事故）。故：预算提到 8192；正文仍为空时
    （finish_reason=length）关掉深度思考再试一次兜底。"""
    c = _cfg()
    if not c:
        raise RuntimeError(_missing_msg())
    body = {'model': c['model'], 'temperature': 0.5, 'max_tokens': 8192,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}]}
    r = requests.post(c['url'], headers={'Authorization': 'Bearer ' + c['key']},
                      json=body, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'模型接口返回 {r.status_code}: {r.text[:200]}')
    try:
        ch = r.json()['choices'][0]
        content = ((ch.get('message') or {}).get('content') or '').strip()
    except Exception:
        raise RuntimeError('模型返回格式异常: ' + r.text[:200])
    if content:
        return content
    if ch.get('finish_reason') == 'length':   # 思考烧光预算 → 关深度思考兜底重试
        r2 = requests.post(c['url'], headers={'Authorization': 'Bearer ' + c['key']},
                           json=dict(body, thinking={'type': 'disabled'}), timeout=TIMEOUT)
        if r2.status_code == 200:
            try:
                content2 = ((r2.json()['choices'][0].get('message') or {})
                            .get('content') or '').strip()
                if content2:
                    return content2
            except Exception:
                pass
        raise RuntimeError('模型思考耗尽了生成长度，关闭深度思考后仍未产出正文'
                           '（可在 secret.py 换 LLM_MODEL 试试）')
    raise RuntimeError('模型没有返回正文: ' + r.text[:200])


# ============ 工具调用循环（function calling） ============

MAX_TOOL_ROUNDS = 12      # 单次分析的模型请求轮次上限：足够完成「总览→切片→对比→合成」，封顶控成本
TOOL_RESULT_LIMIT = 6000  # 单条工具结果注入对话的最大字符数——超出截断，防大数据包撑爆上下文


def chat_tools(system, messages, tools, tool_fn):
    """多轮工具调用循环。模型自主决定何时调工具、调哪个；工具结果由 tool_fn(name, args)
    本地执行后以 role=tool 注入对话，直到模型产出最终正文（或触发轮次上限）。

    - 深度思考默认开启：携带 tool_calls 的中间轮不占正文，仅最终轮消耗正文预算；
      若正文为空且 finish_reason=length，对该轮关闭深度思考重试一次（chat 同款兜底）。
    - 工具执行异常不终止循环：以 {'error': ...} 回给模型让它自行调整查询参数——
      坏结果也是信息，这是 Agent 与固定管道的本质区别。
    返回 {'text', 'rounds', 'tool_calls'}。"""
    msgs = [{'role': 'system', 'content': system}] + list(messages)
    rounds = tool_calls_n = 0
    for _ in range(MAX_TOOL_ROUNDS):
        rounds += 1
        body = {'model': _cfg()['model'], 'temperature': 0.4, 'max_tokens': 8192, 'messages': msgs}
        if tools:
            body['tools'] = tools
        data = _post(body)
        try:
            ch = data['choices'][0]
            msg = ch.get('message') or {}
        except Exception:
            raise RuntimeError('模型返回格式异常: ' + json.dumps(data)[:200])
        calls = msg.get('tool_calls') or []
        content = (msg.get('content') or '').strip()
        if calls:
            msgs.append({'role': 'assistant', 'content': content or None, 'tool_calls': calls})
            for tc in calls:
                name = (tc.get('function') or {}).get('name') or ''
                try:
                    args = json.loads((tc.get('function') or {}).get('arguments') or '{}')
                except Exception:
                    args = {}
                try:
                    result = tool_fn(name, args)
                except Exception as e:   # 工具报错回灌给模型自行纠正，不让单次失败终止分析
                    result = {'error': f'{type(e).__name__}: {e}'}
                msgs.append({'role': 'tool', 'tool_call_id': tc.get('id') or '',
                             'content': json.dumps(result, ensure_ascii=False)[:TOOL_RESULT_LIMIT]})
                tool_calls_n += 1
            continue
        if content:
            return {'text': content, 'rounds': rounds, 'tool_calls': tool_calls_n}
        if ch.get('finish_reason') == 'length':   # 思考烧光预算 → 关深度思考兜底重试
            data2 = _post(dict(body, thinking={'type': 'disabled'}))
            content2 = ((data2['choices'][0].get('message') or {})
                        .get('content') or '').strip()
            if content2:
                return {'text': content2, 'rounds': rounds, 'tool_calls': tool_calls_n}
        raise RuntimeError('模型没有返回正文: ' + json.dumps(data)[:200])
    raise RuntimeError(f'工具调用轮次达到上限（{MAX_TOOL_ROUNDS}）仍未收敛出结论，'
                       '可减少数据量或更换模型后重试')
