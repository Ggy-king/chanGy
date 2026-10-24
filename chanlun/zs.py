"""缠论中枢（笔级别）：三笔重叠成枢。

理念：原著中枢定义是"至少三个连续次级别走势类型的重叠"，但"次级别"到底对应
哪个K线级别在原著中是模糊的、无法落实的，因此任何用"次级别走势类型"模拟本级别
中枢的做法都不可靠。唯一可落地的做法：用本级别的笔作为构成单元（笔中枢）。

规则（默认 = 最标准缠论）：
1. 相邻三笔端点价格区间有公共重叠（min(高点) > max(低点)）→ 成枢；
2. 中枢区间 [ZD, ZG] = 成枢三笔的重叠区，成枢后固定（对应原著 ZG/ZD 定义）；
3. 延伸：后续笔与 [ZD, ZG] 重叠 → 只延长中枢结束笔，区间不变；
4. 进入笔 bi_in = 成枢三笔之前那一笔；离开笔 bi_out = 第一笔与区间不重叠的笔；
5. 不看线段、不看次级别，纯本级别笔。

开关（边界处理参考 chan.py，默认值=标准缠论）：
- zs_extend:   中枢延伸（默认开）。关掉后中枢只由成枢三笔构成，后续笔不延长。
- zs_merge:    相邻中枢合并（默认关）。区间重叠的相邻中枢并成一个（中枢扩张式），
               chan.py 的 need_combine/zs_combine_mode='zs' 同款逻辑。
- zs_first_bi: 允许第一笔就是中枢起点（默认关=禁止，与 chan.py
               "禁止 begin_bi.idx==0 成枢" 的处理一致）。
"""


def _rng(b):
    """一笔的价格区间：取两端点价格的高低。"""
    lo = min(b['start_price'], b['end_price'])
    hi = max(b['start_price'], b['end_price'])
    return lo, hi


def build_zs(bi_list, zs_extend=True, zs_merge=False, zs_first_bi=False):
    """从最终笔列表构建中枢。笔形一变，中枢自动跟着变（每次现算，无缓存状态）。

    返回 list[dict]：
    {'begin_bi': 起始笔下标, 'end_bi': 结束笔下标,
     'low': ZD, 'high': ZG,
     'bi_in': 进入笔下标(-1=无), 'bi_out': 离开笔下标(-1=尚未离开),
     'merged_count': 合并了几个中枢(未合并=1)}
    """
    zs_list = []
    cur = None
    free = []  # 暂未入枢的笔下标（滑动窗口，参考 chan.py free_item_lst）

    for idx, b in enumerate(bi_list):
        lo, hi = _rng(b)
        consumed = False
        if cur is not None:
            if zs_extend and lo <= cur['high'] and hi >= cur['low']:
                # 延伸：区间不变，只延长结束笔
                cur['end_bi'] = idx
                cur['bi_out'] = -1
                consumed = True
            else:
                # 该笔与区间不重叠 → 离开笔，中枢完结
                cur['bi_out'] = idx
                cur = None
                # 离开笔本身落入自由列表，参与下一个中枢的窗口判断
        if not consumed:
            free.append(idx)
            if len(free) >= 3:
                w = free[-3:]
                lows, highs = [], []
                for i in w:
                    l, h = _rng(bi_list[i])
                    lows.append(l)
                    highs.append(h)
                if min(highs) > max(lows):
                    if (not zs_first_bi) and w[0] == 0:
                        # chan.py：禁止第一笔就是中枢的起点
                        pass
                    else:
                        cur = {
                            'begin_bi': w[0], 'end_bi': w[2],
                            'low': max(lows), 'high': min(highs),
                            'bi_in': w[0] - 1 if w[0] > 0 else -1,
                            'bi_out': -1,
                            'merged_count': 1,
                        }
                        zs_list.append(cur)
                        # 成枢三笔的最后一笔保留在自由窗口中：它本身已经离开中枢方向，
                        # 是新中枢的第一笔候选（原著：离开中枢的笔自然成为下一走势段起点）。
                        free = [w[2]]

    # 相邻中枢合并（默认关）：区间重叠 → 并成一个（扩张式合并）
    if zs_merge:
        merged_lst = []
        for z in zs_list:
            if merged_lst:
                p = merged_lst[-1]
                if z['low'] <= p['high'] and z['high'] >= p['low']:
                    p['low'] = min(p['low'], z['low'])
                    p['high'] = max(p['high'], z['high'])
                    p['end_bi'] = z['end_bi']
                    p['bi_out'] = z['bi_out']
                    p['merged_count'] += z['merged_count']
                    continue
            merged_lst.append(z)
        zs_list = merged_lst

    return zs_list


def zs_trend(bi_list, zs_list=None):
    """中枢序列方向（顺大逆小影子打标 v1，2026-10-23 与用户对齐定稿）。

    返回 'up' / 'down'（永远二值，没有"震荡"桶）：
    1. 分离：后中枢整体高于前中枢 → up；低于 → down；
    2. 重叠但重心漂移（中枢扩张）：重心上移 → up，下移 → down
       （15分向下扩张 = 15分还是向下，方向照定）；
    3. 真·喇叭口扩展（后中枢上下沿都包住前中枢、重心判不动）：跳过这对往前翻；
    4. 兜底：翻遍都定不了 → 最后一笔方向；无笔 → None。"""
    if zs_list is None:
        zs_list = build_zs(bi_list)
    if not zs_list:
        return (bi_list[-1]['direction'] if bi_list else None)
    i = len(zs_list) - 1
    while i >= 1:
        a, b = zs_list[i - 1], zs_list[i]
        if b['low'] > a['high']:
            return 'up'                      # 分离：整体在上
        if b['high'] < a['low']:
            return 'down'                    # 分离：整体在下
        if not (b['low'] <= a['low'] and b['high'] >= a['high']):   # 喇叭口（后包前）→ 跳过往前翻
            ca = (a['high'] + a['low']) / 2.0
            cb = (b['high'] + b['low']) / 2.0
            if cb > ca:
                return 'up'                  # 重叠但重心上漂（向上扩张）
            if cb < ca:
                return 'down'                # 重叠但重心下漂（向下扩张）
        i -= 1
    return (bi_list[-1]['direction'] if bi_list else None)
