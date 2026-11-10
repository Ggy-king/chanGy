"""模型接入层（Agent 唯一的联网出口）。当前：智谱 GLM-4.7-Flash（免费旗舰，国内直连）。
换供应商（DeepSeek / 本地 Ollama）只改这个文件，上层模块不动。"""
import json
import time
import requests

API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
MODEL = 'glm-4.7-flash'   # 2026-10 查证：智谱免费旗舰（30B 级）；4.5-Flash 已公告下线迁移至此
TIMEOUT = 150   # glm-4.7 深度思考模型跑大数据包很慢（实测 >60s），放宽到 150s


def get_key():
    """从 secret.py 读 ZHIPU_API_KEY，未配置返回 None。"""
    try:
        import secret
        return getattr(secret, 'ZHIPU_API_KEY', '') or None
    except Exception:
        return None


def get_model():
    """模型可被 secret.ZHIPU_MODEL 覆盖（想试别的免费模型不用改代码）。"""
    try:
        import secret
        return getattr(secret, 'ZHIPU_MODEL', '') or MODEL
    except Exception:
        return MODEL


def _post(body):
    """统一请求出口：无 key / 非 200 抛 RuntimeError（消息可直接展示给用户）。"""
    key = get_key()
    if not key:
        raise RuntimeError('未配置 ZHIPU_API_KEY：到智谱开放平台 bigmodel.cn 免费领取，'
                           '然后在 secret.py 加一行  ZHIPU_API_KEY = "你的key"')
    r = requests.post(API_URL, headers={'Authorization': 'Bearer ' + key},
                      json=body, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'模型接口返回 {r.status_code}: {r.text[:200]}')
    return r.json()


def _post(body):
    """统一请求出口：无 key 抛错；429/5xx 指数退避重试 2 次（免费模型高峰限流常态）；
    其余非 200 直接抛 RuntimeError（消息可直接展示给用户）。"""
    key = get_key()
    if not key:
        raise RuntimeError('未配置 ZHIPU_API_KEY：到智谱开放平台 bigmodel.cn 免费领取，'
                           '然后在 secret.py 加一行  ZHIPU_API_KEY = "你的key"')
    last = None
    for wait in (0, 8, 20, 45):
        if wait:
            time.sleep(wait)
        r = requests.post(API_URL, headers={'Authorization': 'Bearer ' + key},
                          json=body, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        last = f'模型接口返回 {r.status_code}: {r.text[:200]}'
        if r.status_code not in (429, 500, 502, 503, 504):
            raise RuntimeError(last)
    raise RuntimeError(last)


def chat(system, user):
    """单轮对话，返回回复文本。无 key / 接口失败抛 RuntimeError（消息可直接展示给用户）。

    glm-4.7 系列默认开深度思考（实测一句话回复也烧 ~941 个思考 token），
    思考+正文共享 max_tokens 预算——2048 会被思考烧光导致 content 返回空串
    （2026-10-21 周复盘空白事故）。故：预算提到 8192；正文仍为空时
    （finish_reason=length）关掉深度思考再试一次兜底。"""
    key = get_key()
    if not key:
        raise RuntimeError('未配置 ZHIPU_API_KEY：到智谱开放平台 bigmodel.cn 免费领取，'
                           '然后在 secret.py 加一行  ZHIPU_API_KEY = "你的key"')
    body = {'model': get_model(), 'temperature': 0.5, 'max_tokens': 8192,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}]}
    r = requests.post(API_URL, headers={'Authorization': 'Bearer ' + key},
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
        r2 = requests.post(API_URL, headers={'Authorization': 'Bearer ' + key},
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
                           '（可把 secret.py 的 ZHIPU_MODEL 换成别的模型试试）')
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
        body = {'model': get_model(), 'temperature': 0.4, 'max_tokens': 8192, 'messages': msgs}
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
            content2 = ((data2['choices'][0].get('message') or {}).get('content') or '').strip()
            if content2:
                return {'text': content2, 'rounds': rounds, 'tool_calls': tool_calls_n}
        raise RuntimeError('模型没有返回正文: ' + json.dumps(data)[:200])
    raise RuntimeError(f'工具调用轮次达到上限（{MAX_TOOL_ROUNDS}）仍未收敛出结论，'
                       '可减少数据量或更换模型后重试')
