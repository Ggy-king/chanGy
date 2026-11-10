# -*- coding: utf-8 -*-
"""区间套金卡（2026-11-04 用户定稿）· 纯评估逻辑。

规则（validate_qjt.py v3 同款，历史验证 374 锚点 / 报卡 28 张 / MFE+1.37% MAE+0.59%）：
  锚点   = 15分 A类确认(VREV_CONF)/B类触发(TRIG) 时刻的回调笔起点（做多=突破高点）。
  起始笔 = 3分只认锚点之后的笔（锚点之前的笔全部不参与评估）。
  候选   = 3分从锚点走出第一个中枢（build_zs ≥1，中位第 4 笔）。
  确认   = 3分同向 A类确认（VREV_CONF，报卡；中位第 6 笔）。
  废卡   = A类确认锚：价格破坏锚点价且当时锚点后完成笔 <3（"中枢未成先破锚"）。
           B触锚：破锚点价不判死（趋势延续前提）。
  前提失效 = 价格破 15分取消线（A类=p3 背驰点 / B类=p5）→ 卡死（任意笔数）。
  过期   = 锚点后 3分走完 7 笔仍无决断；或 3分窗口滚过锚点（数据不够回看）。
幂等：同一份 (df3, merged3, bis3) 重复评估结果一致（server 每 3 分全量重放一次）。
"""
import pandas as pd

from chanlun import build_zs
from chanlun.pattern import evaluate as pattern_evaluate

MAX_BI = 7          # 卡片寿命（用户定稿 2026-11-04）
MIN_BI_FOR_DEAD = 3  # A类确认锚：破锚点价时完成笔少于 3 → 废卡

REASON = {'dead_break': '中枢未成先破锚点价',
          'dead_kill': '价格破 15 分取消线（前提失效）',
          'expired': f'{MAX_BI} 笔内未走出确认',
          'rolled': '3 分窗口已滚过锚点'}


def eval_card(card, df3, merged3, bis3):
    """对一张活卡做 3 分锚定重放（幂等）。

    card: {'side', 'kind_anchor': 'conf'|'trig_b', 'anchor_t': str, 'anchor_p': float,
           'kill_p': float}
    df3/merged3/bis3: engine.pattern_snapshot 扩展返回的 3 分原始K线/合并K线/完成笔。
    返回 dict：
      outcome: 'pending' | 'confirm' | 'dead_break' | 'dead_kill' | 'expired'
      bi_n(锚点后完成笔数) / cand_bi_n / cand_time / fire_bi_n / fire_price / fire_time
      decide_bi_n / break_bi_n / opposite / reason
    """
    side = card['side']
    d = 1 if side == 'long' else -1
    out = {'outcome': 'pending', 'bi_n': 0, 'cand_bi_n': None, 'cand_time': None,
           'fire_bi_n': None, 'fire_price': None, 'fire_time': None,
           'decide_bi_n': None, 'break_bi_n': None, 'opposite': False, 'reason': None}
    if df3 is None or len(df3) == 0:
        return out
    try:
        at = pd.Timestamp(card['anchor_t'])
        if at.tz is None:
            tz0 = df3['datetime'].iloc[0].tz
            at = at.tz_localize(tz0 or 'Asia/Shanghai')
        a3 = int(df3['datetime'].searchsorted(at))
    except Exception:
        return out
    if a3 < 5:      # 锚点已滚出 3 分回看窗口（等待超过窗口覆盖）→ 过期
        out['outcome'] = 'expired'
        out['reason'] = REASON['rolled']
        return out
    m_start = merged3['start_idx'].values
    m_end = merged3['end_idx'].values
    j0 = max(int(pd.Series(m_start).searchsorted(a3, side='right') - 1), 0)
    anc = [b for b in bis3 if b['start_idx'] >= j0]
    out['bi_n'] = len(anc)
    if not anc:
        return out
    anchor_p = float(card['anchor_p'])
    kill_p = float(card['kill_p'])
    conf_anchor = card.get('kind_anchor') == 'conf'
    opp_side = 'short' if side == 'long' else 'long'
    prev_raw = a3 - 1
    for k in range(1, len(anc) + 1):
        if k > MAX_BI:
            out['outcome'] = 'expired'
            out['decide_bi_n'] = k - 1
            out['reason'] = REASON['expired']
            return out
        b = anc[k - 1]
        raw_bar = int(m_end[b['end_idx']])
        seg = df3.iloc[prev_raw + 1:raw_bar + 1]
        if len(seg):
            hi, lo = float(seg['high'].max()), float(seg['low'].min())
            broke_anchor = hi > anchor_p if d == 1 else lo < anchor_p
            broke_kill = lo < kill_p if d == 1 else hi > kill_p
            if broke_kill:
                out.update({'outcome': 'dead_kill', 'decide_bi_n': k,
                            'reason': REASON['dead_kill']})
                return out
            if broke_anchor and out['break_bi_n'] is None:
                out['break_bi_n'] = k
                if conf_anchor and k < MIN_BI_FOR_DEAD:
                    out.update({'outcome': 'dead_break', 'decide_bi_n': k,
                                'reason': REASON['dead_break']})
                    return out
                # ≥3 笔（中枢已成立）或 B触锚：卡活着，破锚价后继续等确认
        sl = df3.iloc[:raw_bar + 1]
        res = pattern_evaluate(anc[:k], merged3.iloc[:b['end_idx'] + 1], set(), raw=sl)
        if out['cand_bi_n'] is None and k >= 2 and len(build_zs(anc[:k])) > 0:
            out['cand_bi_n'] = k
            out['cand_time'] = str(df3['datetime'].iloc[raw_bar])
        for ev in res[side].get('fired', []):
            if 'VREV_CONF' in ev['pattern']:
                out.update({'outcome': 'confirm', 'fire_bi_n': k,
                            'fire_price': ev['price'],
                            'fire_time': str(df3['datetime'].iloc[raw_bar])})
                return out
        for ev in res[opp_side].get('fired', []):
            if 'VREV_CONF' in ev['pattern']:
                out['opposite'] = True
        prev_raw = raw_bar
    return out     # 7 笔内未决 → pending（下轮 3 分扫描继续）
