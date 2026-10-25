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


def _bars_beyond(merged, k, level, side, raw=None):
    """bars 档：最低点邻域内完全在 level 外侧的K线数，允许间断
    （缩回区间里的K线自然不计数，后面仍在外侧的继续数——用户间断规则 2026-10-24）。
    raw 给定时按【原始K线】数（用户定稿：不考虑合并——合并会把矮K线和邻边高K线
    捆成一捆，导致"明明在低位待了好几根"数不出来，焦煤 10-22 实测 6 根数成 2 根）；
    窗口 = 最低点左右 ±A3_BARS_WIN 根合并K线所覆盖的原始K线范围。
    raw=None 时退化为合并K线口径（老测试/无原始数据的调用方）。"""
    lo_m = max(0, k - A3_BARS_WIN)
    hi_m = min(len(merged) - 1, k + A3_BARS_WIN)
    if raw is not None:
        r_lo = max(0, int(merged['start_idx'].iloc[lo_m]))
        r_hi = min(len(raw) - 1, int(merged['end_idx'].iloc[hi_m]))
        if side == 'long':
            return int((raw['high'].iloc[r_lo:r_hi + 1] < level).sum())
        return int((raw['low'].iloc[r_lo:r_hi + 1] > level).sum())
    if side == 'long':
        return sum(1 for j in range(lo_m, hi_m + 1) if float(merged['high'].iloc[j]) < level)
    return sum(1 for j in range(lo_m, hi_m + 1) if float(merged['low'].iloc[j]) > level)


def _a3_gate(merged, k, p1, p2, p3, side, raw=None):
    """A类观察结构成立与否的统一入口（背驰确认三档口径，2026-10-24）。
    多头：p1=底1 p2=顶2 p3=背驰低点(<p1)；空头镜像。返回 True=结构成立进观察。"""
    strict_ok = _fx_below_level(merged, k, p1) if side == 'long' else _fx_above_level(merged, k, p1)
    if strict_ok:
        return True                      # bars/fusion 都完整继承 strict 的通过面
    mode = A3_DIVER_MODE
    if mode == 'strict':
        return False
    if _bars_beyond(merged, k, p1, side, raw) >= A3_BARS_NEED:
        return True
    if mode == 'fusion':
        # 打穿深度：跌破前低的深度 ≥ 30% × 1-2 笔区间高度（真打穿，不是毛刺）
        zone = abs(p2 - p1)
        return zone > 0 and abs(p1 - p3) >= A3_DEPTH_RATIO * zone
    return False


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
        't1': str(struct.get('t1') or ''), 't2': str(struct.get('t2') or ''),
        't3': str(struct.get('t3') or ''),             # 结构三点时刻（详情页按卡片画结构区间用）
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


# 反向中枢否定（用户 2026-10-14 提出，做成开关由监控页⚙设置面板控制，默认关）：
# A类结构成立后，若 3' 之后连续三笔重叠成枢、且重叠区完全脱离原结构区间
# （空头：重叠区最低 > 顶1'；多头：重叠区最高 < 底1'），且期间没有任何笔端点
# 突破背驰点 3' → 原结构作废（不可借"回到确认区"复活）。破背驰点走原有逻辑。
OPP_ZS_KILL = False

# A类确认取消线（实验性，用户 2026-10-21 定稿，监控页⚙设置面板开关，默认关）：
# 默认关=A类确认报出后，等背驰点 3（做多最低点/做空最高点）被破坏才取消（原逻辑）；
# 开=取消线改为结构区间（详情页橙色矩形 1-2 区间）靠背驰点那一边：
#   做多=区间下沿（底1=p1），做空=区间上沿（顶1'=p1）。确认成立条件不变。
# **破线即永久作废，无"回升复活"**（用户纠正定稿）：实时价破了区间边，未来完成笔
# 的端点必然连到这个破线极值（笔端点=极值，不会消失），破线不可避免——
# 所以确认（含确认笔自身）以来任意已完结K线破线 → 卡片直接作废，不等完成笔。
CONF_ZONE_KILL = False


