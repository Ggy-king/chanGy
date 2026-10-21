"""模型接入层（Agent 唯一的联网出口）。当前：智谱 GLM-4.7-Flash（免费旗舰，国内直连）。
换供应商（DeepSeek / 本地 Ollama）只改这个文件，上层模块不动。"""
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
