# -*- coding: utf-8 -*-
"""
缠论 笔（bi）构建 —— 严格移植 chan.py（Vespa314/chan.py）的 CBiList 算法

配置与 chan.py 的 CChanConfig 默认值完全一致（经 CChan 正常调用时的实际配置）：
- is_strict=True      严格笔：相邻顶底分型的【合并K线 idx 差】>= 4
- bi_fx_check="strict" 分型有效性 STRICT 检查（两个分型各自3根K线的区域必须分离）
- gap_as_kl=False     span 直接用合并K线 idx 差，不把跳空缺口折算成K线
- bi_end_is_peak=True 端点必须是区间极值（两分型之间不能有K线越过端点）
- bi_allow_sub_peak=True  允许次高点成笔（此时 update_peak 回退不启用）

增量逻辑（批处理等价实现）：
1. 同性质分型（连续两个顶/两个底）：新分型更极端则更新最后一笔的终点（笔延伸）。
2. 异性质分型：满足 can_make_bi（span + fx_valid + end_is_peak）则成新笔，否则忽略。
3. 第一笔成立前用 free_lst 缓存分型。

所有判断均基于 K 线包含合并完成后的合并 K 线。
"""

# 严格笔：两分型中间K线 idx 差 >= 4（chan.py satisfy_bi_span，is_strict=True）
MIN_BI_SPAN = 4


def _fx_valid_strict(merged, a, b):
    """
    对应 chan.py KLine.check_fx_valid（FX_CHECK_METHOD.STRICT，非虚笔）。
    a = 已确认端点分型(last_end)，b = 当前异性质分型。
    分型 dict 含 t('top'/'bottom')、idx、high、low。
    与 HALF 的区别：各取分型【三根】合并K线（含 pre/next）的极值做比较，更严格。
    """
    n = len(merged)

    def hi(i):
        return float(merged['high'].iloc[i]) if 0 <= i < n else float('inf')

    def lo(i):
        return float(merged['low'].iloc[i]) if 0 <= i < n else float('-inf')

    # 当前分型 b 的 next（idx+1）在 b 确认时尚未走完（是当时最后一根合并K线），
    # 必须用它"创建瞬间"的 init 高低点，与 chan.py 增量时序一致。
    def bnext_hi(i):
        return float(merged['init_high'].iloc[i]) if 0 <= i < n else float('inf')

    def bnext_lo(i):
        return float(merged['init_low'].iloc[i]) if 0 <= i < n else float('-inf')

    if a['t'] == 'top':  # 顶 → 底
        # item2_high = max(b.pre.high, b.high, b.next.high)，b.next 用创建瞬间值
        item2_high = max(hi(b['idx'] - 1), float(b['high']), bnext_hi(b['idx'] + 1))
        # self_low = min(a.pre.low, a.low, a.next.low)（a 早已定型，用合并值）
        self_low = min(lo(a['idx'] - 1), float(a['low']), lo(a['idx'] + 1))
        return float(a['high']) > item2_high and float(b['low']) < self_low
    else:  # 底 → 顶
        # item2_low = min(b.pre.low, b.low, b.next.low)，b.next 用创建瞬间值
        item2_low = min(lo(b['idx'] - 1), float(b['low']), bnext_lo(b['idx'] + 1))
        # cur_high = max(a.pre.high, a.high, a.next.high)（a 早已定型，用合并值）
        cur_high = max(hi(a['idx'] - 1), float(a['high']), hi(a['idx'] + 1))
        return float(a['low']) < item2_low and float(b['high']) > cur_high


def _end_is_peak(merged, last_end, cur_end):
    """
    对应 chan.py BiList.end_is_peak。
    底→顶：last_end 之后到 cur_end 之间，不能有任何合并K线 high 超过 cur_end.high；
    顶→底：不能有任何合并K线 low 跌破 cur_end.low。
    """
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
    """对应 chan.py CBiList.can_make_bi（bi_algo='normal'）。"""
    # 1) span 检查（严格笔）
    if cur['idx'] - last_end['idx'] < MIN_BI_SPAN:
        return False
    # 2) 分型有效性 STRICT 检查
    if not _fx_valid_strict(merged, last_end, cur):
        return False
    # 3) 端点极值检查
    if not _end_is_peak(merged, last_end, cur):
        return False
    return True


def build_bi(top_fx, bottom_fx, merged):
    # 合并所有分型，按 idx 排序（不预先做交替压缩，同性质分型交给状态机做端点延伸）
    allfx = [dict(f, t='top') for f in top_fx] + \
            [dict(f, t='bottom') for f in bottom_fx]
    allfx.sort(key=lambda x: x['idx'])

    bi_list = []    # 每笔 {'start': fx, 'end': fx}
    last_end = None  # 最后一笔的终点分型
    free_lst = []    # 第一笔成立前的缓存分型

    for klc in allfx:
        if len(bi_list) == 0:
            # —— 第一笔之前（对应 try_create_first_bi）——
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
            # —— 同性质分型：更极端则更新最后一笔终点（对应 try_update_end）——
            last_bi = bi_list[-1]
            if last_bi['start']['t'] == 'bottom':   # 向上笔，终点应为顶
                if float(klc['high']) >= float(last_bi['end']['high']):
                    last_bi['end'] = klc
                    last_end = klc
            else:                                    # 向下笔，终点应为底
                if float(klc['low']) <= float(last_bi['end']['low']):
                    last_bi['end'] = klc
                    last_end = klc
        else:
            # —— 异性质分型：能成笔则新增，否则忽略（allow_sub_peak=True，不做 update_peak）——
            if _can_make_bi(merged, last_end, klc):
                bi_list.append({'start': last_end, 'end': klc})
                last_end = klc

    # 转换为输出格式
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
