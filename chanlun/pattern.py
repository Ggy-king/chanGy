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
    """结构指纹（不含 symbol/period，调用方拼全键）：同一结构只报一次。"""
    return f"{pattern}|{_fmt(struct['p1'])}|{_fmt(struct['p2'])}|{_fmt(struct['p3'])}"


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
    """确认/触发后，价格是否已"向上/向下离开"确认分型区域（离开=该阶段结束，跌回来会重新显示）。
    bottom(底分型确认)：三根K线最高价为门槛，最新K线最高价 > 门槛 = 向上离开
    top(顶分型确认)：  三根K线最低价为门槛，最新K线最低价 < 门槛 = 向下离开
    分型贴右边缘（后面还没有新K线）= 未离开。"""
    n = len(merged)
    if fx_k + 1 >= n:            # 分型右边还没有新K线，谈不上离开
        return False
    if fx_side == 'bottom':
        level = max(float(merged['high'].iloc[fx_k]), float(merged['high'].iloc[fx_k + 1]))
        if fx_k - 1 >= 0:
            level = max(level, float(merged['high'].iloc[fx_k - 1]))
        return float(merged['high'].iloc[-1]) > level
    level = min(float(merged['low'].iloc[fx_k]), float(merged['low'].iloc[fx_k + 1]))
    if fx_k - 1 >= 0:
        level = min(level, float(merged['low'].iloc[fx_k - 1]))
    return float(merged['low'].iloc[-1]) < level


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
            if since_fire and state == 'idle':
                if b['end_price'] > last_fire[0]['p3']:
                    # A类确认：回调笔成立 且 未破坏背驰点(3)——进场关注期开始
                    f_struct, f_ev = last_fire[0], last_fire[1]
                    ev = _event('VREV_CONF_LONG', f_struct, b)
                    is_new = ev['fingerprint'] not in reported
                    if is_new:
                        reported.add(ev['fingerprint'])
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
                if is_new:
                    reported.add(ev['fingerprint'])
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
        else:
            return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
    if since_fire and last_fire is not None:
        f_struct, f_ev, _, _ = last_fire
        return {'state': 'cand_a', 'struct': f_struct, 'fired': events}
    return _idle()


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
            if since_fire and state == 'idle':
                if b['end_price'] < last_fire[0]['p3']:
                    # A类确认：反抽笔成立 且 未破坏背驰点(3')——进场关注期开始
                    f_struct, f_ev = last_fire[0], last_fire[1]
                    ev = _event('VREV_CONF_SHORT', f_struct, b)
                    is_new = ev['fingerprint'] not in reported
                    if is_new:
                        reported.add(ev['fingerprint'])
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
                if is_new:
                    reported.add(ev['fingerprint'])
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
        else:
            return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
    if since_fire and last_fire is not None:
        f_struct, f_ev, _, _ = last_fire
        return {'state': 'cand_a', 'struct': f_struct, 'fired': events}
    return _idle()


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
        fired = []
        if ev['fingerprint'] not in reported:
            reported.add(ev['fingerprint'])
            fired.append(ev)
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
        fired = []
        if ev['fingerprint'] not in reported:
            reported.add(ev['fingerprint'])
            fired.append(ev)
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
        fired = []
        if ev['fingerprint'] not in reported:
            reported.add(ev['fingerprint'])
            fired.append(ev)
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
        fired = []
        if ev['fingerprint'] not in reported:
            reported.add(ev['fingerprint'])
            fired.append(ev)
        # 触发后价格向下离开反抽分型区域 → 触发阶段结束（涨回来会重新显示）
        if _left_confirm_zone(merged, bi_list[n - 1]['end_idx'], 'top'):
            return _idle()
        return {'state': 'trig_b', 'struct': struct, 'fired': fired}
    return _idle()


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
    返回 {'long': {'state','struct','fired'}, 'short': {...}}，纯函数幂等。"""
    rep = reported_keys if reported_keys is not None else set()
    return {
        'long': _merge(_eval_a_long(bi_list, merged, rep), _eval_c_long(bi_list, merged, rep)),
        'short': _merge(_eval_a_short(bi_list, merged, rep), _eval_c_short(bi_list, merged, rep)),
    }