# A类背驰确认口径（三档，2026-10-24 用户定稿，矩阵页⚙设置面板控制，默认=原版）：
#   strict = 原版 R3：背驰点分型的三根合并K线，high(多)/low(空) 全部严格在 p1 外侧。
#            问题：V反太快时分型自己长高/扎低（焦煤1374右肩差0.5个点），结构永远不成立。
#   bars   = 三K线：最低点左右 ±A3_BARS_WIN 根合并K线内，存在 ≥A3_BARS_NEED 根
#            完全在 p1 外侧的K线即可，允许间断（中间K线缩回区间就跳过，
#            后面仍在外侧的继续数）——"价格在低位真实待过时间"。
#   fusion = 融合：strict / bars 任一满足即过；都不满足时再看打穿深度——
#            (p1-p3)/(p2-p1) ≥ A3_DEPTH_RATIO（跌破前低的深度达到 1-2 笔
#            区间高度的三成=真打穿不是毛刺），三条件任意成立即算背驰。
A3_DIVER_MODE = 'strict'      # 'strict' | 'bars' | 'fusion'
A3_DEPTH_RATIO = 0.30
A3_BARS_WIN = 5               # bars 档：最低点左右各看多少根合并K线
A3_BARS_NEED = 3              # bars 档：需要几根完全在外侧的K线


def _dead_by_opposite_zs(tail, p1, side):
    """反向中枢否定判定：tail 最近三笔的重叠区是否完全脱离原结构区间——
    空头：重叠区最低价 > 顶1'（新中枢整个在上方）；多头：重叠区最高价 < 底1'。
    若期间价格曾破背驰点 3'，调用方已先走原有逻辑，不会进到这里。"""
    w = tail[-3:]
    lo = max(min(x['start_price'], x['end_price']) for x in w)
    hi = min(max(x['start_price'], x['end_price']) for x in w)
    return lo > p1 if side == 'short' else hi < p1


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


def _eval_a_long(bi_list, merged, reported, raw=None):
    """做多：1(低)-2(高)-3(低,破1且分型全在1下方)。
    三阶段（用户术语）：观察（结构成立）→ 候选（向上笔突破2，VREV_LONG）
    → 确认（突破后第一根向下回调笔成立，VREV_CONF_LONG = 二买进场关注）。
    候选后状态机回 IDLE（§3.3）；确认每结构只报一次；确认后若回调跌破旧 3，
    自然走出新结构重新观察（循环判断）。"""
    state, struct = 'idle', None
    since_fire, last_fire = False, None       # A候选: (struct, ev, is_new, fire_i)
    since_conf, last_conf = False, None       # A确认: (struct, ev, is_new)
    events = []                               # 本轮新产生的事件（调用方负责推送）
    # 反向中枢否定追踪（开关 OPP_ZS_KILL，多头镜像）：struct_ctx=当前结构；
    # dead_tail=3'之后的笔；p3_broken=期间破过背驰点（破则走原有逻辑）
    struct_ctx, dead_tail, p3_broken = None, [], False
    for i, b in enumerate(bi_list):
        # 反向中枢否定：3' 之后连续三笔重叠且重叠区完全脱离结构区间（重叠区最高 < 底1'），
        # 且期间未破背驰点 → 原结构作废（机器重新算）。先破背驰点 → 走原有逻辑，不否定。
        if OPP_ZS_KILL and struct_ctx is not None and not p3_broken:
            if b['end_price'] < struct_ctx['p3']:
                p3_broken = True
            else:
                dead_tail.append(b)
                # 三笔重叠必须全是**已确认笔**（用户定稿 2026-10-14，多头镜像）：
                # 窗口取 dead_tail[-4:-1]，第四笔只做确认不进窗口。
                if len(dead_tail) >= 4 and _dead_by_opposite_zs(dead_tail[:-1], struct_ctx['p1'], 'long'):
                    struct_ctx = None
                    state, struct = 'idle', None
                    since_fire, since_conf = False, False
                    last_fire, last_conf = None, None
        if b['direction'] == 'down':
            if state == 'watch' and b['end_price'] < struct['p3']:
                state, struct = 'idle', None      # 新低 → 旧结构作废，立即重建检查（§3.4）
            if since_conf and b['end_price'] < (last_conf[0]['p1'] if CONF_ZONE_KILL else last_conf[0]['p3']):
                since_conf = False   # 确认后跌破取消线 → 假确认作废（默认=背驰点；实验开关=区间下沿p1，转新观察）
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
                        and _a3_gate(merged, b['end_idx'], p1, p2, p3, 'long', raw)):
                    struct = _mkstruct(bi_list[i - 1], b)
                    struct['anchor_i'] = i
                    state = 'watch'
                    since_fire = since_conf = False
                    struct_ctx, dead_tail, p3_broken = struct, [], False  # 新结构 → 追踪重置
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
        elif CONF_ZONE_KILL:
            # 实验口径：确认（含确认笔自身）以来任意已完结K线破区间下沿 → 永久作废。
            # 实时破线 ⇒ 未来完成笔端点必然连到该极值、破线不可避免，不存在回升复活。
            if float(merged['low'].iloc[ci[0]:].min()) < c_struct['p1']:
                since_conf = False
            else:
                return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
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


