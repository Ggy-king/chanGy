# -*- coding: utf-8 -*-
"""
回测模块（与实时预警 engine.py 完全分离）
- 实时预警：engine.py 每15分钟检测最新买卖点推手机
- 回测：本模块按"品种 + 时间区间"切片历史数据，跑 合并->分型->笔->底买顶卖配对
       返回统计(胜率/盈亏比/回撤/资金曲线) + K线/笔/买卖点图数据 + 交易明细
入口：
    run_backtest(symbol, months)   symbol='ALL' 时返回全部品种统计表（不画图）
months: 1/2/3 表示近 N 个月，0 表示全部历史
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
sys.path.insert(0, _HERE)

import pandas as pd
import akshare as ak
from chanlun import merge_klines, find_fenxing, build_bi
import config, fees

PERIODS = [0, 1, 2, 3]   # 0=全部


def _slice_by_months(df, months):
    """近 months 个月切片；months<=0 返回全部。按数据里最新时间往前推。"""
    if months and months > 0:
        end = df['datetime'].max()
        start = end - pd.DateOffset(months=months)
        df = df[df['datetime'] >= start].reset_index(drop=True)
    return df


def _build_kline_payload(df, merged, bi_list):
    """从切好的 df 构造前端画图数据：kline / vol / bi_line / markers / signals。"""
    kline, vol = [], []
    for _, r in df.iterrows():
        ts = int(r['datetime'].timestamp())
        kline.append({'time': ts, 'open': round(r['open'], 1), 'high': round(r['high'], 1),
                     'low': round(r['low'], 1), 'close': round(r['close'], 1)})
        vol.append({'time': ts, 'value': int(r['volume'])})

    bi_line = []
    if bi_list:
        s0 = merged['low_idx'].iloc[bi_list[0]['start_idx']]
        bi_line.append({'time': int(df['datetime'].iloc[s0].timestamp()),
                        'value': bi_list[0]['start_price']})
        for b in bi_list:
            col = 'high_idx' if b['direction'] == 'up' else 'low_idx'
            e = merged[col].iloc[b['end_idx']]
            bi_line.append({'time': int(df['datetime'].iloc[e].timestamp()),
                            'value': b['end_price']})

    markers, signals = [], []
    for b in bi_list:
        if b['direction'] == 'up':
            raw = int(merged['low_idx'].iloc[b['start_idx']])
            ts = int(df['datetime'].iloc[raw].timestamp())
            buy_price = round(float(df['close'].iloc[raw]), 1)
            markers.append({'time': ts, 'position': 'belowBar', 'color': '#ef5350',
                            'shape': 'arrowUp', 'text': '买'})
            signals.append({'side': 'buy', 'time': ts, 'price': buy_price,
                            'label': '底分型·买'})
        else:
            raw = int(merged['high_idx'].iloc[b['start_idx']])
            ts = int(df['datetime'].iloc[raw].timestamp())
            sell_price = round(float(df['close'].iloc[raw]), 1)
            markers.append({'time': ts, 'position': 'aboveBar', 'color': '#26a69a',
                            'shape': 'arrowDown', 'text': '平'})
            signals.append({'side': 'sell', 'time': ts, 'price': sell_price,
                            'label': '顶分型·平'})
    return kline, vol, bi_line, markers, signals


def _stats(signals, mult, open_cost, close_cost):
    """底买->顶平 配对，只做多。返回(统计dict, 交易list, 资金曲线)。"""
    trades, buy = [], None
    for s in signals:
        if s['side'] == 'buy' and buy is None:
            buy = s
        elif s['side'] == 'sell' and buy is not None:
            pts = s['price'] - buy['price']
            money = pts * mult - (open_cost or 0) - (close_cost or 0)
            trades.append({'buy_time': buy['time'], 'sell_time': s['time'],
                           'buy_price': buy['price'], 'sell_price': s['price'],
                           'pts': round(pts, 1), 'money': round(money, 1)})
            buy = None
    n = len(trades)
    if n == 0:
        return {'trades': 0, 'win_rate': None, 'pl_ratio': None, 'total_money': 0,
                'max_dd': 0, 'avg_dd': 0}, trades, []
    pts = [t['pts'] for t in trades]
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    total = round(sum(t['money'] for t in trades), 1)
    curve, cum = [], 0.0
    for t in trades:
        cum += t['money']
        curve.append({'time': t['sell_time'], 'value': round(cum, 0)})
    # 回撤
    peak, events, cur = curve[0]['value'], [], 0.0
    for c in curve:
        v = c['value']
        if v >= peak:
            if cur > 0:
                events.append(cur)
            peak, cur = v, 0.0
        else:
            cur = peak - v
    if cur > 0:
        events.append(cur)
    mdd = round(max(events), 1) if events else 0.0
    add = round(sum(events) / len(events), 1) if events else 0.0
    st = {
        'trades': n,
        'win_rate': round(len(wins) / n * 100, 1),
        'pl_ratio': round(avg_win / abs(avg_loss), 2) if avg_loss != 0 else None,
        'total_money': total,
        'max_dd': mdd, 'avg_dd': add,
    }
    return st, trades, curve


def _one(symbol, months):
    """单品种回测，返回完整结果 dict。"""
    name = config.CONTRACT_MAP[symbol]['name']
    df = ak.futures_zh_minute_sina(symbol=symbol, period=config.PERIOD_MAIN)
    df = df.reset_index(drop=True)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = _slice_by_months(df, months)

    merged = merge_klines(df)
    top_fx, bottom_fx = find_fenxing(merged)
    bi_list = build_bi(top_fx, bottom_fx, merged)

    kline, vol, bi_line, markers, signals = _build_kline_payload(df, merged, bi_list)
    fee = fees.get(symbol)
    st, trades, curve = _stats(signals, fee.get('multiplier', 10),
                               fee.get('open_cost', 0), fee.get('close_cost', 0))
    return {
        'symbol': symbol, 'name': name,
        'period': config.PERIOD_MAIN,
        'start': str(df['datetime'].iloc[0]), 'end': str(df['datetime'].iloc[-1]),
        'bar_count': len(df), 'bi_count': len(bi_list),
        'kline': kline, 'vol': vol, 'bi_line': bi_line,
        'markers': markers, 'signals': signals,
        'stats': st, 'trades': trades, 'equity': curve,
        'multiplier': fee.get('multiplier', 10),
    }


def run_backtest(symbol, months):
    """入口。symbol='ALL' 返回全部品种统计表+全局汇总；否则返回单品种完整回测。"""
    if str(symbol).upper() == 'ALL':
        rows = []
        for c in config.CONTRACTS:
            try:
                r = _one(c['symbol'], months)
                s = r['stats']
                rows.append({'symbol': c['symbol'], 'name': c['name'],
                             'bars': r['bar_count'], 'bi': r['bi_count'],
                             'trades': s['trades'], 'win_rate': s['win_rate'],
                             'pl_ratio': s['pl_ratio'], 'total_money': s['total_money'],
                             'max_dd': s['max_dd']})
            except Exception as e:
                rows.append({'symbol': c['symbol'], 'name': c['name'], 'error': str(e)[:60]})
        ok = [x for x in rows if 'error' not in x]
        g = {
            'watch': len(ok), 'failed': len(rows) - len(ok),
            'total_trades': sum(x['trades'] for x in ok),
            'total_money': round(sum(x['total_money'] for x in ok), 1),
            'avg_win_rate': round(sum(x['win_rate'] for x in ok if x['win_rate'] is not None)
                                  / max(1, len([x for x in ok if x['win_rate'] is not None])), 1) or None,
        }
        return {'mode': 'all', 'months': months, 'rows': rows, 'global': g}
    else:
        r = _one(symbol, months)
        r['mode'] = 'one'
        r['months'] = months
        return r
