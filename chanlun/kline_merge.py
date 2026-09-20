# -*- coding: utf-8 -*-
"""
缠论 K线包含关系处理（标准实现）
- 合并后的K线继续和下一根比较（不是和前一根原始K线）
- 向上方向包含：取max(high), max(low)
- 向下方向包含：取min(high), min(low)
- 记录high_idx/low_idx：真正高低点在哪根原始K线
"""
import pandas as pd


def merge_klines(df):
    merged = []
    
    for i in range(len(df)):
        h = float(df['high'].iloc[i])
        l = float(df['low'].iloc[i])
        
        if not merged:
            merged.append({
                'high': h, 'low': l,
                'start': i, 'end': i,
                'high_idx': i, 'low_idx': i,
            })
            continue
        
        last = merged[-1]
        last_h, last_l = last['high'], last['low']
        
        # 判断包含关系
        has_contain = (h <= last_h and l >= last_l) or (h >= last_h and l <= last_l)
        
        if has_contain:
            # 确定方向：往前找最近两根没有包含关系的K线
            direction = None
            for k in range(len(merged) - 1, 0, -1):
                prev = merged[k - 1]
                cur = merged[k]
                if cur['high'] > prev['high'] and cur['low'] > prev['low']:
                    direction = 'up'
                    break
                elif cur['high'] < prev['high'] and cur['low'] < prev['low']:
                    direction = 'down'
                    break
            
            if direction == 'up':
                new_h = max(last_h, h)
                new_l = max(last_l, l)
                new_high_idx = i if h > last_h else last['high_idx']
                new_low_idx = i if l > last_l else last['low_idx']
            elif direction == 'down':
                new_h = min(last_h, h)
                new_l = min(last_l, l)
                new_high_idx = i if h < last_h else last['high_idx']
                new_low_idx = i if l < last_l else last['low_idx']
            else:
                # 方向仍未确定（最开始几根连续包含），取外框
                new_h = max(last_h, h)
                new_l = min(last_l, l)
                new_high_idx = i if h >= last_h else last['high_idx']
                new_low_idx = i if l <= last_l else last['low_idx']
            
            last['high'] = new_h
            last['low'] = new_l
            last['end'] = i
            last['high_idx'] = new_high_idx
            last['low_idx'] = new_low_idx
        else:
            # 无包含关系，新增K线，方向自动确定
            merged.append({
                'high': h, 'low': l,
                'start': i, 'end': i,
                'high_idx': i, 'low_idx': i,
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
            'datetime': df['datetime'].iloc[m['start']],
        })
    return pd.DataFrame(rows)