def _eval_a_short(bi_list, merged, reported, raw=None):
    """做空镜像：1'(顶)-2'(低)-3'(新高,破1'且顶分型全在1'上方)。三阶段同 _eval_a_long。"""
    state, struct = 'idle', None
    since_fire, last_fire = False, None       # A候选: (struct, ev, is_new, fire_i)
    since_conf, last_conf = False, None       # A确认: (struct, ev, is_new)
    events = []
    # 反向中枢否定追踪（开关 OPP_ZS_KILL，监控页⚙设置面板控制）：struct_ctx=当前结构；
    # dead_tail=3'之后的笔；p3_broken=期间破过背驰点（破则走原有逻辑，不再适用本规则）
    struct_ctx, dead_tail, p3_broken = None, [], False
    for i, b in enumerate(bi_list):
        # 反向中枢否定：3' 之后连续三笔重叠且重叠区完全脱离结构区间（重叠区最低 > 顶1'），
        # 且期间未破背驰点 → 原结构作废（不可借"回到确认区"复活，机器重新算）。
        # 先破背驰点 → 走原有逻辑（多空重新判定），不否定。
        if OPP_ZS_KILL and struct_ctx is not None and not p3_broken:
            if b['end_price'] > struct_ctx['p3']:
                p3_broken = True
            else:
                dead_tail.append(b)
                # 三笔重叠必须全是**已确认笔**（用户定稿 2026-10-14）：第四笔出现才确认
                # 第三笔的端点，最新完成笔的端点还可能被延伸/回退修订，不参与判定。
                # 即窗口取 dead_tail[-4:-1]：第四笔只做确认，自身不进重叠窗口。
                if len(dead_tail) >= 4 and _dead_by_opposite_zs(dead_tail[:-1], struct_ctx['p1'], 'short'):
                    struct_ctx = None
                    state, struct = 'idle', None
                    since_fire, since_conf = False, False
                    last_fire, last_conf = None, None
        if b['direction'] == 'up':
            if state == 'watch' and b['end_price'] > struct['p3']:
                state, struct = 'idle', None          # 新高 → 旧结构作废，立即重建检查
            if since_conf and b['end_price'] > (last_conf[0]['p1'] if CONF_ZONE_KILL else last_conf[0]['p3']):
                since_conf = False   # 确认后涨破取消线 → 假确认作废（默认=背驰点；实验开关=区间上沿p1，转新观察）
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
                        and _a3_gate(merged, b['end_idx'], p1, p2, p3, 'short', raw)):
                    struct = _mkstruct(bi_list[i - 1], b)
                    struct['anchor_i'] = i
                    state = 'watch'
                    since_fire = since_conf = False
                    struct_ctx, dead_tail, p3_broken = struct, [], False  # 新结构 → 追踪重置
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
        elif CONF_ZONE_KILL:
            # 实验口径（多头镜像）：确认以来任意已完结K线涨破区间上沿 → 永久作废，不回落复活。
            if float(merged['high'].iloc[ci[0]:].max()) > c_struct['p1']:
                since_conf = False
            else:
                return {'state': 'conf_a', 'struct': c_struct, 'fired': events}
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


def evaluate(bi_list, merged, reported_keys=None, raw=None):
    """形态评估总入口。

    bi_list: build_bi 输出（时间升序）；merged: merge_klines 输出（R3 分型位置检查用）。
    raw: 原始K线 DataFrame（可选，bars 档按原始K线数"完全在外侧"的K线，用户定稿
    2026-10-24 不考虑合并；不传时 bars 档退化合并口径）。
    reported_keys: 已报指纹集合（调用方按 (symbol,period) 持有并持久传入；
    本函数对首次触发的事件原地写入集合，并放入返回值 fired）。
    评估输入 = 完成笔 + 进行中笔（图上画出来的就要算，突破/成笔瞬间触发）。
    返回 {'long': {'state','struct','fired'}, 'short': {...}}，纯函数幂等。"""
    rep = reported_keys if reported_keys is not None else set()
    bis = _effective_bis(bi_list, merged)
    return {
        'long': _merge(_eval_a_long(bis, merged, rep, raw), _eval_c_long(bis, merged, rep)),
        'short': _merge(_eval_a_short(bis, merged, rep, raw), _eval_c_short(bis, merged, rep)),
    }
