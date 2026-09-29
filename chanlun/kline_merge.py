# -*- coding: utf-8 -*-
"""
缠论 K线包含关系处理 —— 严格照抄 chan.py（Vespa314/chan.py）
Combiner/KLine_Combiner.py 的 test_combine / try_add 增量逻辑。

- 增量状态机：维护"当前方向"dir（新K线相对上一根独立K线的 UP/DOWN）。
- 包含判定（相等也算包含，独立K线要求 high、low 严格同向）：
    self.high>=item.high 且 self.low<=item.low  → 包含
    self.high<=item.high 且 self.low>=item.low  → 被包含（同样合并）
    self.high>item.high 且 self.low>item.low    → 向下独立K线 DOWN
    self.high<item.high 且 self.low<item.low    → 向上独立K线 UP
- 向上方向包含：high=max、low=max（高高）；向下方向包含：high=min、low=min（低低）。
- 一字K线特殊处理与 chan.py 完全一致。
- 第一根合并K线方向默认 UP（与 chan.py CKLine 默认 _dir=UP 一致）。
"""
import pandas as pd

UP = 'up'
DOWN = 'down'


def _test_combine(sh, sl, ih, il):
    """对应 chan.py CKLine_Combiner.test_combine，返回 'combine'/'up'/'down'。"""
    if sh >= ih and sl <= il:
        return 'combine'
    if sh <= ih and sl >= il:
        return 'combine'
    if sh > ih and sl > il:
        return DOWN
    if sh < ih and sl < il:
        return UP
    # 浮点异常等极端情况，按包含处理兜底
    return 'combine'


def merge_klines(df):
    merged = []

    for i in range(len(df)):
        h = float(df['high'].iloc[i])
        l = float(df['low'].iloc[i])

        if not merged:
            merged.append({
                'high': h, 'low': l, 'dir': UP,
                'start': i, 'end': i, 'high_idx': i, 'low_idx': i,
            })
            continue

        last = merged[-1]
        rel = _test_combine(last['high'], last['low'], h, l)

        if rel == 'combine':
            if last['dir'] == UP:
                # 一字K线且一字价正好等于当前最高价：不抬高 low（chan.py 原逻辑）
                if not (h == l and h == last['high']):
                    nh, nl = max(last['high'], h), max(last['low'], l)
                    if h > last['high']:
                        last['high_idx'] = i
                    if l > last['low']:
                        last['low_idx'] = i
                    last['high'], last['low'] = nh, nl
            else:  # DOWN
                if not (h == l and l == last['low']):
                    nh, nl = min(last['high'], h), min(last['low'], l)
                    if h < last['high']:
                        last['high_idx'] = i
                    if l < last['low']:
                        last['low_idx'] = i
                    last['high'], last['low'] = nh, nl
            last['end'] = i
        else:
            # 独立K线，方向即本次 test_combine 的结果
            merged.append({
                'high': h, 'low': l, 'dir': rel,
                'start': i, 'end': i, 'high_idx': i, 'low_idx': i,
            })

    rows = []
    for m in merged:
        rows.append({
            'start_idx': m['start'],
            'end_idx': m['end'],
            'high_idx': m['high_idx'],
            'low_idx': m['low_idx'],
            'high': m['high'],
            'low': m['low'],
            # 该合并K线"创建瞬间"（第一根原始K线）的高低点。
            # chan.py 增量算法中，新分型在它成为"倒数第二根合并K线"时确认，
            # 此时它的 next（最后一根）尚未走完，fx_valid 用的是 next 创建时的值。
            'init_high': float(df['high'].iloc[m['start']]),
            'init_low': float(df['low'].iloc[m['start']]),
            # 组内原始K线的真实高低点（缺口检测用，对应原版 get_klu_max_high/min_low）
            'raw_high': float(df['high'].iloc[m['start']:m['end'] + 1].max()),
            'raw_low': float(df['low'].iloc[m['start']:m['end'] + 1].min()),
            'datetime': df['datetime'].iloc[m['start']],
        })
    return pd.DataFrame(rows)
