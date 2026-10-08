# -*- coding: utf-8 -*-
"""
背驰-V反 形态评估器（纯函数、幂等、无 IO）

形态定义见 docs/背驰反转形态预警系统设计.md §2/§3。多空双向独立评估。

设计原则（文档 §3.7）：
- 每次对"最新完整笔列表"全量重评，不做增量假设；
- 推送去重靠结构指纹（fingerprint），由调用方持久传入 reported_keys，
  本模块对 fingerprint 不在集合内的事件原地追加集合并放进 fired；
- state 完全由笔列表推导，重启后天然恢复。

状态枚举（与矩阵页 §9.3 的 cell.state 一一对应）：
  idle / watch_a / cand_a / conf_a / cand_b / trig_b
（B 类候选作废时返回 idle，由调用方决定是否短暂显示 gone_b。）
"""

PATTERN_RATIO = 2.0  # 形态C 幅度过滤常数（§3.5，用户要求写成常量便于调整）


# ---------------- 分型位置检查（R3 / R3'，§3.2 最保守背驰量化） ----------------

def _fx_below_level(merged, k, level):
    """R3(多)：3 处底分型的三根合并K线，high 全部严格低于 level（=p1）。

    merged 的 high = 组内全部原始K线的最高价，故本条件 ⟺
    分型覆盖范围内每根原始K线的最高点都 < p1（文档 §3.2 等价性说明）。
    贴边（k-1<0 或 k+1 越界）→ False，保守跳过。"""
    n = len(merged)
    if k - 1 < 0 or k + 1 >= n:
        return False
    return (float(merged['high'].iloc[k - 1]) < level
            and float(merged['high'].iloc[k]) < level
            and float(merged['high'].iloc[k + 1]) < level)


def _fx_above_level(merged, k, level):
    """R3'(空) 镜像：3' 处顶分型的三根合并K线，low 全部严格高于 level（=p1'）。"""
    n = len(merged)
    if k - 1 < 0 or k + 1 >= n:
        return False
    return (float(merged['low'].iloc[k - 1]) > level
            and float(merged['low'].iloc[k]) > level
            and float(merged['low'].iloc[k + 1]) > level)


# ---------------- 事件与指纹（§5.3） ----------------

def _fmt(p):
    return f'{float(p):.2f}'


def fingerprint(pattern, struct):
    """结构指纹（不含 symbol/period，调用方拼全键）：同一结构只报一次。
    只取 pattern|p1|p2（结构锚，固定不变）——进行中笔的端点 p3 是浮动的
    （随行情延伸每轮变化），若入指纹会导致同一信号反复推送。"""
    return f"{pattern}|{_fmt(struct['p1'])}|{_fmt(struct['p2'])}"


def _event(pattern, struct, trig_bi, bi_span=None, ratio=None):
    ev = {
        'pattern': pattern,
        'price': float(trig_bi['end_price']),          # 触发笔端点价
        'time': str(trig_bi['end_datetime']),          # 触发笔端点时间
        'p1': float(struct['p1']), 'p2': float(struct['p2']), 'p3': float(struct['p3']),
        'fingerprint': fingerprint(pattern, struct),
    }
    if bi_span is not None:
        ev['bi_span'] = bi_span
    if ratio is not None:
        ev['ratio'] = round(ratio, 2)
    return ev



