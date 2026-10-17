"""模型接入层（Agent 唯一的联网出口）。当前：智谱 GLM-4-Flash（免费额度，国内直连）。
换供应商（DeepSeek / 本地 Ollama）只改这个文件，上层模块不动。"""
import requests

API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
MODEL = 'glm-4.7-flash'   # 2026-10 查证：智谱免费旗舰（30B 级）；4.5-Flash 已公告下线迁移至此
TIMEOUT = 60   # 深度分析生成长，放宽到 60s


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


def get_key():
    """从 secret.py 读 ZHIPU_API_KEY，未配置返回 None。"""
    try:
        import secret
        return getattr(secret, 'ZHIPU_API_KEY', '') or None
    except Exception:
        return None


def chat(system, user):
    """单轮对话，返回回复文本。无 key / 接口失败抛 RuntimeError（消息可直接展示给用户）。"""
    key = get_key()
    if not key:
        raise RuntimeError('未配置 ZHIPU_API_KEY：到智谱开放平台 bigmodel.cn 免费领取，'
                           '然后在 secret.py 加一行  ZHIPU_API_KEY = "你的key"')
    r = requests.post(API_URL, headers={'Authorization': 'Bearer ' + key},
                      json={'model': get_model(), 'temperature': 0.5, 'max_tokens': 2048,
                            'messages': [{'role': 'system', 'content': system},
                                         {'role': 'user', 'content': user}]},
                      timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'模型接口返回 {r.status_code}: {r.text[:200]}')
    try:
        return r.json()['choices'][0]['message']['content']
    except Exception:
        raise RuntimeError('模型返回格式异常: ' + r.text[:200])
