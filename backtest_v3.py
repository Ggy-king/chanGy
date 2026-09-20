# -*- coding: utf-8 -*-
"""
甲醇15分钟 缠论笔 + 底买顶卖回测（只做多）
- K线合并 -> 顶底分型 -> 笔
- 每个 up 笔 = 一次"底分型买、顶分型卖"的多单
- 成交价采用【分型确认K线收盘价】(右侧确认, 无未来函数);
  同时记录【理论极值价】(底最低买/顶最高卖) 作为对照, 揭示信号延迟成本
- TradingView lightweight-charts 渲染: K线+成交量+笔+买卖点+统计面板+资金曲线
"""
import sys, os, json
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
sys.path.insert(0, _HERE)

import akshare as ak
import pandas as pd
from chanlun import merge_klines, find_fenxing, build_bi

# ============ 回测参数 ============
MULTIPLIER = 10          # 甲醇合约乘数: 10吨/手, 每1个点(1元/吨) = 10元/手
COST_PER_SIDE = 0.0      # 单边手续费(元/手), 0=暂不计; 实测可填 3~12

# ============ 1. 拉数据 ============
print("拉取数据...")
df = ak.futures_zh_minute_sina(symbol="MA0", period="15")
df = df.reset_index(drop=True)
df['datetime'] = pd.to_datetime(df['datetime'])
print(f"原始K线: {len(df)}")

# ============ 2. 缠论结构 ============
merged = merge_klines(df)
top_fx, bottom_fx = find_fenxing(merged)
bi_list = build_bi(top_fx, bottom_fx, merged)
print(f"合并后: {len(merged)}, 顶分型:{len(top_fx)}, 底分型:{len(bottom_fx)}, 笔:{len(bi_list)}")

# ============ 3. 回测：每个 up 笔一次多单 ============
trades = []
skipped_open = 0
for bi in bi_list:
    if bi['direction'] != 'up':
        continue
    s, e = bi['start_idx'], bi['end_idx']
    # 顶分型右侧确认K线必须存在, 否则最后一笔尚未确认, 不计入
    if e + 1 >= len(merged):
        skipped_open += 1
        continue

    # 理论极值(上帝视角, 实盘拿不到)
    rb = int(merged['low_idx'].iloc[s])
    rs = int(merged['high_idx'].iloc[e])
    theo_buy = float(df['low'].iloc[rb])
    theo_sell = float(df['high'].iloc[rs])

    # 可成交价: 分型由 i-1/i/i+1 三根构成, i+1 收盘才确认 -> 用 i+1 这根合并K线
    # 最后一根原始K线的收盘价
    cb_raw = int(merged['end_idx'].iloc[s + 1])
    cs_raw = int(merged['end_idx'].iloc[e + 1])
    buy_t = df['datetime'].iloc[cb_raw]
    sell_t = df['datetime'].iloc[cs_raw]
    buy = float(df['close'].iloc[cb_raw])
    sell = float(df['close'].iloc[cs_raw])

    pts = sell - buy
    pnl_gross = pts * MULTIPLIER
    pnl = pnl_gross - 2 * COST_PER_SIDE
    theo_pts = theo_sell - theo_buy
    hold_bars = cs_raw - cb_raw
    hold_min = hold_bars * 15

    trades.append({
        'buy_time': str(buy_t), 'buy': round(buy, 1),
        'sell_time': str(sell_t), 'sell': round(sell, 1),
        'pts': round(pts, 1), 'pnl': round(pnl, 0),
        'theo_pts': round(theo_pts, 1),
        'theo_buy': round(theo_buy, 1), 'theo_sell': round(theo_sell, 1),
        'hold_bars': hold_bars, 'hold_hours': round(hold_min / 60, 1),
        'win': pts > 0,
        'eq_time': int(sell_t.timestamp()),
    })