def _left_confirm_zone(merged, fx_k, fx_side):
    """确认/触发后，价格是否已"离开"确认分型区域（离开=该阶段结束，跌回来会重新显示）。

    【用户定稿 2026-10-08 v2：3 根K线创新高制】单根K线越线不算（价格在门槛附近
    徘徊会造成确认状态反复横跳），必须分型之后出现**连续 3 根已完结K线依次创新高**
    （多头：high 逐根抬升；空头：low 逐根下移）才算真正离开——持续性上攻/下杀。
    离开后价格回到区间内（多头：最新完结K线 high < 门槛；空头镜像）→ 重新显示。
    门槛 = 分型 3 根K线极值的极值（区域边界），与 v1 一致。
    分型后不足 3 根完结K线 = 未离开。"""
    n = len(merged)
    if fx_k + 3 >= n:            # 分型后不足 3 根完结K线，谈不上离开
        return False
    highs = merged['high'].values
    lows = merged['low'].values
    if fx_side == 'bottom':
        level = max(float(highs[fx_k]), float(highs[fx_k + 1]))
        if fx_k - 1 >= 0:
            level = max(level, float(highs[fx_k - 1]))
        rose = any(highs[j] < highs[j + 1] < highs[j + 2]
                   for j in range(fx_k + 1, n - 2))
        if not rose:
            return False
        return not (float(highs[n - 1]) < level)     # 回到区间内 → 重新显示
    level = min(float(lows[fx_k]), float(lows[fx_k + 1]))
    if fx_k - 1 >= 0:
        level = min(level, float(lows[fx_k - 1]))
    fell = any(lows[j] > lows[j + 1] > lows[j + 2]
               for j in range(fx_k + 1, n - 2))
    if not fell:
        return False
    return not (float(lows[n - 1]) > level)          # 回到区间内 → 重新显示


def _idle():
    return {'state': 'idle', 'struct': None, 'fired': []}


def _mkstruct(bi_a, bi_b):
    """结构三点：p1/p2/p3 + 时间。bi_a 为第 1(5)→第 2(6) 笔，bi_b 为第 2(3)→第 3 笔。"""
    return {
        'p1': bi_a['start_price'], 'p2': bi_a['end_price'], 'p3': bi_b['end_price'],
        't1': str(bi_a['start_datetime']), 't2': str(bi_a['end_datetime']),
        't3': str(bi_b['end_datetime']),
        'anchor_bi': bi_b['end_idx'],
    }


# ---------------- 形态A：背驰反转（VREV，§3.1-3.4） ----------------
# 实现方式：对笔列表做正向状态机模拟（与文档 §3.7 时间演化 1:1 对应）：
#   - 每根向下笔完成时评估（§3.1）；已有结构时仅"新低(<p3)"才作废重建（§3.4，
#     更高低点的自有 pair 必然 R2 不成立，两种读法数学等价）；
#   - WATCH 中任一向上笔终点严格破 2 → 触发即结束（状态回 IDLE，§3.3）。

def _finite(*vals):
    """价格必须为有限数（数据源偶发 NaN K线会污染笔端点，直接排除）。"""
    import math
    return all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in vals)


