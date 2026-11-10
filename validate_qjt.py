# -*- coding: utf-8 -*-
"""区间套验证 v2（2026-11-04 用户拍板口径）：

锚点 = 15分 A类确认（VREV_CONF）或 B类触发（TRIG）报出时刻的【回调笔起点】
      （做多=突破高点，做空=镜像低点）——用户定稿："15分确认后的第一笔向下才算
      3分真正意义上的第一笔向下"，观察锚方案已否决（观察时 15分仍在向下走势
      类型里，3分可以不回来）。
3分从锚点起"起始笔"重算，复用 evaluate 全管线：
  金卡候选 = 3分从锚点走出第一个中枢（build_zs ≥1）
  金卡确认 = 3分同向 A类确认（VREV_CONF，报卡）
  废卡     = 价格破坏锚点价 且 锚点后完成笔 <3
  破位不报 = 价格破坏锚点价 且 ≥3笔（用户不想报 3分B类，只统计供决策）
  前提失效 = 价格破 15分取消线（A类=p3 背驰点 / B类=p5）——AI 补充规则
  过期     = 锚点后 3分走完 7 笔仍无决断（用户定稿 7 笔）
口径同步：pattern_flags（bars/opp_zs_kill）+ pattern_bi_conf（宽松笔）。
数据缓存 data/qjt_cache/，重放零网络。用法：.venv/bin/python -u validate_qjt.py [品种...]
"""
import os, sys, json, time, statistics
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

import config
import tqsdk_data as tq
from chanlun import merge_klines, find_fenxing, build_bi, build_zs
from chanlun import pattern as P
from chanlun.pattern import evaluate as pattern_evaluate, _effective_bis
from engine import BI_CONF_DEFAULT

LEN15, LEN3 = 2000, 8000
WARMUP15 = 200
HORIZON3 = 160          # 3分信号后观察窗（根）≈8小时
MAX_BI = 7              # 卡片寿命（用户定稿）
CACHE = os.path.join('data', 'qjt_cache')
os.makedirs(CACHE, exist_ok=True)

try:
    flags = json.load(open('data/pattern_flags.json', encoding='utf-8'))
    P.A3_DIVER_MODE = flags.get('a3_diver_mode', P.A3_DIVER_MODE)
    P.OPP_ZS_KILL = flags.get('opp_zs_kill', P.OPP_ZS_KILL)
    P.CONF_ZONE_KILL = flags.get('conf_zone_kill', P.CONF_ZONE_KILL)
except Exception:
    flags = {}
BI_CONF = dict(BI_CONF_DEFAULT)
try:
    BI_CONF.update(json.load(open('data/pattern_bi_conf.json', encoding='utf-8')))
except Exception:
    pass
print(f"[口径] a3={P.A3_DIVER_MODE} opp_zs={P.OPP_ZS_KILL} conf_zone={P.CONF_ZONE_KILL} "
      f"笔={BI_CONF.get('bi_algo')}/strict={BI_CONF.get('is_strict')} 寿命={MAX_BI}笔")


def prep(df):
    df = df.reset_index(drop=True)
    if df['datetime'].dt.tz is None:
        df['datetime'] = pd.to_datetime(df['datetime'])
    return df.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)


def formal(df_sl):
    merged = merge_klines(df_sl)
    top_fx, bottom_fx = find_fenxing(merged)
    return merged, build_bi(top_fx, bottom_fx, merged, **BI_CONF)


def get_df(sym, sec, n):
    fp = os.path.join(CACHE, f'{sym}_{sec}.csv')
    if os.path.exists(fp):
        df = pd.read_csv(fp)
        df['datetime'] = pd.to_datetime(df['datetime'], utc=True).dt.tz_convert('Asia/Shanghai')
        return prep(df)
    df = prep(tq.fetch_kline(sym, sec, data_length=n, force=True))
    df.to_csv(fp, index=False)
    return df


def replay15(df):
    """15分逐根重放：锚点 = A类确认 / B类触发 时刻的回调笔起点。"""
    seen, anchors = set(), []
    for t in range(WARMUP15, len(df)):
        sl = df.iloc[:t]
        merged, bis = formal(sl)
        if not bis:
            continue
        res = pattern_evaluate(bis, merged, set(), raw=sl)
        if not any(res[s].get('fired') for s in ('long', 'short')):
            continue
        eff = _effective_bis(bis, merged)
        for side in ('long', 'short'):
            for ev in res[side].get('fired', []):
                pat = ev['pattern']
                if 'VREV_CONF' not in pat and 'TRIG' not in pat:
                    continue
                # 前提笔 = 确认/触发时刻刚完成(或进行中)的回调笔：端点价=事件价
                cb = next((b for b in reversed(eff)
                           if b['direction'] != ('up' if side == 'long' else 'down')
                           and abs(float(b['end_price']) - ev['price']) < 1e-6), None)
                if cb is None:
                    continue
                a = {'type': 'conf' if 'VREV_CONF' in pat else 'trig_b', 'side': side,
                     'fp': ev['fingerprint'],
                     'anchor_t': str(cb['start_datetime']), 'anchor_p': float(cb['start_price']),
                     'kill_p': float(ev['p3']) if 'VREV_CONF' in pat else float(ev['p1']),
                     'ev_t': str(ev['time']), 'ev_price': float(ev['price']), 'bar15': t}
                key = (a['type'], side, a['anchor_t'][:16], round(a['anchor_p'], 5))
                if key not in seen:
                    seen.add(key)
                    anchors.append(a)
    return anchors


