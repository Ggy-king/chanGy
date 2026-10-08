# -*- coding: utf-8 -*-
"""chanlun/pattern.py 形态评估器单元测试。
运行：.venv/bin/python tests/test_pattern.py（覆盖设计文档 §7.1 要求的全部分支）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
from chanlun.pattern import evaluate


def mk(i, d, si, ei, sp, ep):
    return {'start_idx': si, 'end_idx': ei, 'direction': d,
            'start_price': sp, 'end_price': ep,
            'start_datetime': f'2026-01-01 09:{30+i:02d}:00',
            'end_datetime': f'2026-01-01 09:{31+i:02d}:00'}


def merged_flat(n, high=100.0, low=90.0):
    """n 根合成合并K线：默认区间 [90,100]，与笔端点价无关，仅供 R3 分型位置检查。"""
    return pd.DataFrame({'high': [high] * n, 'low': [low] * n})


def merged_for_a_long(n=12, fx_k=5, p1=100.0):
    """底分型位于 fx_k，其三根 high 全部 < p1；其余K线区间也 < p1。
    low 保持在常见背驰点(95)上方——避免误触"现价破背驰点=假确认"实时判定。"""
    return pd.DataFrame({'high': [p1 - 3.0] * n, 'low': [p1 - 4.0] * n})


PASS = 0


def check(name, cond):
    global PASS
    assert cond, f'FAIL: {name}'
    PASS += 1
    print(f'  ok - {name}')


# ===== 1. 形态A 做多：正常触发（一笔突破，图1）=====
print('[1] A-long 正常触发')
merged = merged_for_a_long()
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),      # 1→2
       mk(1, 'down', 2, 5, 110.0, 95.0),     # 2→3 (95<100, 分型全在1下方)
       mk(2, 'up', 5, 8, 95.0, 110.5)]       # 3→4 破2
rep = set()
r = evaluate(bis, merged, rep)
check('触发 VREV_LONG', r['long']['state'] == 'cand_a' and len(r['long']['fired']) == 1)
ev = r['long']['fired'][0]
check('触发价=4点端点', ev['price'] == 110.5 and ev['p1'] == 100.0 and ev['p2'] == 110.0 and ev['p3'] == 95.0)
check('bi_span=1', ev['bi_span'] == 1)
r2 = evaluate(bis, merged, rep)
check('指纹去重：第二次重发但 first=False（卡片更新由服务端去重）', r2['long']['state'] == 'cand_a'
      and len(r2['long']['fired']) == 1 and r2['long']['fired'][0]['first'] == False)

# ===== 2. 多笔触发（图2）=====
print('[2] A-long 多笔触发')
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 7, 95.0, 105.0),       # 未破2，继续等
       mk(3, 'down', 7, 9, 105.0, 97.0),     # 更高低点（>=3，不重建）
       mk(4, 'up', 9, 11, 97.0, 111.0)]      # 最终突破
r = evaluate(bis, merged, set())
check('跨3笔后触发', r['long']['state'] == 'cand_a' and r['long']['fired'][0]['bi_span'] == 3
      and r['long']['fired'][0]['price'] == 111.0)

# ===== 3. 新低重建（§3.4）=====
print('[3] A-long 新低重建')
merged_low = pd.DataFrame({'high': [90.0] * 12, 'low': [87.0] * 12})  # 分型在两个 p1 (100/95) 下方
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 7, 95.0, 105.0),
       mk(3, 'down', 7, 9, 105.0, 93.0)]     # 新低 93 < 95 → 以最新三笔重建
r = evaluate(bis, merged_low, set())
check('重建后 WATCH（新结构 p3=87：进行中笔端点实时延伸到现价）', r['long']['state'] == 'watch_a'
      and r['long']['struct']['p3'] == 87.0 and r['long']['struct']['p1'] == 95.0)

# ===== 4. 新低重建后 R3 不满足 → IDLE =====
print('[4] A-long 重建后 R3 不满足 → IDLE')
# 注：真新低后重建的 p1'=前次反弹起点（更高低点），p3'=新低 < p1' 恒成立（R2' 自动满足），
#     重建后可能失败的只有 R3（新底分型未完整落在新 1 下方）。
merged_mid = pd.DataFrame({'high': [97.0] * 16, 'low': [94.0] * 16})  # 分型高点 97 不在新 1(=96) 下方
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 4, 110.0, 95.0),      # 原 3=95
       mk(2, 'up', 4, 6, 95.0, 101.0),
       mk(3, 'down', 6, 8, 101.0, 96.0),      # 更高低点
       mk(4, 'up', 8, 10, 96.0, 102.0),
       mk(5, 'down', 10, 14, 102.0, 94.0)]    # 新低 94 → 重建: p1'=96, p3'=94
r = evaluate(bis, merged_mid, set())
check('重建后 R3 不满足 → IDLE', r['long']['state'] == 'idle')

# ===== 5. R3 分型不在 1 下方 → 不进 WATCH =====
print('[5] A-long R3 不满足')
merged_hi = pd.DataFrame({'high': [105.0] * 12, 'low': [95.0] * 12})  # 分型三根high=105 > p1=100
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0)]
r = evaluate(bis, merged_hi, set())
check('R3 拒绝 → IDLE', r['long']['state'] == 'idle')

# ===== 6. R2 不满足（3 未破 1）=====
print('[6] A-long R2 不满足')
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 102.0)]    # 102 > 100
merged6 = pd.DataFrame({'high': [105.0] * 12, 'low': [103.0] * 12})  # 后续K线反弹在102上方，笔不延伸
r = evaluate(bis, merged6, set())
check('R2 拒绝 → IDLE', r['long']['state'] == 'idle')

# ===== 7. 形态C 三买：候选 → 确认 =====
print('[7] C-long 候选→确认')
# K线贴着 143-145（回测夹具须与实时语义一致：价格没回到 121 附近，回调笔不延伸、不提前成笔）
merged = pd.DataFrame({'high': [145.0] * 20, 'low': [143.0] * 20})
bis = [mk(0, 'down', 0, 3, 120.0, 100.0),    # 5→6 跌20
       mk(1, 'up', 3, 6, 100.0, 145.0)]      # 6→7 涨45 > 2×20 ✓ 且 145>120 ✓
rep = set()
r = evaluate(bis, merged, rep)
check('3BUY_CAND', r['long']['state'] == 'cand_b' and r['long']['fired'][0]['pattern'] == '3BUY_CAND')
check('ratio=2.25', abs(r['long']['fired'][0]['ratio'] - 2.25) < 1e-6)
bis.append(mk(2, 'down', 6, 9, 145.0, 121.0))  # 回调 121 > 120 → 确认
r = evaluate(bis, merged, rep)
check('3BUY_TRIG', r['long']['state'] == 'trig_b' and r['long']['fired'][0]['pattern'] == '3BUY_TRIG')

# ===== 8. 形态C 幅度过滤拒绝 =====
print('[8] C-long 幅度过滤拒绝')
bis = [mk(0, 'down', 0, 3, 120.0, 100.0),
       mk(1, 'up', 3, 6, 100.0, 139.0)]      # 涨39 < 2×20=40
merged8 = pd.DataFrame({'high': [138.0] * 20, 'low': [136.0] * 20})  # K线低于139：笔不延伸
r = evaluate(bis, merged8, set())
check('幅度不足 → IDLE', r['long']['state'] == 'idle')

# ===== 9. 形态C 候选作废 =====
print('[9] C-long 作废')
bis = [mk(0, 'down', 0, 3, 120.0, 100.0),
       mk(1, 'up', 3, 6, 100.0, 145.0),
       mk(2, 'down', 6, 9, 145.0, 118.0)]    # 回调 118 <= 120
r = evaluate(bis, merged, set())
check('跌破5 → 作废 IDLE', r['long']['state'] == 'idle')

# ===== 10. 做空镜像：A-short 触发 =====
print('[10] A-short 镜像触发')
merged_s = pd.DataFrame({'high': [103.0] * 12, 'low': [101.0] * 12})  # 顶分型三根 low 全部 > p1=100
bis = [mk(0, 'down', 0, 2, 100.0, 90.0),     # 1'(顶100)→2'(低90)
       mk(1, 'up', 2, 5, 90.0, 105.0),       # 2'→3'(新高105>100)
       mk(2, 'down', 5, 8, 105.0, 89.5)]     # 跌破 2'=90
r = evaluate(bis, merged_s, set())
check('VREV_SHORT', r['short']['state'] == 'cand_a' and r['short']['fired'][0]['pattern'] == 'VREV_SHORT')
check('short 触发价', r['short']['fired'][0]['price'] == 89.5 and r['short']['fired'][0]['bi_span'] == 1)

# ===== 11. 做空镜像：R3' 拒绝 =====
print('[11] A-short R3\' 拒绝')
merged_bad = pd.DataFrame({'high': [106.0] * 12, 'low': [99.0] * 12})  # 分型 low=99 < p1=100
bis = [mk(0, 'down', 0, 2, 100.0, 90.0),
       mk(1, 'up', 2, 5, 90.0, 105.0)]
r = evaluate(bis, merged_bad, set())
check("R3' 拒绝 → IDLE", r['short']['state'] == 'idle')

# ===== 12. 做空镜像：三卖候选→确认 =====
print('[12] C-short 镜像')
merged12 = pd.DataFrame({'high': [78.0] * 20, 'low': [76.0] * 20})   # K线低于79：反抽笔不延伸
bis = [mk(0, 'up', 0, 3, 80.0, 100.0),       # 5(低80)→6(高100) 涨20
       mk(1, 'down', 3, 6, 100.0, 55.0)]     # 6→7 跌45 > 40 ✓ 且 55<80 ✓
rep = set()
r = evaluate(bis, merged12, rep)
check('3SELL_CAND', r['short']['state'] == 'cand_b' and r['short']['fired'][0]['pattern'] == '3SELL_CAND')
bis.append(mk(2, 'up', 6, 9, 55.0, 79.0))    # 反抽 79 < 80 → 确认
r = evaluate(bis, merged12, rep)
check('3SELL_TRIG', r['short']['state'] == 'trig_b' and r['short']['fired'][0]['pattern'] == '3SELL_TRIG')

# ===== 13. 分型贴边 → 保守跳过 =====
print('[13] 底分型贴边')
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 1, 110.0, 95.0)]     # end_idx=1 → k-1=0 ok, k+1=2 ok... 换成 k=0 场景：
bis = [mk(0, 'up', 0, 0, 100.0, 110.0),      # end_idx=0 → k-1=-1 贴边
       mk(1, 'down', 0, 0, 110.0, 95.0)]
merged_edge = merged_for_a_long(5)
r = evaluate(bis, merged_edge, set())
check('贴边 → IDLE', r['long']['state'] == 'idle')

# ===== 14. 多空互不干扰 =====
print('[14] 多空并行')
merged_l = merged_for_a_long()
bis_l = [mk(0, 'up', 0, 2, 100.0, 110.0), mk(1, 'down', 2, 5, 110.0, 95.0)]
r = evaluate(bis_l, merged_l, set())
check('多头 WATCH、空头 IDLE', r['long']['state'] == 'watch_a' and r['short']['state'] == 'idle')



# ===== 15. A类确认：突破(候选)后第一根向下回调笔成立 =====
print('[15] A-long 候选→确认')
merged = merged_for_a_long()
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5)]        # 突破 → 候选
rep = set()
r = evaluate(bis, merged, rep)
check('突破 → A类候选', r['long']['state'] == 'cand_a' and r['long']['fired'][0]['pattern'] == 'VREV_LONG')
bis.append(mk(3, 'down', 8, 11, 110.5, 103.0))  # 回调笔成立 → 确认
r = evaluate(bis, merged, rep)
check('回调笔确立 → A类确认', r['long']['state'] == 'conf_a' and r['long']['fired'][-1]['pattern'] == 'VREV_CONF_LONG')
check('确认事件价格=回调笔端点', r['long']['fired'][-1]['price'] == 103.0)
r2 = evaluate(bis, merged, rep)
check('确认指纹去重（first=False）', r2['long']['state'] == 'conf_a'
      and r2['long']['fired'][-1]['pattern'] == 'VREV_CONF_LONG' and r2['long']['fired'][-1]['first'] == False)

# ===== 16. 深回调同时形成新结构：确认不吞掉，状态回观察 =====
print('[16] 深回调：确认+新结构并存')
merged_low = pd.DataFrame({'high': [90.0] * 16, 'low': [87.0] * 16})
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5),        # 候选
       mk(3, 'down', 8, 12, 110.5, 92.0)]     # 深回调跌破旧3 → 确认 + 新结构重建
rep = set()
r = evaluate(bis, merged_low, rep)
pats = [e['pattern'] for e in r['long']['fired']]
check('跌破背驰点=假确认，不发确认事件', 'VREV_CONF_LONG' not in pats)
check('状态回观察(新结构,端点实时延伸)', r['long']['state'] == 'watch_a' and r['long']['struct']['p3'] == 87.0)

# ===== 17. 确认的生命周期：连续3根K线创新高 → 确认结束（3根新高制，可逆） =====
print('[17] A确认结束（连续3根K线创新高）')
mh = [97.0] * 11 + [104.0, 106.0] + [110.0, 114.0, 118.0, 119.0]   # 11=回调低点分型, 12-14依次创新高
merged17 = pd.DataFrame({'high': mh, 'low': [m - 3 for m in mh]})
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5),           # 候选（突破）
       mk(3, 'down', 8, 11, 110.5, 103.0)]       # 回调笔确立(end_idx=11) → 确认
rep = set()
r = evaluate(bis, merged17[:13], rep)             # 分型后不足3根完结K线，谈不上离开
check('回调笔确立且未离开 → conf_a', r['long']['state'] == 'conf_a')
bis.append(mk(4, 'up', 11, 14, 103.0, 112.0))     # 顺势向上（后续笔确认）
r = evaluate(bis, merged17, rep)                  # 连续3根创新高出现
check('连续3根K线创新高 → idle', r['long']['state'] == 'idle')

# ===== 18. 确认可逆：离开后价格跌回确认区域 → 恢复显示 =====
print('[18] 确认可逆（跌回来恢复显示）')
mh2 = [97.0] * 11 + [104.0, 106.0] + [110.0, 114.0, 118.0] + [104.0]   # 3根新高离开后跌回区间内
merged18 = pd.DataFrame({'high': mh2, 'low': [m - 3 for m in mh2]})
r = evaluate(bis, merged18, set())
check('跌回确认区域 → 恢复 conf_a', r['long']['state'] == 'conf_a')

# ===== 18. 确认后回调跌破背驰点 → 假确认，演化为新结构观察 =====
print('[18] 确认后回调破背驰点')
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5),
       mk(3, 'down', 8, 12, 110.5, 92.0)]     # 回调笔直接破背驰点(95)
r = evaluate(bis, merged_low, set())
pats = [e['pattern'] for e in r['long']['fired']]
check('破背驰点=假确认，无确认事件', 'VREV_CONF_LONG' not in pats)
check('演化为新结构观察', r['long']['state'] == 'watch_a')

# ===== 19. 僵尸确认回归：确认后现价实时破背驰点 → 确认立即作废 =====
print('[19] A确认后现价破背驰点（实时作废，不等你笔走完）')
merged19 = pd.DataFrame({'high': [97.0] * 12, 'low': [94.0] * 12})   # 最新K线 low 94 < 背驰点 95
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5),
       mk(3, 'down', 8, 11, 110.5, 103.0)]       # 回调确立，确认本应成立
r = evaluate(bis, merged19, set())
check('现价跌破背驰点 → 确认作废(不挂僵尸)', r['long']['state'] != 'conf_a')

# ===== 20. 空头镜像：确认后现价涨破背驰点 → 作废（PG2611 9-18 僵尸确认事故）=====
print('[20] A空头确认后现价涨破背驰点')
mh_s = [112.0] * 11 + [110.0, 110.0, 110.0]      # 全部K线未破背驰点 110.5（确认合法成立）
merged_s = pd.DataFrame({'high': mh_s, 'low': [m - 1 for m in mh_s]})
bis = [mk(0, 'down', 0, 2, 110.0, 95.0),         # 1'=110 2'=95
       mk(1, 'up', 2, 5, 95.0, 110.5),           # 3'=110.5 (新高)
       mk(2, 'down', 5, 8, 110.5, 94.5),         # 跌破2' → 候选
       mk(3, 'up', 8, 11, 94.5, 100.0)]          # 反抽确立 → 确认
r = evaluate(bis, merged_s, set())
check('反抽确立未破背驰点 → conf_a', r['short']['state'] == 'conf_a')
bis.append(mk(4, 'up', 11, 14, 100.0, 111.0))    # 上涨笔(完成)涨破背驰点
r = evaluate(bis, merged_s, set())
check('完成笔涨破背驰点 → 确认作废', r['short']['state'] != 'conf_a')

# ===== 23. 进行中笔：B类候选实时触发（成笔瞬间就报，不等分型确认） =====
print('[23] 进行中笔实时触发 B候选（鸡蛋9:00案例回归）')
mh23 = [129.0] * 20
ml23 = [99.0] * 8 + [95.0, 90.0, 85.0, 80.0, 75.0, 65.0] + [66.0] * 6
merged23 = pd.DataFrame({'high': mh23, 'low': ml23})
bis = [mk(0, 'down', 0, 3, 120.0, 100.0),
       mk(1, 'up', 3, 8, 100.0, 130.0)]           # 5=100 6=130，之后进行中向下
rep23 = set()
r = evaluate(bis, merged23, rep23)
check('进行中笔破5+幅度够 → 实时候选', r['short']['state'] == 'cand_b'
      and r['short']['fired'][0]['pattern'] == '3SELL_CAND' and r['short']['fired'][0]['price'] == 65.0)
r2 = evaluate(bis, merged23, rep23)
check('进行中候选指纹去重(重发 first=False)', len(r2['short']['fired']) == 1
      and r2['short']['fired'][0]['first'] == False)
ml23b = list(ml23); ml23b[13] = 63.0             # 端点继续延伸(浮动)
merged23b = pd.DataFrame({'high': mh23, 'low': ml23b})
r3 = evaluate(bis, merged23b, rep23)
check('端点延伸后同一结构不重报(first=False, 端点更新)', r3['short']['state'] == 'cand_b'
      and r3['short']['fired'][0]['first'] == False and r3['short']['fired'][0]['price'] == 63.0)

# ===== 24. 进行中笔：A类候选实时触发（突破关键价位2一瞬间） =====
print('[24] 进行中笔实时触发 A候选')
mh24 = [97.0] * 7 + [104.0, 107.0, 109.0, 111.0, 111.0, 111.0]
merged24 = pd.DataFrame({'high': mh24, 'low': [m - 1 for m in mh24]})   # low 不破 95：向下笔不延伸
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0)]          # 结构1-2-3成立(观察)，之后进行中向上
r = evaluate(bis, merged24, set())
check('进行中笔突破2 → 实时A候选', r['long']['state'] == 'cand_a'
      and r['long']['fired'][0]['pattern'] == 'VREV_LONG' and r['long']['fired'][0]['price'] == 111.0)

# ===== 25. 事件流"留底"：状态终结不吞事件（棉花10-08确认未推送事故回归） =====
print('[25] 候选+确认触发后价格离开确认区域 → 状态idle但事件必须保留')
mh25 = [97.0] * 11 + [104.0, 106.0] + [110.0, 114.0, 118.0, 122.0]   # 末段连续3根创新高=离开确认区域
merged25 = pd.DataFrame({'high': mh25, 'low': [m - 3 for m in mh25]})
bis = [mk(0, 'up', 0, 2, 100.0, 110.0),
       mk(1, 'down', 2, 5, 110.0, 95.0),
       mk(2, 'up', 5, 8, 95.0, 110.5),              # 候选触发
       mk(3, 'down', 8, 11, 110.5, 103.0)]          # 回调确立 → 确认触发
r = evaluate(bis, merged25, set())
pats = [e['pattern'] for e in r['long']['fired']]
check('状态=idle(窗口已关)', r['long']['state'] == 'idle')
check('但候选+确认事件都在 fired 里(不被吞)', 'VREV_LONG' in pats and 'VREV_CONF_LONG' in pats)
print(f'\n全部 {PASS} 项断言通过')

# ===== 21. B触发生命周期：连续3根K线创新高 → 触发结束（纯K线延伸，不加笔） =====
print('[21] B触发结束（连续3根K线创新高离开回踩区域）')
mh3 = [122, 116, 110, 100, 115, 130, 145, 143, 135, 122, 130, 138, 146, 154]
merged21 = pd.DataFrame({'high': [float(x) for x in mh3], 'low': [float(x) - 3 for x in mh3]})
bis = [mk(0, 'down', 0, 3, 120.0, 100.0),
       mk(1, 'up', 3, 6, 100.0, 145.0),                    # 突破(候选)
       mk(2, 'down', 6, 9, 145.0, 121.0)]                  # 回调笔确立(121>120) → 触发
rep = set()
r = evaluate(bis, merged21[:11], rep)                       # 回踩分型后不足3根完结K线
check('回踩确立未离开 → trig_b', r['long']['state'] == 'trig_b' and r['long']['fired'][0]['pattern'] == '3BUY_TRIG')
r = evaluate(bis, merged21, rep)
check('价格向上离开回踩区域 → idle', r['long']['state'] == 'idle')