def _eval_a_long(bi_list, merged, reported):
    """做多：1(低)-2(高)-3(低,破1且分型全在1下方)。
    三阶段（用户术语）：观察（结构成立）→ 候选（向上笔突破2，VREV_LONG）
    → 确认（突破后第一根向下回调笔成立，VREV_CONF_LONG = 二买进场关注）。
    候选后状态机回 IDLE（§3.3）；确认每结构只报一次；确认后若回调跌破旧 3，
    自然走出新结构重新观察（循环判断）。"""
    state, struct = 'idle', None
    since_fire, last_fire = False, None       # A候选: (struct, ev, is_new, fire_i)
    since_conf, last_conf = False, None       # A确认: (struct, ev, is_new)
    events = []                               # 本轮新产生的事件（调用方负责推送）
    for i, b in enumerate(bi_list):
        if b['direction'] == 'down':
            if state == 'watch' and b['end_price'] < struct['p3']:
                state, struct = 'idle', None      # 新低 → 旧结构作废，立即重建检查（§3.4）
            if since_conf and b['end_price'] < last_conf[0]['p3']:
                since_conf = False   # 确认后跌破背驰点 → 假确认作废（用户定稿：破背驰点=假确认，转新观察）
            if since_fire and state == 'idle':
                if b['end_price'] > last_fire[0]['p3']:
                    # A类确认：回调笔成立 且 未破坏背驰点(3)——进场关注期开始
                    f_struct, f_ev = last_fire[0], last_fire[1]
                    ev = _event('VREV_CONF_LONG', f_struct, b)
                    is_new = ev['fingerprint'] not in reported
                    reported.add(ev['fingerprint'])
                    ev['first'] = is_new
                    events.append(ev)
                    last_conf = (f_struct, ev, is_new, (b['end_idx'], 'bottom'))
                    since_fire, since_conf = False, True
                else:
                    # 回调跌破背驰点 → 假突破，候选+结构整体作废
                    # （由下方新结构评估接管：可能演化为新的 A类观察）
                    since_fire = False
            if state == 'idle' and i >= 1:
                p1 = bi_list[i - 1]['start_price']    # 1 = 向上笔起点(底)
                p2 = bi_list[i - 1]['end_price']      # 2 = 向上笔终点(顶)
                p3 = b['end_price']                   # 3
                if (p3 < p1 and _finite(p1, p2, p3)
                        and _fx_below_level(merged, b['end_idx'], p1)):
                    struct = _mkstruct(bi_list[i - 1], b)
                    struct['anchor_i'] = i
                    state = 'watch'
                    since_fire = since_conf = False
        else:                                          # 向上笔完成
            # 确认阶段的结束由 _left_confirm_zone 门控判定（价格向上离开=结束，跌回来=恢复显示）
            if state == 'watch' and b['end_price'] > struct['p2']:
                ev = _event('VREV_LONG', struct, b, bi_span=i - struct['anchor_i'])
                is_new = ev['fingerprint'] not in reported
                reported.add(ev['fingerprint'])    # 幂等：延续触发也记录
                ev['first'] = is_new               # 首次=True / 延续=False（调用方据此更新卡片）
                events.append(ev)
                last_fire = (struct, ev, is_new, i)
                state, struct = 'idle', None           # 候选成立，回 IDLE 等回调（§3.3）
                since_fire = True
    if state == 'watch':
        return {'state': 'watch_a', 'struct': struct, 'fired': events}
    if since_conf and last_conf is not None:
        c_struct, c_ev, _, ci = last_conf
        if _left_confirm_zone(merged, *ci):
            since_conf = False   # 价格已向上离开确认分型区域 → 确认阶段结束（跌回来会重新显示）
        elif float(merged.iloc[-1]['low']) < c_struct['p3']:
            since_conf = False   # 现价实时跌破背驰点 → 假确认，确认窗口关闭（回升则重开）
        else:
            return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
    if since_fire and last_fire is not None:
        f_struct, f_ev, _, _ = last_fire
        return {'state': 'cand_a', 'struct': f_struct, 'fired': events}
    # 状态回 idle 不等于"什么都没发生"——模拟中途触发过的事件（候选/确认）是已发生的
    # 事实，必须随 fired 返回（调用方按指纹去重，只推一次）；格子只显示当前状态，
    # 事件流要为实时变化"留底"（用户 2026-10-08：棉花确认被状态终结吞掉事故）
    return {'state': 'idle', 'struct': None, 'fired': events}


