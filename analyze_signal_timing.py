# -*- coding: utf-8 -*-
"""信号时机分析（一次性）：A类确认 / B类触发 报出时刻 vs 回调笔图上正式成立时刻。

逐根K线重放线上同一链路（merge_klines → find_fenxing → build_bi → pattern.evaluate），
对每个首次报出的信号向后寻找"正式笔列表新增同向笔"的那根K线（=图上第一次画出该笔），
对比：早报进场 vs 等笔成立进场的价差、信号后逆向热度、后续最大有利幅度、p3 作废率。
用法：.venv/bin/python -u analyze_signal_timing.py
"""
import os, sys, json, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import tqsdk_data as tq
from chanlun import merge_klines, find_fenxing, build_bi
from chanlun.pattern import evaluate as pattern_evaluate, _effective_bis
from engine import BI_CONF_DEFAULT

PERIOD_SEC = 300          # 5分钟（用户截图级别）
DATA_LEN = 1400
WARMUP = 300              # 前 300 根做笔的上下文，不产信号
HORIZON = 48              # 信号后观察窗口（根）：48×5min=4小时
SYMBOLS = ['jd2612', 'MA2611', 'v2701', 'SA2701', 'SR2701', 'CF2701', 'RB2701', 'RU2701']


def prep(df):
    df = df.reset_index(drop=True)
    if df['datetime'].dt.tz is None:
        df['datetime'] = pd.to_datetime(df['datetime'])
    return df.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)


def formal(df_sl):
    """线上同款：合并→分型→正式笔（图上画的笔）。"""
    merged = merge_klines(df_sl)
    top_fx, bottom_fx = find_fenxing(merged)
    return merged, build_bi(top_fx, bottom_fx, merged, **dict(BI_CONF_DEFAULT))


def replay(sym):
    df = prep(tq.fetch_kline(sym, PERIOD_SEC, data_length=DATA_LEN, force=True))
    out = []
    seen = {}                                  # (side,pattern,fp) -> 首报 eval_t
    for t in range(WARMUP, len(df)):
        sl = df.iloc[:t]
        merged, bis = formal(sl)
        if not bis:
            continue
        res = pattern_evaluate(bis, merged, set())
        for side in ('long', 'short'):
            for ev in res[side].get('fired', []):
                kind = ('A确认' if 'CONF' in ev['pattern'] else
                        'B触发' if 'TRIG' in ev['pattern'] else None)
                if not kind:
                    continue
                key = (side, ev['pattern'], ev['fingerprint'])
                if key in seen:
                    continue
                seen[key] = t
                # 触发笔 = 进行中笔列表里端点价等于事件价的笔
                eff = _effective_bis(bis, merged)
                cb = next((b for b in reversed(eff)
                           if abs(float(b['end_price']) - ev['price']) < 1e-6), eff[-1])
                out.append({
                    'kind': kind, 'side': side, 'fp': ev['fingerprint'],
                    'eval_t': t, 'sig_bar': t - 1,
                    'sig_time': str(df['datetime'].iloc[t - 1]),
                    'p_sig': float(df['close'].iloc[t - 1]),
                    'p3': ev['p3'],
                    'cb_dir': cb['direction'], 'cb_end_idx': int(cb['end_idx']),
                    'cb_end_price': float(cb['end_price']),
                    'formal_last_dir': bis[-1]['direction'], 'formal_n': len(bis),
                })
    # 逐信号向后找正式成立时刻
    for s in out:
        s['form_bar'] = None
        for t2 in range(s['eval_t'] + 1, len(df)):
            _, bis2 = formal(df.iloc[:t2])
            if (len(bis2) > s['formal_n'] and bis2[-1]['direction'] == s['cb_dir']
                    and int(bis2[-1]['end_idx']) >= s['cb_end_idx'] - 2):
                s['form_bar'] = t2 - 1
                s['form_time'] = str(df['datetime'].iloc[t2 - 1])
                s['p_form'] = float(df['close'].iloc[t2 - 1])
                break
    # 指标（dir=+1 多 / -1 空，收益按信号方向折算）
    for s in out:
        d = 1 if s['side'] == 'long' else -1
        sb, sig_end = s['sig_bar'], min(len(df), s['sig_bar'] + HORIZON)
        seg = df.iloc[sb:sig_end]
        if s['form_bar'] is None:
            s['delay'] = None
            continue
        fb = s['form_bar']
        s['delay'] = fb - sb
        s['entry_diff'] = (s['p_form'] - s['p_sig']) * d / s['p_sig'] * 100
        mid = df.iloc[sb:fb + 1]
        ext = float(mid['low'].min()) if d == 1 else float(mid['high'].max())
        s['heat'] = (ext - s['p_sig']) * d / s['p_sig'] * 100        # 负=先吃亏
        s['broke_p3'] = (float(mid['low'].min()) < s['p3']) if d == 1 else \
                        (float(mid['high'].max()) > s['p3'])
        s['mfe_sig'] = (float(seg['high'].max()) - s['p_sig']) * d / s['p_sig'] * 100 \
            if d == 1 else (s['p_sig'] - float(seg['low'].min())) / s['p_sig'] * 100
        seg2 = df.iloc[fb:min(len(df), fb + HORIZON)]
        s['mfe_form'] = (float(seg2['high'].max()) - s['p_form']) * d / s['p_form'] * 100 \
            if d == 1 else (s['p_form'] - float(seg2['low'].min())) / s['p_form'] * 100
    return df, out


