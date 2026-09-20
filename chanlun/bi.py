# -*- coding: utf-8 -*-
"""
缠论 笔（bi）构建 —— 回归缠论原版（老笔口径）
规则：
1. 顶分型、底分型必须严格交替；连续出现同性质分型时，顶取更高、底取更低，前面的作废。
2. 相邻顶底分型，经过 K 线包含处理后，分型中间K线的 idx 差 >= MIN_BI_GAP（老笔：中间至少
   留出独立K线，含端点 >= 5 根合并K线），间隔不够则该反向分型作废、原端点保留。
3. 向上笔终点(顶)必须高于起点(底)，向下笔终点(底)必须低于起点(顶)。
4. 一个端点在被反向分型确认成笔前，若出现同性质更极端分型，则端点上移/下移，并同步回改
   上一笔的终点，保证折线端点首尾相接、不断点、不出现连续同向笔。
5. 成笔还要有真实推动：起点到终点的合并K线逐次创新高(向上)/新低(向下)至少 MIN_BREAKS 次，
   否则只是横盘浅回调、不成笔（间隔够但推动不够的假笔在此被滤掉）。
注意：已成交替笔的历史端点不回退（后视视角，回测只画已确认的正确笔）。
"""

# 老笔：两分型中间K线 idx 差 >= 4（含端点共 >=5 根合并K线）；新笔可放宽到 3
MIN_BI_GAP = 4
# 成笔的推进台阶：从起点分型中间K线到终点分型中间K线，合并K线逐次创新高(up)/新低(down)
# 至少 3 次。用于滤掉"间隔够但只横盘浅回调、没有真实推动"的假笔（如顶后仅2个台阶的小回撤）。
MIN_BREAKS = 3


def _breaks_count(merged, a, b, direction):
    """从分型中间K线 a 到分型中间K线 b，合并K线逐次创新高/新低的台阶数（含 b）。"""
    cnt = 0
    if direction == 'down':
        ref = float(merged['low'].iloc[a])
        for k in range(a + 1, b + 1):
            v = float(merged['low'].iloc[k])
            if v < ref:
                cnt += 1
                ref = v
    else:
        ref = float(merged['high'].iloc[a])
        for k in range(a + 1, b + 1):
            v = float(merged['high'].iloc[k])
            if v > ref:
                cnt += 1
                ref = v
    return cnt


def build_bi(top_fx, bottom_fx, merged):
    allfx = [dict(f, t='top') for f in top_fx] + \
            [dict(f, t='bottom') for f in bottom_fx]
    allfx.sort(key=lambda x: x['idx'])

    # 第一步：严格顶底交替，连续同性质分型取最极端
    seq = []
    for fx in allfx:
        if seq and seq[-1]['t'] == fx['t']:
            if fx['t'] == 'top' and fx['high'] > seq[-1]['high']:
                seq[-1] = fx
            elif fx['t'] == 'bottom' and fx['low'] < seq[-1]['low']:
                seq[-1] = fx
        else:
            seq.append(fx)

    if len(seq) < 2:
        return []

    # 第二步：状态机成笔
    segs = []          # [{'start': fx, 'end': fx}, ...]
    anchor = seq[0]    # 当前待定端点（上一笔终点 / 下一笔起点候选）
    for j in range(1, len(seq)):
        fx = seq[j]
        if fx['t'] == anchor['t']:
            # 同性质分型：更极端则移动端点，并回改上一笔终点
            more_extreme = (fx['t'] == 'top' and fx['high'] > anchor['high']) or \
                           (fx['t'] == 'bottom' and fx['low'] < anchor['low'])
            if more_extreme:
                anchor = fx
                if segs:
                    segs[-1]['end'] = fx
            continue

        # 反性质分型：检查间隔、价格方向、推进台阶
        gap = fx['idx'] - anchor['idx']
        direction = 'up' if anchor['t'] == 'bottom' else 'down'
        if direction == 'up':
            valid = fx['high'] > anchor['low']
        else:
            valid = fx['low'] < anchor['high']
        breaks = _breaks_count(merged, anchor['idx'], fx['idx'], direction)
        if gap >= MIN_BI_GAP and valid and breaks >= MIN_BREAKS:
            segs.append({'start': anchor, 'end': fx})
            anchor = fx
        # 间隔/方向/推进不够：该分型作废，anchor 保持，继续等待

    rows = []
    for s in segs:
        a, b = s['start'], s['end']
        direction = 'up' if a['t'] == 'bottom' else 'down'
        start_price = a['low'] if a['t'] == 'bottom' else a['high']
        end_price = b['high'] if b['t'] == 'top' else b['low']
        rows.append({
            'start_idx': int(a['idx']),
            'end_idx': int(b['idx']),
            'direction': direction,
            'start_price': round(float(start_price), 2),
            'end_price': round(float(end_price), 2),
            'start_datetime': a['datetime'],
            'end_datetime': b['datetime'],
        })
    return rows