def _eval_a_short(bi_list, merged, reported):
    """做空镜像：1'(顶)-2'(低)-3'(新高,破1'且顶分型全在1'上方)。三阶段同 _eval_a_long。"""
    state, struct = 'idle', None
    since_fire, last_fire = False, None       # A候选: (struct, ev, is_new, fire_i)
    since_conf, last_conf = False, None       # A确认: (struct, ev, is_new)
    events = []
    for i, b in enumerate(bi_list):
        if b['direction'] == 'up':
            if state == 'watch' and b['end_price'] > struct['p3']:
                state, struct = 'idle', None          # 新高 → 旧结构作废，立即重建检查
            if since_conf and b['end_price'] > last_conf[0]['p3']:
                since_conf = False   # 确认后涨破背驰点 → 假确认作废（用户定稿：破背驰点=假确认，转新观察）
            if since_fire and state == 'idle':
                if b['end_price'] < last_fire[0]['p3']:
                    # A类确认：反抽笔成立 且 未破坏背驰点(3')——进场关注期开始
                    f_struct, f_ev = last_fire[0], last_fire[1]
                    ev = _event('VREV_CONF_SHORT', f_struct, b)
                    is_new = ev['fingerprint'] not in reported
                    reported.add(ev['fingerprint'])
                    ev['first'] = is_new
                    events.append(ev)
                    last_conf = (f_struct, ev, is_new, (b['end_idx'], 'top'))
                    since_fire, since_conf = False, True
                else:
                    since_fire = False   # 反抽突破背驰点 → 假突破，结构作废（新结构评估接管）
            if state == 'idle' and i >= 1:
                p1 = bi_list[i - 1]['start_price']    # 1' = 向下笔起点(顶)
                p2 = bi_list[i - 1]['end_price']      # 2' = 向下笔终点(低)
                p3 = b['end_price']                   # 3'
                if (p3 > p1 and _finite(p1, p2, p3)
                        and _fx_above_level(merged, b['end_idx'], p1)):
                    struct = _mkstruct(bi_list[i - 1], b)
                    struct['anchor_i'] = i
                    state = 'watch'
                    since_fire = since_conf = False
        else:                                          # 向下笔完成
            # 确认阶段的结束由 _left_confirm_zone 门控判定（价格向下离开=结束，涨回来=恢复显示）
            if state == 'watch' and b['end_price'] < struct['p2']:
                ev = _event('VREV_SHORT', struct, b, bi_span=i - struct['anchor_i'])
                is_new = ev['fingerprint'] not in reported
                reported.add(ev['fingerprint'])    # 幂等：延续触发也记录
                ev['first'] = is_new               # 首次=True / 延续=False（调用方据此更新卡片）
                events.append(ev)
                last_fire = (struct, ev, is_new, i)
                state, struct = 'idle', None
                since_fire = True
    if state == 'watch':
        return {'state': 'watch_a', 'struct': struct, 'fired': events}
    if since_conf and last_conf is not None:
        c_struct, c_ev, _, ci = last_conf
        if _left_confirm_zone(merged, *ci):
            since_conf = False   # 价格已向下离开确认分型区域 → 确认阶段结束（涨回来会重新显示）
        elif float(merged.iloc[-1]['high']) > c_struct['p3']:
            since_conf = False   # 现价实时涨破背驰点 → 假确认，确认窗口关闭（回落则重开）
        else:
            return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
    if since_fire and last_fire is not None:
        f_struct, f_ev, _, _ = last_fire
        return {'state': 'cand_a', 'struct': f_struct, 'fired': events}
    # 同 _eval_a_long：状态回 idle 不丢弃中途已触发的事件（事件流要"留底"）
    return {'state': 'idle', 'struct': None, 'fired': events}


# ---------------- 形态C：三买/三卖（3BUY/3SELL，§3.5） ----------------

def _eval_c_long(bi_list, merged, reported):
    """做多：5(高)-6(低)-7(高破5 且 (7-6)>2×(5-6)) → 3BUY_CAND；
    回调笔终点 > 5 → 3BUY_CONF；<= 5 → 作废（idle，由形态A逻辑接管重建检查）。"""
    n = len(bi_list)
    if n < 2:
        return _idle()
    if bi_list[-1]['direction'] == 'up':
        # 最后一笔是 6→7，候选待确认
        p5, p6, p7 = bi_list[n - 2]['start_price'], bi_list[n - 2]['end_price'], bi_list[n - 1]['end_price']
        if not (p7 > p5):
            return _idle()
        rng56 = p5 - p6
        rng67 = p7 - p6
        if not (rng67 > PATTERN_RATIO * rng56):      # C2 幅度过滤
            return _idle()
        struct = {
            'p1': p5, 'p2': p6, 'p3': p7,
            't1': str(bi_list[n - 2]['start_datetime']), 't2': str(bi_list[n - 2]['end_datetime']),
            't3': str(bi_list[n - 1]['end_datetime']), 'anchor_bi': bi_list[n - 1]['end_idx'],
        }
        ev = _event('3BUY_CAND', struct, bi_list[n - 1], ratio=rng67 / rng56 if rng56 else None)
        is_new = ev['fingerprint'] not in reported
        reported.add(ev['fingerprint'])
        ev['first'] = is_new
        fired = [ev]
        return {'state': 'cand_b', 'struct': struct, 'fired': fired}
    # 最后一笔是回调（7 之后的向下笔）
    p5, p6, p7 = (bi_list[n - 3]['start_price'], bi_list[n - 3]['end_price'],
                  bi_list[n - 2]['end_price'])
    if not (p7 > p5):
        return _idle()
    rng56 = p5 - p6
    rng67 = p7 - p6
    if not (rng67 > PATTERN_RATIO * rng56):
        return _idle()
    struct = {
        'p1': p5, 'p2': p6, 'p3': p7,
        't1': str(bi_list[n - 3]['start_datetime']), 't2': str(bi_list[n - 3]['end_datetime']),
        't3': str(bi_list[n - 2]['end_datetime']), 'anchor_bi': bi_list[n - 2]['end_idx'],
    }
    pb = bi_list[n - 1]['end_price']                 # 回调低点
    if pb > p5:                                      # 回调不回 5 → 触发
        ev = _event('3BUY_TRIG', struct, bi_list[n - 1])
        is_new = ev['fingerprint'] not in reported
        reported.add(ev['fingerprint'])
        ev['first'] = is_new
        fired = [ev]
        # 触发后价格向上离开回踩分型区域 → 触发阶段结束（跌回来会重新显示）
        if _left_confirm_zone(merged, bi_list[n - 1]['end_idx'], 'bottom'):
            return _idle()
        return {'state': 'trig_b', 'struct': struct, 'fired': fired}
    return _idle()                                   # 跌破 5 → 作废，按图1 逻辑演化