# ============ 统计(可成交价口径) ============
n = len(trades)
wins = [t for t in trades if t['win']]
losses = [t for t in trades if not t['win']]
win_rate = len(wins) / n if n else 0
avg_win = sum(t['pts'] for t in wins) / len(wins) if wins else 0
avg_loss = sum(t['pts'] for t in losses) / len(losses) if losses else 0
payoff = (avg_win / abs(avg_loss)) if avg_loss else float('inf')
net_pts = sum(t['pts'] for t in trades)
net_pnl = sum(t['pnl'] for t in trades)
gross_win = sum(t['pts'] for t in wins)
gross_loss = sum(t['pts'] for t in losses)
profit_factor = (gross_win / abs(gross_loss)) if gross_loss else float('inf')
theo_net_pts = sum(t['theo_pts'] for t in trades)
max_win = max((t['pts'] for t in wins), default=0)
max_loss = min((t['pts'] for t in losses), default=0)
avg_hold = sum(t['hold_bars'] for t in trades) / n if n else 0

# 资金曲线(累计净利, 元/手)
equity = []
cum = 0.0
for t in trades:
    cum += t['pnl']
    equity.append({'time': t['eq_time'], 'value': round(cum, 0)})

summary = {
    'n': n, 'win_rate': round(win_rate * 100, 1),
    'payoff': round(payoff, 2) if payoff != float('inf') else '全胜',
    'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else '全胜',
    'net_pts': round(net_pts, 1), 'net_pnl': round(net_pnl, 0),
    'avg_win': round(avg_win, 1), 'avg_loss': round(avg_loss, 1),
    'max_win': round(max_win, 1), 'max_loss': round(max_loss, 1),
    'theo_net_pts': round(theo_net_pts, 1),
    'delay_cost': round(theo_net_pts - net_pts, 1),
    'avg_hold_bars': round(avg_hold, 1),
    'skipped': skipped_open,
}
max_loss_txt = f"最大单笔亏 {summary['max_loss']} 点" if losses else "无亏损单"
print(f"交易次数: {n}  胜率: {summary['win_rate']}%  盈亏比: {summary['payoff']}  "
      f"净利: {summary['net_pts']}点 / {summary['net_pnl']:.0f}元/手  (理论极值 {summary['theo_net_pts']}点)")
if skipped_open:
    print(f"注: 最后 {skipped_open} 个up笔顶分型未确认, 未计入回测")

# ============ 4. 准备图表 JSON ============
kline_data, vol_data = [], []
for _, row in df.iterrows():
    ts = int(row['datetime'].timestamp())
    kline_data.append({'time': ts, 'open': round(row['open'], 1),
                       'high': round(row['high'], 1), 'low': round(row['low'], 1),
                       'close': round(row['close'], 1)})
    vol_data.append({'time': ts, 'value': int(row['volume'])})

bi_line_points = []
if bi_list:
    s0 = merged['low_idx'].iloc[bi_list[0]['start_idx']]
    bi_line_points.append({'time': int(df['datetime'].iloc[s0].timestamp()),
                           'value': bi_list[0]['start_price']})
    for bi in bi_list:
        if bi['direction'] == 'up':
            e = merged['high_idx'].iloc[bi['end_idx']]
        else:
            e = merged['low_idx'].iloc[bi['end_idx']]
        bi_line_points.append({'time': int(df['datetime'].iloc[e].timestamp()),
                               'value': bi['end_price']})

markers = []
for bi in bi_list:
    if bi['direction'] == 'up':
        s = merged['low_idx'].iloc[bi['start_idx']]
        markers.append({'time': int(df['datetime'].iloc[s].timestamp()),
                        'position': 'belowBar', 'color': '#ef5350',
                        'shape': 'arrowUp', 'text': '买'})
    else:
        s = merged['high_idx'].iloc[bi['start_idx']]
        markers.append({'time': int(df['datetime'].iloc[s].timestamp()),
                        'position': 'aboveBar', 'color': '#26a69a',
                        'shape': 'arrowDown', 'text': '卖'})

