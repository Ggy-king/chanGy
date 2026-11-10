# -*- coding: utf-8 -*-
"""区间套金卡纯逻辑单测（自跑式）：eval_card 四终局 + 候选时机。
用法：.venv/bin/python tests/test_qsq.py"""
import os, sys, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chanlun.pattern as _P
_P.A3_DIVER_MODE = 'bars'            # 单测固定口径=线上当前档（data/pattern_flags.json）
from chanlun import merge_klines, find_fenxing, build_bi
from chanlun.qsq import eval_card, MAX_BI
from engine import BI_CONF_DEFAULT


def build_df(legs, t0=pd.Timestamp('2026-11-03 09:00:00', tz='Asia/Shanghai'), step_min=3,
             start=94.0):
    """legs: [(目标价, 根数), ...]，从 start 起，每根 3 分钟，腿内线性单调（防包含合并干扰）。
    转折处不等高/不等低（上腿末根 hi=c+0.4、下腿首根 hi=o+0.05），保证分型严格成立。"""
    rows, price, ts = [], start, t0
    up = legs and legs[0][0] >= 100.0
    for target, n in legs:
        for i in range(n):
            nxt = price + (target - price) * (i + 1) / n
            o, c = price, nxt
            if up:
                hi, lo = c + 0.4, o - 0.05
            else:
                hi, lo = o + 0.05, c - 0.4
            rows.append({'datetime': ts, 'open': o, 'high': hi, 'low': lo, 'close': c,
                         'volume': 1})
            price, ts = c, ts + pd.Timedelta(minutes=step_min)
        up = target >= price
    df = pd.DataFrame(rows).reset_index(drop=True)
    merged = merge_klines(df)
    top_fx, bottom_fx = find_fenxing(merged)
    bis = build_bi(top_fx, bottom_fx, merged, **dict(BI_CONF_DEFAULT))
    return df, merged, bis


def card_at(df, bar_i, anchor_p, kill_p, side='long', kind_anchor='conf'):
    return {'side': side, 'kind_anchor': kind_anchor,
            'anchor_t': str(df['datetime'].iloc[bar_i]), 'anchor_p': anchor_p,
            'kill_p': kill_p}


def T(name, cond, info=''):
    print(('  PASS ' if cond else '  FAIL ') + name + ('  ' + info if info else ''))
    assert cond, name


def main():
    print('[qsq] 单测开始')

    # ① 标准确认路径：锚(100.4峰) → 下90 → 上96 → 下88(新低,中枢) → 上97(破96,不破锚) → 下91(不破88) → 小升(确认91分型)
    df, merged, bis = build_df([(100, 6), (90, 7), (96, 7), (88, 7), (97, 7), (91, 7), (93, 5)])
    anchor_p = float(df['high'].iloc[5])          # 生产口径：锚点=笔端点极值
    r = eval_card(card_at(df, 5, anchor_p, 85.0), df, merged, bis)
    T('标准路径报确认', r['outcome'] == 'confirm', str(r))
    T('候选=第4笔（build_zs 默认笔1为进入段，中枢=2-4笔）', r['cand_bi_n'] == 4,
      f"cand={r['cand_bi_n']}")
    T('确认=第5笔（回调）', r['fire_bi_n'] == 5, f"fire={r['fire_bi_n']}")

    # ② A类确认锚：中枢未成（第2笔）就破回锚价 → 废卡
    df, merged, bis = build_df([(100, 6), (95, 6), (101, 6), (99, 6)])
    r = eval_card(card_at(df, 5, float(df['high'].iloc[5]), 85.0), df, merged, bis)
    T('第2笔破锚 → 废卡', r['outcome'] == 'dead_break', str(r))

    # ③ 同路径但 B触锚：破锚不判死（卡活着/继续）
    df, merged, bis = build_df([(100, 6), (95, 6), (101, 6), (99, 6)])
    r = eval_card(card_at(df, 5, float(df['high'].iloc[5]), 85.0, kind_anchor='trig_b'),
                  df, merged, bis)
    T('B触锚破锚不判死', r['outcome'] != 'dead_break', str(r))

    # ④ 破 15分取消线 → 前提失效（任意笔数）
    df, merged, bis = build_df([(100, 6), (94, 8), (97, 6)])
    r = eval_card(card_at(df, 5, float(df['high'].iloc[5]), 96.0), df, merged, bis)
    T('破取消线 → 前提失效', r['outcome'] == 'dead_kill', str(r))

    # ⑤ 7 笔内不破锚/不破线/无新低结构（二探不破前低）→ 过期
    legs = [(100, 6)]
    p = 100.0
    lo = 92.0
    up_target = 97.0
    legs += [(lo, 7), (up_target, 7)]           # 下92 上97（均不越界）
    for i in range(7):                          # 7 笔窄幅震荡：低点抬升不破 92，高点不破 100
        lo = lo + 0.3
        up_target = up_target - 0.4
        legs += [(lo, 7), (up_target, 7)]
    df, merged, bis = build_df(legs)
    r = eval_card(card_at(df, 5, float(df['high'].iloc[5]), 85.0), df, merged, bis)
    T(f'寿命 {MAX_BI} 笔无决断 → 过期', r['outcome'] == 'expired', str(r))

    # ⑥ 锚点滚出窗口 → 过期(rolled)
    df, merged, bis = build_df([(100, 6), (95, 7)])
    r = eval_card({'side': 'long', 'kind_anchor': 'conf',
                   'anchor_t': '2026-10-01 09:00:00+08:00', 'anchor_p': 100.0,
                   'kill_p': 85.0}, df, merged, bis)
    T('锚点滚出窗口 → 过期', r['outcome'] == 'expired' and '窗口' in (r.get('reason') or ''), str(r))

    print('[qsq] 全部通过')


if __name__ == '__main__':
    main()