def _eval_c_short(bi_list, merged, reported):
    """做空镜像：5(低)-6(高)-7(低破5 且 (6-7)>2×(6-5)) → 3SELL_CAND；
    反抽笔终点 < 5 → 3SELL_CONF；>= 5 → 作废。"""
    n = len(bi_list)
    if n < 2:
        return _idle()
    if bi_list[-1]['direction'] == 'down':
        p5, p6, p7 = bi_list[n - 2]['start_price'], bi_list[n - 2]['end_price'], bi_list[n - 1]['end_price']
        if not (p7 < p5):
            return _idle()
        rng56 = p6 - p5
        rng67 = p6 - p7
        if not (rng67 > PATTERN_RATIO * rng56):
            return _idle()
        struct = {
            'p1': p5, 'p2': p6, 'p3': p7,
            't1': str(bi_list[n - 2]['start_datetime']), 't2': str(bi_list[n - 2]['end_datetime']),
            't3': str(bi_list[n - 1]['end_datetime']), 'anchor_bi': bi_list[n - 1]['end_idx'],
        }
        ev = _event('3SELL_CAND', struct, bi_list[n - 1], ratio=rng67 / rng56 if rng56 else None)
        is_new = ev['fingerprint'] not in reported
        reported.add(ev['fingerprint'])
        ev['first'] = is_new
        fired = [ev]
        return {'state': 'cand_b', 'struct': struct, 'fired': fired}
    p5, p6, p7 = (bi_list[n - 3]['start_price'], bi_list[n - 3]['end_price'],
                  bi_list[n - 2]['end_price'])
    if not (p7 < p5):
        return _idle()
    rng56 = p6 - p5
    rng67 = p6 - p7
    if not (rng67 > PATTERN_RATIO * rng56):
        return _idle()
    struct = {
        'p1': p5, 'p2': p6, 'p3': p7,
        't1': str(bi_list[n - 3]['start_datetime']), 't2': str(bi_list[n - 3]['end_datetime']),
        't3': str(bi_list[n - 2]['end_datetime']), 'anchor_bi': bi_list[n - 2]['end_idx'],
    }
    pb = bi_list[n - 1]['end_price']                 # 反抽高点
    if pb < p5:
        ev = _event('3SELL_TRIG', struct, bi_list[n - 1])
        is_new = ev['fingerprint'] not in reported
        reported.add(ev['fingerprint'])
        ev['first'] = is_new
        fired = [ev]
        # 触发后价格向下离开反抽分型区域 → 触发阶段结束（涨回来会重新显示）
        if _left_confirm_zone(merged, bi_list[n - 1]['end_idx'], 'top'):
            return _idle()
        return {'state': 'trig_b', 'struct': struct, 'fired': fired}
    return _idle()


# ---------------- 进行中笔（用户定稿 2026-10-08：图上笔画出来的瞬间就要参与评估） ----------------

