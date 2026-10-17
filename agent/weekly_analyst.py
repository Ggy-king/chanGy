"""第一个 Agent：周复盘分析师（Phase 1，设计文档 §5）。
输入脱敏后的周数据包 → GLM 生成纪律教练分析 → 返回文本。只读，不写任何交易/审计数据；
存档由 server 落到 reviews.json 的 weekly[week].agent。"""
import json
import time

from . import llm, prompts


def run(pack):
    """分析一个周数据包，成功返回 {text, model, created}；两次失败抛 RuntimeError。"""
    user = '以下是本周复盘数据包（JSON，时间已脱敏到小时）：\n' + \
        json.dumps(pack, ensure_ascii=False, indent=1)
    last_err = None
    for attempt in range(2):   # 失败重试 1 次（设计文档 §4 定稿）
        try:
            text = llm.chat(prompts.WEEKLY_SYSTEM, user)
            return {'text': text, 'model': llm.get_model(),
                    'created': time.strftime('%Y-%m-%d %H:%M')}
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise RuntimeError(str(last_err))
