# -*- coding: utf-8 -*-
"""
缠论 笔（bi）构建 —— 严格移植 chan.py（Bi/BiList.py + KLine/KLine.py check_fx_valid）

六个开关与 chan.py CBiConfig 一一对应，默认值 = 原版默认：
- bi_algo:          'normal' 常规（要求跨度达标） / 'fx' 有分型就成笔（跳过跨度检查）
- is_strict:        True 严格笔（老笔，合并K线跨度>=4） / False 宽松笔（新笔，跨度>=3 且两分型间原始K线>=3）
- bi_fx_check:      'loss'/'half'/'strict'/'totally' 两分型价格重叠检查的严格度
- gap_as_kl:        True 跳空缺口按一根K线计入跨度（chan.py get_klc_span）
- bi_end_is_peak:   True 笔端点必须是区间最极端点
- bi_allow_sub_peak: True=允许次高点成笔（原版默认）；False=启用原版 update_peak（次高点回退延伸）
"""

MIN_BI_SPAN = 4  # 严格笔最小跨度（chan.py satisfy_bi_span: bi_span >= 4）


def _hi(merged, i):
    n = len(merged)
    return float(merged['high'].iloc[i]) if 0 <= i < n else float('inf')


def _lo(merged, i):
    n = len(merged)
    return float(merged['low'].iloc[i]) if 0 <= i < n else float('-inf')


def _bnext_hi(merged, i):
    n = len(merged)
    return float(merged['init_high'].iloc[i]) if 0 <= i < n else float('inf')


def _bnext_lo(merged, i):
    n = len(merged)
    return float(merged['init_low'].iloc[i]) if 0 <= i < n else float('-inf')


def _fx_valid(merged, a, b, mode='strict'):
    """两分型价格重叠检查。逐行移植 chan.py CKLine.check_fx_valid 四种模式。

    a=前分型(last_end)，b=后分型。
    b 的 next 一侧用"创建瞬间"值（init_high/init_low），与原版增量算法时序一致：
    新分型在它成为倒数第二根合并K线时确认，此时它的 next 尚未走完。
    """
    if a['t'] == 'top':
        # a 是顶分型，b 是底分型（向下笔）
        if mode == 'loss':       # 只检查分型本身那一根
            item2_high = float(b['high'])
            self_low = float(a['low'])
        elif mode == 'half':     # 各自向对方扩半边
            item2_high = max(_hi(merged, b['idx'] - 1), float(b['high']))
            self_low = min(float(a['low']), _lo(merged, a['idx'] + 1))
        else:                    # strict / totally：双方前后各扩一根
            item2_high = max(_hi(merged, b['idx'] - 1), float(b['high']),
                             _bnext_hi(merged, b['idx'] + 1))
            self_low = min(_lo(merged, a['idx'] - 1), float(a['low']),
                           _lo(merged, a['idx'] + 1))
        if mode == 'totally':    # 两个分型区间必须完全不重叠
            return float(a['low']) > item2_high
        return float(a['high']) > item2_high and float(b['low']) < self_low
    else:
        # a 是底分型，b 是顶分型（向上笔）
        if mode == 'loss':
            item2_low = float(b['low'])
            cur_high = float(a['high'])
        elif mode == 'half':
            item2_low = min(_lo(merged, b['idx'] - 1), float(b['low']))
            cur_high = max(float(a['high']), _hi(merged, a['idx'] + 1))
        else:                    # strict / totally
            item2_low = min(_lo(merged, b['idx'] - 1), float(b['low']),
                            _bnext_lo(merged, b['idx'] + 1))
            cur_high = max(_hi(merged, a['idx'] - 1), float(a['high']),
                          _hi(merged, a['idx'] + 1))
        if mode == 'totally':
            return float(a['high']) < item2_low
        return float(a['low']) < item2_low and float(b['high']) > cur_high


def _has_gap_with_next(merged, i):
    """merged[i] 与 merged[i+1] 的原始K线之间是否有跳空缺口。
    对应 chan.py has_gap_with_next + has_overlap(equal=True)：相等算重叠（无缺口）。"""
    return (float(merged['raw_high'].iloc[i]) < float(merged['raw_low'].iloc[i + 1]) or
            float(merged['raw_high'].iloc[i + 1]) < float(merged['raw_low'].iloc[i]))


def _klc_span(merged, a, b, gap_as_kl):
    """对应 chan.py get_klc_span：合并K线索引跨度；gap_as_kl 时跳空缺口按一根K线计。"""
    span = b['idx'] - a['idx']
    if not gap_as_kl:
        return span
    if span >= 4:  # 原版加速：跨度已达标，无需精确
        return span
    i = a['idx']
    while i < b['idx']:
        if _has_gap_with_next(merged, i):
            span += 1
        i += 1
    return span


def _satisfy_span(merged, last_end, cur, bi_algo, is_strict, gap_as_kl):
    """对应 chan.py satisfy_bi_span。bi_algo='fx' 时跳过跨度检查。"""
    if bi_algo == 'fx':
        return True
    span = _klc_span(merged, last_end, cur, gap_as_kl)
    if is_strict:
        return span >= MIN_BI_SPAN
    # 宽松笔（新笔）：跨度>=3 且两分型之间原始K线数>=3（原版 uint_kl_cnt）
    uint_kl_cnt = 0
    for i in range(last_end['idx'] + 1, cur['idx']):
        uint_kl_cnt += int(merged['end_idx'].iloc[i] - merged['start_idx'].iloc[i]) + 1
    return span >= 3 and uint_kl_cnt >= 3


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


def _can_make_bi(merged, last_end, cur, conf):
    if not _satisfy_span(merged, last_end, cur, conf['bi_algo'],
                         conf['is_strict'], conf['gap_as_kl']):
        return False
    if not _fx_valid(merged, last_end, cur, conf['bi_fx_check']):
        return False
    if conf['bi_end_is_peak'] and not _end_is_peak(merged, last_end, cur):
        return False
    return True


def build_bi(top_fx, bottom_fx, merged, bi_allow_sub_peak=True, bi_algo='normal',
             is_strict=True, bi_fx_check='strict', bi_end_is_peak=True, gap_as_kl=False):
    """
    六个开关见模块 docstring，默认值 = chan.py 原版默认。
    bi_allow_sub_peak: True=允许次高点成笔（原版默认）；
                       False=启用 chan.py 原版 update_peak（次高点不成笔，回退延伸）
    """
    conf = {'bi_algo': bi_algo, 'is_strict': is_strict, 'bi_fx_check': bi_fx_check,
            'bi_end_is_peak': bi_end_is_peak, 'gap_as_kl': gap_as_kl,
            'bi_allow_sub_peak': bi_allow_sub_peak}

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
                if _can_make_bi(merged, exist, klc, conf):
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
            if _can_make_bi(merged, last_end, klc, conf):
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