def _effective_bis(bi_list, merged):
    """评估用笔列表 = 完成笔 + 进行中笔（用户定稿 2026-10-08：图上笔画出来的瞬间就要参与评估）。

    两种形态（与实时行情图上笔的画法一致）：
    ① 同向延伸：后续极值突破完成笔端点 → 最后一笔端点实时前移【替换】（图上延伸的笔
       就是同一根在变长，不能追加成连续同向的两根）；
    ② 反向新笔：从完成笔终点反向走到当前极值，跨度满足 MIN_BI_SPAN 即视为成笔
       （=用户说的"5根K线成笔的那一瞬间"，哪怕之后继续延伸 6、7、8 根）。
    都不满足 → 原列表原样返回。

    极值只取【已完结K线】——forming bar（未完结的原始K线）已在数据源头
    （engine.pattern_snapshot）剔除，这里不再排除最后一根合并K线（它可能同时
    包含完结与未完结的原始K线，整根排除会漏掉已完结部分的突破）。
    用户定稿："K线结束的时候，再去算这根K线的高点是否有突破关键价格"。
    K线走完的下一轮扫描（≤30s）自然计入，既不失时也不吃盘中毛刺。"""
    from chanlun.bi import MIN_BI_SPAN
    if not bi_list or len(merged) == 0:
        return bi_list
    last = bi_list[-1]
    si, sp = int(last['end_idx']), float(last['end_price'])
    seg = merged.iloc[si + 1:]
    if seg.empty:
        return bi_list
    base = si + 1
    dts = merged['datetime'] if 'datetime' in merged.columns else None

    def _dt(i):
        return str(dts.iloc[i]) if dts is not None else ''

    def _ext(end_i, end_p):
        b = dict(last)
        b.update({'end_idx': end_i, 'end_price': end_p, 'end_datetime': _dt(end_i)})
        return bi_list[:-1] + [b]

    def _new(direction, end_i, end_p):
        return bi_list + [{'start_idx': si, 'end_idx': end_i, 'direction': direction,
                           'start_price': sp, 'end_price': end_p,
                           'start_datetime': last['end_datetime'], 'end_datetime': _dt(end_i)}]

    if last['direction'] == 'up':
        hi_i = base + int(seg['high'].values.argmax())     # 全部用位置索引（夹具可能切片过，标签≠位置）
        if float(seg['high'].iloc[hi_i - base]) > sp:      # ① 向上延伸
            return _ext(hi_i, float(seg['high'].iloc[hi_i - base]))
        lo_i = base + int(seg['low'].values.argmin())
        if lo_i - si < MIN_BI_SPAN:                        # ② 反向成笔跨度门槛
            return bi_list
        return _new('down', lo_i, float(seg['low'].iloc[lo_i - base]))
    else:
        lo_i = base + int(seg['low'].values.argmin())
        if float(seg['low'].iloc[lo_i - base]) < sp:       # ① 向下延伸
            return _ext(lo_i, float(seg['low'].iloc[lo_i - base]))
        hi_i = base + int(seg['high'].values.argmax())
        if hi_i - si < MIN_BI_SPAN:
            return bi_list
        return _new('up', hi_i, float(seg['high'].iloc[hi_i - base]))


# ---------------- 总入口（§5.1） ----------------

def _merge(a, c):
    """A 类与 C 类同侧结果合并：fired 相加，状态取更高优先级。"""
    rank = {'conf_a': 5, 'trig_b': 4, 'cand_a': 3, 'cand_b': 2, 'watch_a': 1, 'idle': 0}
    st = a if rank[a['state']] >= rank[c['state']] else c
    return {'state': st['state'], 'struct': st['struct'], 'fired': a['fired'] + c['fired']}


def evaluate(bi_list, merged, reported_keys=None):
    """形态评估总入口。

    bi_list: build_bi 输出（时间升序）；merged: merge_klines 输出（R3 分型位置检查用）。
    reported_keys: 已报指纹集合（调用方按 (symbol,period) 持有并持久传入；
    本函数对首次触发的事件原地写入集合，并放入返回值 fired）。
    评估输入 = 完成笔 + 进行中笔（图上画出来的就要算，突破/成笔瞬间触发）。
    返回 {'long': {'state','struct','fired'}, 'short': {...}}，纯函数幂等。"""
    rep = reported_keys if reported_keys is not None else set()
    bis = _effective_bis(bi_list, merged)
    return {
        'long': _merge(_eval_a_long(bis, merged, rep), _eval_c_long(bis, merged, rep)),
        'short': _merge(_eval_a_short(bis, merged, rep), _eval_c_short(bis, merged, rep)),
    }