# ============ 5. 生成 HTML ============
html = f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>甲醇MA0 15分钟 - 缠论笔回测</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#131722; font-family:-apple-system,"Microsoft YaHei",sans-serif; color:#d1d4dc; overflow:hidden; }}
  #header {{ padding:8px 14px; display:flex; justify-content:space-between; align-items:center; height:46px; }}
  #header h1 {{ font-size:15px; font-weight:600; white-space:nowrap; }}
  #stats {{ font-size:12px; display:flex; align-items:center; gap:16px; }}
  .chip {{ display:flex; flex-direction:column; align-items:center; line-height:1.25; }}
  .chip .k {{ color:#787b86; font-size:11px; }}
  .chip .v {{ font-size:14px; font-weight:700; }}
  .pos {{ color:#ef5350; }} .neg {{ color:#26a69a; }} .neu {{ color:#f0b90b; }}
  #btn {{ background:#2a2e39; color:#d1d4dc; border:1px solid #363a45; border-radius:5px;
          padding:6px 12px; font-size:12px; cursor:pointer; }}
  #btn:hover {{ background:#363a45; }}
  #chart {{ width:100%; height:calc(100vh - 46px); }}
  /* 明细抽屉 */
  #drawer {{ position:fixed; top:0; right:0; width:460px; height:100vh; background:#1e222d;
            box-shadow:-4px 0 24px rgba(0,0,0,.5); transform:translateX(105%);
            transition:transform .25s ease; z-index:50; display:flex; flex-direction:column; }}
  #drawer.open {{ transform:translateX(0); }}
  #drawerHead {{ display:flex; justify-content:space-between; align-items:center; padding:12px 16px; border-bottom:1px solid #2a2e39; }}
  #drawerHead h2 {{ font-size:14px; }}
  #closeX {{ cursor:pointer; color:#787b86; font-size:20px; }}
  #eqchart {{ width:100%; height:170px; }}
  #tblWrap {{ overflow:auto; flex:1; }}
  table {{ width:100%; border-collapse:collapse; font-size:11.5px; }}
  th,td {{ padding:6px 6px; text-align:right; border-bottom:1px solid #2a2e39; white-space:nowrap; }}
  th {{ position:sticky; top:0; background:#2a2e39; color:#9ca3af; font-weight:600; }}
  td.l,th.l {{ text-align:left; }}
  .w {{ color:#ef5350; }} .l {{ color:#26a69a; }}
  #note {{ padding:8px 16px; font-size:11px; color:#787b86; border-top:1px solid #2a2e39; line-height:1.5; }}
</style>
</head>
<body>
<div id="header">
  <h1>甲醇 MA0 · 15分钟 · 底买顶卖回测（只做多）</h1>
  <div id="stats">
    <div class="chip"><span class="k">交易次数</span><span class="v neu">{summary['n']}</span></div>
    <div class="chip"><span class="k">胜率</span><span class="v pos">{summary['win_rate']}%</span></div>
    <div class="chip"><span class="k">盈亏比</span><span class="v neu">{summary['payoff']}</span></div>
    <div class="chip"><span class="k">盈利因子</span><span class="v neu">{summary['profit_factor']}</span></div>
    <div class="chip"><span class="k">净利(点)</span><span class="v {'pos' if net_pts>=0 else 'neg'}">{summary['net_pts']}</span></div>
    <div class="chip"><span class="k">净利(元/手)</span><span class="v {'pos' if net_pnl>=0 else 'neg'}">{int(summary['net_pnl'])}</span></div>
    <button id="btn">交易明细 / 资金曲线</button>
  </div>
</div>
<div id="chart"></div>

<div id="drawer">
  <div id="drawerHead"><h2>交易明细与资金曲线</h2><span id="closeX">✕</span></div>
  <div id="eqchart"></div>
  <div id="tblWrap">
    <table>
      <thead><tr>
        <th class="l">#</th><th class="l">买入时间</th><th>买价</th>
        <th class="l">卖出时间</th><th>卖价</th><th>持仓根</th>
        <th>点数</th><th>盈亏元</th><th>理论点</th>
      </tr></thead>
      <tbody id="tblBody"></tbody>
    </table>
  </div>
  <div id="note">
    成交价=分型右侧确认K线收盘价（无未来函数）；理论点=最低点买/最高点卖的上帝视角，
    两者差 <b style="color:#f0b90b">{summary['delay_cost']}</b> 点即“信号延迟成本”。<br>
    每点 {MULTIPLIER} 元/手；当前未计手续费/滑点（COST_PER_SIDE={COST_PER_SIDE}）。
    平均持仓 {summary['avg_hold_bars']} 根15分K；最大单笔盈 {summary['max_win']} 点，{max_loss_txt}。<br>
    <b style="color:#ef5350">口径提示：</b>当前仅统计“已走完上涨笔”的理想底买顶卖，
    未含“底分型后失败、应止损”的单子，故胜率偏高；加入止损、且每个底分型信号都进场后才是真实绩效。
  </div>
</div>

<script>
const klineData = {json.dumps(kline_data)};
const volData = {json.dumps(vol_data)};
const biLinePoints = {json.dumps(bi_line_points)};
const markers = {json.dumps(markers)};
const trades = {json.dumps(trades)};
const equity = {json.dumps(equity)};

const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
  layout: {{ background:{{type:'solid',color:'#131722'}}, textColor:'#d1d4dc', fontSize:11 }},
  grid: {{ vertLines:{{color:'rgba(42,46,57,.6)'}}, horzLines:{{color:'rgba(42,46,57,.6)'}} }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  rightPriceScale: {{ borderColor:'rgba(197,203,206,.2)' }},
  timeScale: {{ borderColor:'rgba(197,203,206,.2)', timeVisible:true, secondsVisible:false }},
}});
const candle = chart.addCandlestickSeries({{
  upColor:'#ef5350', downColor:'#26a69a', borderUpColor:'#ef5350', borderDownColor:'#26a69a',
  wickUpColor:'#ef5350', wickDownColor:'#26a69a' }});
candle.setData(klineData);
const vol = chart.addHistogramSeries({{ priceFormat:{{type:'volume'}}, priceScaleId:'vol' }});
chart.priceScale('vol').applyOptions({{ scaleMargins:{{top:0.84,bottom:0}} }});
vol.setData(volData.map(d=>({{time:d.time,value:d.value,color:'rgba(38,166,154,.35)'}})));
const biLine = chart.addLineSeries({{ color:'#f0b90b', lineWidth:1, priceLineVisible:false, lastValueVisible:false }});
biLine.setData(biLinePoints);
candle.setMarkers(markers);
chart.timeScale().fitContent();

// 资金曲线小图
const eqChart = LightweightCharts.createChart(document.getElementById('eqchart'), {{
  layout:{{background:{{type:'solid',color:'#1e222d'}},textColor:'#9ca3af',fontSize:10}},
  grid:{{vertLines:{{color:'rgba(42,46,57,.4)'}},horzLines:{{color:'rgba(42,46,57,.4)'}}}},
  rightPriceScale:{{borderColor:'rgba(197,203,206,.15)'}},
  timeScale:{{timeVisible:true,secondsVisible:false,borderVisible:false}},
  crosshair:{{mode:0}},
}});
const eqSeries = eqChart.addAreaSeries({{ lineColor:'#f0b90b', topColor:'rgba(240,185,11,.35)',
  bottomColor:'rgba(240,185,11,.03)', lineWidth:2, priceLineVisible:false }});
eqSeries.setData(equity);
eqChart.timeScale().fitContent();

// 明细表
const tb = document.getElementById('tblBody');
trades.forEach(function(t,i){{
  var c = t.win ? 'w' : 'l';
  var sg = t.pts > 0 ? '+' : '';
  var st = t.theo_pts > 0 ? '+' : '';
  var row = '<tr><td class="l">'+(i+1)+'</td><td class="l">'+t.buy_time.slice(5,16)+'</td><td>'+t.buy+'</td>'
    +'<td class="l">'+t.sell_time.slice(5,16)+'</td><td>'+t.sell+'</td><td>'+t.hold_bars+'</td>'
    +'<td class="'+c+'">'+sg+t.pts+'</td><td class="'+c+'">'+sg+t.pnl+'</td>'
    +'<td style="color:#787b86">'+st+t.theo_pts+'</td></tr>';
  tb.insertAdjacentHTML('beforeend', row);
}});

const drawer = document.getElementById('drawer');
document.getElementById('btn').onclick = ()=>{{
  drawer.classList.toggle('open');
  setTimeout(()=>{{ eqChart.applyOptions({{width:460}}); eqChart.timeScale().fitContent(); }}, 280);
}};
document.getElementById('closeX').onclick = ()=>drawer.classList.remove('open');

new ResizeObserver(en=>chart.applyOptions({{width:en[0].contentRect.width}})).observe(document.getElementById('chart'));
window.addEventListener('resize',()=>chart.applyOptions({{width:window.innerWidth}}));
</script>
</body>
</html>'''

output = os.path.join(_HERE, 'ma_15min_backtest.html')
with open(output, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"图表: {output}")
