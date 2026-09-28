# -*- coding: utf-8 -*-
"""
缠论 笔（bi）构建 —— 严格移植 chan.py

含 chan.py 原版 update_peak（次高点处理，bi_allow_sub_peak=False 时启用）：
- 向上笔 A(底)→B(顶) 成立后，若 B 未突破前高（次高点）且后续底 C 跌破 A.low
  （且 B→C 不成笔、C 为区间最极端点），则取消 A→B，前一笔向下笔终点延伸到 C。
- 向下笔对称处理。
"""

MIN_BI_SPAN = 4


def _fx_valid_strict(merged, a, b):
    n = len(merged)

    def hi(i):
        return float(merged['high'].iloc[i]) if 0 <= i < n else float('inf')

    def lo(i):
        return float(merged['low'].iloc[i]) if 0 <= i < n else float('-inf')

    def bnext_hi(i):
        return float(merged['init_high'].iloc[i]) if 0 <= i < n else float('inf')

    def bnext_lo(i):
        return float(merged['init_low'].iloc[i]) if 0 <= i < n else float('-inf')

    if a['t'] == 'top':
        item2_high = max(hi(b['idx'] - 1), float(b['high']), bnext_hi(b['idx'] + 1))
        self_low = min(lo(a['idx'] - 1), float(a['low']), lo(a['idx'] + 1))
        return float(a['high']) > item2_high and float(b['low']) < self_low
    else:
        item2_low = min(lo(b['idx'] - 1), float(b['low']), bnext_lo(b['idx'] + 1))
        cur_high = max(hi(a['idx'] - 1), float(a['high']), hi(a['idx'] + 1))
        return float(a['low']) < item2_low and float(b['high']) > cur_high


def _end_is_peak(merged, last_end, cur_end):
    if last_end['t'] == 'bottom':
        cmp_thred = float(cur_end['high'])
        for k in range(last_end['idx'] + 1, cur_end['idx']):
            if float(merged['high'].iloc[k]) > cmp_thred:
                return False
    else:
        cmp_thred = float(cur_end['low'])
        for k in range(last_end['idx'] + 1, cur_end['idx']):
            if float(merged['low'].iloc[k]) < cmp_thred:
                return False
    return True


def _can_make_bi(merged, last_end, cur):
    if cur['idx'] - last_end['idx'] < MIN_BI_SPAN:
        return False
    if not _fx_valid_strict(merged, last_end, cur):
        return False
    if not _end_is_peak(merged, last_end, cur):
        return False
    return True


def build_bi(top_fx, bottom_fx, merged, bi_allow_sub_peak=True):
    """
    bi_allow_sub_peak: True=允许次高点成笔（chan.py 原版默认）；
                       False=启用 chan.py 原版 update_peak（次高点不成笔，回退延伸）
    """
    allfx = [dict(f, t='top') for f in top_fx] + \
            [dict(f, t='bottom') for f in bottom_fx]
    allfx.sort(key=lambda x: x['idx'])

    bi_list = []
    last_end = None
    free_lst = []

    for klc in allfx:
        if len(bi_list) == 0:
            made = False
            for exist in free_lst:
                if exist['t'] == klc['t']:
                    continue
                if _can_make_bi(merged, exist, klc):
                    bi_list.append({'start': exist, 'end': klc})
                    last_end = klc
                    made = True
                    break
            if not made:
                free_lst.append(klc)
                last_end = klc
            continue

        if klc['t'] == last_end['t']:
            # 同性质分型：更极端则更新最后一笔终点
            last_bi = bi_list[-1]
            if last_bi['start']['t'] == 'bottom':
                if float(klc['high']) >= float(last_bi['end']['high']):
                    last_bi['end'] = klc
                    last_end = klc
            else:
                if float(klc['low']) <= float(last_bi['end']['low']):
                    last_bi['end'] = klc
                    last_end = klc
        else:
            # 异性质分型
            if _can_make_bi(merged, last_end, klc):
                bi_list.append({'start': last_end, 'end': klc})
                last_end = klc
            else:
                # === chan.py 原版 update_peak（次高点处理，bi_allow_sub_peak=False 时启用）===
                # 对应原版 BiList.can_update_peak + update_peak + try_update_end
                if not bi_allow_sub_peak and len(bi_list) >= 2:
                    curr_bi = bi_list[-1]
                    prev_bi = bi_list[-2]
                    ok = False
                    if curr_bi['start']['t'] == 'bottom':
                        # 最后一笔为向上笔 A→B（A=curr_bi['start']，B=curr_bi['end']），klc 为新底
                        a_low = float(curr_bi['start']['low'])
                        b_high = float(curr_bi['end']['high'])
                        prev_top_high = float(prev_bi['start']['high'])
                        # 条件1: klc.low <= A.low（跌破向上笔起点）
                        # 条件2: B.high <= 前一笔起点的高（B 是次高点，未真突破）
                        # 条件3: klc 是从前高到 klc 区间内最低点（end_is_peak）
                        if (float(klc['low']) <= a_low and b_high <= prev_top_high
                                and _end_is_peak(merged, prev_bi['start'], klc)):
                            ok = True
                    else:
                        # 最后一笔为向下笔 A→B，klc 为新顶（对称）
                        a_high = float(curr_bi['start']['high'])
                        b_low = float(curr_bi['end']['low'])
                        prev_bottom_low = float(prev_bi['start']['low'])
                        if (float(klc['high']) >= a_high and b_low >= prev_bottom_low
                                and _end_is_peak(merged, prev_bi['start'], klc)):
                            ok = True
                    if ok:
                        # 删除次高点那一笔，前一笔终点延伸到 klc
                        bi_list.pop()
                        prev_bi['end'] = klc
                        last_end = klc

    rows = []
    for s in bi_list:
        a, b = s['start'], s['end']
        d = 'up' if a['t'] == 'bottom' else 'down'
        start_price = a['low'] if a['t'] == 'bottom' else a['high']
        end_price = b['high'] if b['t'] == 'top' else b['low']
        rows.append({
            'start_idx': int(a['idx']),
            'end_idx': int(b['idx']),
            'direction': d,
            'start_price': round(float(start_price), 2),
            'end_price': round(float(end_price), 2),
            'start_datetime': a['datetime'],
            'end_datetime': b['datetime'],
        })
    return rows