def sim3(df3, merged3, bis3, a3, side, anchor_p, kill_p, anchor_kind):
    """3分锚定重放。笔索引是合并K线空间，经 merged3 的 start_idx/end_idx 换算。
    v3 语义（用户 11-04 反馈修正）：破锚点价仅当 <3 笔（中枢未成）才废卡，且仅限
    A类确认锚——B触锚破锚不判死（趋势延续前提）；≥3 笔破坏中枢后卡继续等确认。"""
    d = 1 if side == 'long' else -1
    m_start = merged3['start_idx'].values
    m_end = merged3['end_idx'].values
    j0 = max(int(pd.Series(m_start).searchsorted(a3, side='right') - 1), 0)
    anc = [b for b in bis3 if b['start_idx'] >= j0]
    if not anc:
        return {'outcome': '无笔'}
    out = {'n_bi': len(anc), 'cand_bi_n': None, 'cand_bar': None, 'cand_time': None,
           'decide_bi_n': None, 'decide_bar': None, 'opposite': False,
           'break_bi_n': None, 'reclaim': False}
    prev_raw = a3 - 1
    for k in range(1, len(anc) + 1):
        if k > MAX_BI:
            out['outcome'] = '过期'
            out['decide_bi_n'] = k - 1
            return out
        b = anc[k - 1]
        raw_bar = int(m_end[b['end_idx']])
        seg = df3.iloc[prev_raw + 1:raw_bar + 1]
        if len(seg):
            hi, lo = float(seg['high'].max()), float(seg['low'].min())
            broke_anchor = hi > anchor_p if d == 1 else lo < anchor_p
            broke_kill = lo < kill_p if d == 1 else hi > kill_p
            if broke_kill:
                out.update({'outcome': '前提失效', 'decide_bi_n': k, 'decide_bar': raw_bar})
                return out
            if broke_anchor and out['break_bi_n'] is None:
                out['break_bi_n'] = k
                if anchor_kind == 'conf' and k < 3:
                    out.update({'outcome': '废卡', 'decide_bi_n': k, 'decide_bar': raw_bar})
                    return out
                # ≥3笔（中枢已成）或 B触锚：卡活着，破锚价后继续等 3分确认
                out['reclaim'] = True
        sl = df3.iloc[:raw_bar + 1]
        res = pattern_evaluate(anc[:k], merged3.iloc[:b['end_idx'] + 1], set(), raw=sl)
        st = res[side]
        if out['cand_bi_n'] is None and k >= 2 and len(build_zs(anc[:k])) > 0:
            out.update({'cand_bi_n': k, 'cand_bar': raw_bar,
                        'cand_time': str(df3['datetime'].iloc[raw_bar])})
        for ev in st.get('fired', []):
            if 'VREV_CONF' in ev['pattern']:
                out.update({'outcome': 'A确认', 'fire_bar': raw_bar,
                            'fire_price': ev['price'], 'fire_bi_n': k,
                            'fire_time': str(df3['datetime'].iloc[raw_bar]),
                            'cand_bi_n': out['cand_bi_n'] or k,
                            'cand_time': out['cand_time'] or str(df3['datetime'].iloc[raw_bar])})
                return out
        for ev in res['short' if side == 'long' else 'long'].get('fired', []):
            if 'VREV_CONF' in ev['pattern']:
                out['opposite'] = True
        prev_raw = raw_bar
    out['outcome'] = '截尾'
    return out


def fwd_exc(df3, bar, side):
    """bar 之后 HORIZON3 根的有利/不利幅度（%）。"""
    seg = df3.iloc[bar:min(len(df3), bar + HORIZON3)]
    if not len(seg):
        return None, None
    p = float(df3['close'].iloc[bar])
    if side == 'long':
        return (float(seg['high'].max()) - p) / p * 100, (p - float(seg['low'].min())) / p * 100
    return (p - float(seg['low'].min())) / p * 100, (float(seg['high'].max()) - p) / p * 100