def agg(vals):
    vals = [v for v in vals if v is not None]
    return f'中位{statistics.median(vals):+.2f}% 均值{statistics.mean(vals):+.2f}%' if vals else '无'


def main():
    syms = sys.argv[1:] or SYMBOLS
    all_sig, unres = [], []
    for sym in syms:
        try:
            df, sigs = replay(sym)
        except Exception as e:
            print(f'{sym}: 拉数/回放失败 {str(e)[:100]}')
            continue
        done = [s for s in sigs if s['delay'] is not None]
        print(f'\n=== {sym} ({config_name(sym)}) {len(df)}根K线, 信号{len(sigs)}条, '
              f'窗口内成立{len(done)}条 ===')
        for s in sigs:
            if s['delay'] is None:
                unres.append(s)
                print(f"  {s['sig_time'][5:16]} {s['kind']}{'多' if s['side']=='long' else '空'} "
                      f"p={s['p_sig']:.0f} → 窗口内未成立（数据截尾）")
                continue
            all_sig.append(s)
            print(f"  {s['sig_time'][5:16]} {s['kind']}{'多' if s['side']=='long' else '空'} "
                  f"p={s['p_sig']:.0f} → 笔成立 {s['form_time'][5:16]} 晚{s['delay']}根 "
                  f"进场差{s['entry_diff']:+.2f}% 热度{s['heat']:+.2f}% "
                  f"作废{'是' if s['broke_p3'] else '否'} "
                  f"MFE早{s['mfe_sig']:+.2f}%/晚{s['mfe_form']:+.2f}%")
    if not all_sig:
        print('\n无样本')
        return
    for kind in ('A确认', 'B触发'):
        g = [s for s in all_sig if s['kind'] == kind]
        if not g:
            continue
        delays = [s['delay'] for s in g]
        print(f"\n---- {kind} 汇总（n={len(g)}）----")
        print(f"报出→笔成立延迟: 中位{statistics.median(delays):.0f}根"
              f"({statistics.median(delays)*5:.0f}分钟) 最长{max(delays)}根")
        print(f"等笔成立的进场差(正=更贵): {agg([s['entry_diff'] for s in g])}")
        print(f"早报进场后的逆向热度: {agg([s['heat'] for s in g])}  "
              f"最差{min(s['heat'] for s in g):+.2f}%")
        n_inv = sum(1 for s in g if s['broke_p3'])
        print(f"作废率(p3被破): {n_inv}/{len(g)} = {n_inv/len(g)*100:.0f}%")
        better = sum(1 for s in g if s['mfe_sig'] > s['mfe_form'])
        print(f"早报MFE优于等笔: {better}/{len(g)}；"
              f"早报MFE {agg([s['mfe_sig'] for s in g])} vs 等笔MFE {agg([s['mfe_form'] for s in g])}")
    json.dump({'signals': all_sig, 'unresolved': unres},
              open('data/signal_timing_report.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'\n明细已存 data/signal_timing_report.json（未成笔 {len(unres)} 条）')


def config_name(sym):
    import config
    return config.CONTRACT_MAP.get(sym, {}).get('name', sym)


if __name__ == '__main__':
    main()