def run_sym(sym):
    name = config.CONTRACT_MAP.get(sym, {}).get('name', sym)
    df15 = get_df(sym, 900, LEN15)
    df3 = get_df(sym, 180, LEN3)
    t0 = time.time()
    anchors = replay15(df15)
    merged3, bis3 = formal(df3)
    print(f'  [3分覆盖 {str(df3["datetime"].iloc[0])[5:16]} ~ {str(df3["datetime"].iloc[-1])[5:16]}]')
    rows = []
    for a in anchors:
        at = pd.Timestamp(a['anchor_t'])
        if at.tz is None:
            at = at.tz_localize('Asia/Shanghai')
        i3 = int(df3['datetime'].searchsorted(at))
        if i3 < 30 or i3 > len(df3) - 60:
            rows.append({**a, 'outcome': '3分覆盖外'})
            continue
        r = sim3(df3, merged3, bis3, i3, a['side'], a['anchor_p'], a['kill_p'], a['type'])
        row = {**a, **r}
        if r['outcome'] == 'A确认':
            row['sig_p'] = float(df3['close'].iloc[r['fire_bar']])
            row['wait_min'] = round((pd.Timestamp(r['fire_time']) - at).total_seconds() / 60, 0)
            row['mfe'], row['mae'] = fwd_exc(df3, r['fire_bar'], a['side'])
        rows.append(row)
    n_watch = sum(1 for a in anchors if a['type'] == 'conf')
    print(f'\n=== {sym} {name}：锚点 {len(anchors)} (A确认锚 {n_watch} / B触锚 {len(anchors)-n_watch}) '
          f'[{time.time()-t0:.0f}s] ===')
    for r in rows:
        tag = 'A确' if r['type'] == 'conf' else 'B触'
        s = f"  {str(r['anchor_t'])[5:16]} {tag}{'多' if r['side']=='long' else '空'} 锚价{r['anchor_p']:.1f} → {r['outcome']}"
        if r['outcome'] == 'A确认':
            s += (f" 候选第{r['cand_bi_n']}笔 确认第{r['fire_bi_n']}笔 价{r['sig_p']:.1f}"
                  + (" 破锚后确认" if r.get('reclaim') else '')
                  + (f" 等{r['wait_min']/60:.1f}h" if r.get('wait_min') is not None else '')
                  + (f" MFE{r['mfe']:+.2f}%/MAE{r['mae']:+.2f}%" if r.get('mfe') is not None else ''))
        elif r['outcome'] in ('废卡', '前提失效', '过期'):
            s += f" 第{r.get('decide_bi_n', '?')}笔"
        if r.get('opposite'):
            s += ' 有反向信号'
        print(s)
    return rows


def main():
    syms = sys.argv[1:] or [c['symbol'] for c in config.monitor_contracts()]
    all_rows = []
    for sym in syms:
        try:
            all_rows += run_sym(sym)
        except Exception as e:
            print(f'{sym}: 失败 {type(e).__name__} {str(e)[:120]}')
    n = len(all_rows)
    cnt = Counter(r['outcome'] for r in all_rows)
    print(f'\n========== 区间套 v3 汇总（锚点 {n} 个，寿命 {MAX_BI} 笔） ==========')
    for k in ('A确认', '废卡', '前提失效', '过期', '截尾', '3分覆盖外', '无笔'):
        if cnt.get(k):
            print(f"{k}: {cnt[k]}  ({cnt[k]/n*100:.1f}%)")
    ok = [r for r in all_rows if r['outcome'] == 'A确认']
    if ok:
        cand = [r['cand_bi_n'] for r in ok]
        fire = [r['fire_bi_n'] for r in ok]
        print(f"\n金卡候选出现笔数: 中位{statistics.median(cand):.0f} "
              f"(分布{dict(sorted(Counter(cand).items()))})")
        print(f"金卡确认出现笔数: 中位{statistics.median(fire):.0f} "
              f"(分布{dict(sorted(Counter(fire).items()))})")
        waits = [r['wait_min'] for r in ok if r.get('wait_min') is not None]
        if waits:
            print(f"锚点(15分确认)→3分报卡 等待: 中位{statistics.median(waits)/60:.1f}h "
                  f"范围{min(waits)/60:.1f}~{max(waits)/60:.1f}h")
        mfes = [r['mfe'] for r in ok if r.get('mfe') is not None]
        maes = [r['mae'] for r in ok if r.get('mae') is not None]
        if mfes:
            print(f"报卡后8h MFE 中位{statistics.median(mfes):+.2f}% 均值{statistics.mean(mfes):+.2f}%；"
                  f"MAE 中位{statistics.median(maes):+.2f}%")
        opp = sum(1 for r in ok if r.get('opposite'))
        print(f"报卡前出现过反向3分确认: {opp}/{len(ok)}")
    bx = [r for r in all_rows if r['outcome'] == '破位不报' and r.get('b_mfe') is not None]
    if bx:
        win = sum(1 for r in bx if r['b_mfe'] > r['b_mae'])
        print(f"\nB出口若报卡的数据({len(bx)}条): MFE 中位{statistics.median(r['b_mfe'] for r in bx):+.2f}% "
              f"MAE 中位{statistics.median(r['b_mae'] for r in bx):+.2f}%，MFE>MAE {win}/{len(bx)}")
    json.dump({'rows': all_rows}, open('data/qjt_validation_report.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('明细已存 data/qjt_validation_report.json')
    tq.TqData().close()


if __name__ == '__main__':
    main()
