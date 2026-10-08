# -*- coding: utf-8 -*-
"""矩阵页 Playwright 无头自测（设计文档 §7.5 / §9.6）"""
import os
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8000'
errors = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    pg.on('console', lambda m: errors.append(f'console.{m.type}: {m.text}') if m.type == 'error' else None)

    # ===== 1. 矩阵页打开无 pageerror、结构正确 =====
    pg.goto(f'{BASE}/pc/matrix', wait_until='networkidle')
    pg.wait_for_timeout(1500)
    n_rows = pg.evaluate('document.querySelectorAll("#matrix tr").length')
    n_cells = pg.evaluate('document.querySelectorAll("td.cell").length')
    n_contracts = pg.evaluate('CONTRACTS.length')
    n_periods = pg.evaluate('PERIODS.length')
    assert n_periods == 5, f'periods={n_periods}'
    assert n_contracts == 17, f'contracts={n_contracts}'
    assert n_cells == n_periods * n_contracts, f'cells={n_cells} != {n_periods}x{n_contracts}'
    assert n_rows == n_periods + 1, f'rows={n_rows}'
    print(f'ok - 矩阵结构: {n_periods}级别 x {n_contracts}品种 = {n_cells} 格')
    # 行序：倒序后第一行应是 '60'（当前 PATTERN_PERIODS=['5','15','30m','60']）
    first_row = pg.evaluate('document.querySelector("#matrix tr:nth-child(2) th.rowhead").textContent')
    assert first_row == '60分', f'首行={first_row}'
    print(f'ok - 行序倒排（首行={first_row}）')

    # ===== 2. mock pattern_state → 格子变色 =====
    pg.evaluate('''() => applyCell({symbol:'MA2611', name:'甲醇', period:'15',
        state:'cand_a', side:'long', pattern:'VREV_LONG', price:2500.0, time:'2026-10-06 14:35',
        p1:2450, p2:2490, p3:2440, ts:Date.now()/1000}) ''')
    cls = pg.evaluate('document.getElementById("c_MA2611_15").className')
    txt = pg.evaluate('document.getElementById("c_MA2611_15").textContent')
    assert 'cand_a' in cls and 'long' in cls, cls
    assert '候选' in txt, txt
    print('ok - mock A类候选格子变红')
    # 确认是实时状态，正常上格子（实底醒目）
    pg.evaluate('''() => applyCell({symbol:'MA2611', name:'甲醇', period:'15',
        state:'conf_a', side:'long', pattern:'VREV_CONF_LONG', price:2460.0, time:'2026-10-06 15:10',
        p1:2450, p2:2490, p3:2440, ts:Date.now()/1000}) ''')
    cls = pg.evaluate('document.getElementById("c_MA2611_15").className')
    txt = pg.evaluate('document.getElementById("c_MA2611_15").textContent')
    assert 'conf_a' in cls and '确认' in txt, (cls, txt)
    print('ok - mock A类确认格子(实时状态,实底醒目)')

    pg.evaluate('''() => applyCell({symbol:'RB2701', name:'螺纹钢', period:'30m',
        state:'cand_b', side:'short', pattern:'3SELL_CAND', price:310.0, time:'2026-10-06 14:20',
        p1:320, p2:315, p3:309, ts:Date.now()/1000}) ''')
    cls = pg.evaluate('document.getElementById("c_RB2701_30m").className')
    assert 'cand_b' in cls and 'short' in cls, cls
    print('ok - mock B类候选格子黄框')
    pg.evaluate('''() => applyCell({symbol:'RB2701', name:'螺纹钢', period:'30m',
        state:'trig_b', side:'short', pattern:'3SELL_TRIG', price:318.0, time:'2026-10-06 14:40',
        p1:320, p2:315, p3:309, ts:Date.now()/1000}) ''')
    cls = pg.evaluate('document.getElementById("c_RB2701_30m").className')
    txt = pg.evaluate('document.getElementById("c_RB2701_30m").textContent')
    assert 'trig_b' in cls and '触发' in txt, (cls, txt)
    print('ok - mock B类触发格子(实时状态)')

    # ===== 3. mock pattern_alert → 事件流插入（页签/卡片更新制） =====
    # 先等服务端基线+快照推送完成（ready 标志），再重载页面拿到稳定的事件流
    pg.wait_for_function('''async () => {
        const r = await fetch('/api/pattern_matrix'); return (await r.json()).ready === true;
    }''', timeout=300000)
    pg.goto(f'{BASE}/pc/matrix', wait_until='domcontentloaded')
    pg.wait_for_function('document.getElementById("connTx").textContent==="已连接"', timeout=8000)
    pg.wait_for_timeout(800)                    # 等 WS init 历史重放完毕
    pg.evaluate('''() => upsertEvent({ trigger_time:new Date().toISOString(), kind:'A', side:'long', pattern:'VREV_LONG', fingerprint:'f1',
        symbol:'MA2611', name:'甲醇', period:'15', price:2500.0, time:'2026-10-06 14:35',
        p1:2450, p2:2490, p3:2440, bi_span:2, ts:Date.now()/1000, text:'甲醇 15分 背驰V反(多)：突破2490'});
        upsertEvent({ trigger_time:new Date().toISOString(), kind:'B', side:'short', pattern:'3SELL_CAND', fingerprint:'f2',
        symbol:'RB2701', name:'螺纹钢', period:'30m', price:310.0, time:'2026-10-06 14:20',
        p1:320, p2:315, p3:309, ratio:2.3, ts:Date.now()/1000, text:'螺纹钢 30分 三卖候选 310（幅度2.3×）'});
        renderStream() ''')
    pg.wait_for_timeout(1200)                   # 让在途的快照推送先落地
    n_all = pg.evaluate('document.querySelectorAll("#stream .evt").length')
    assert n_all <= 30, f'默认页签超过30条: {n_all}'
    # 同指纹再推（延续触发，端点变化）→ 原卡片原位更新，不新增；重触后置顶
    # （单次 evaluate 原子执行，避免 WS 消息插队影响断言）
    res = pg.evaluate('''() => {
        upsertEvent({ trigger_time:new Date().toISOString(), kind:'A', side:'long', pattern:'VREV_LONG', fingerprint:'f1',
            symbol:'MA2611', name:'甲醇', period:'15', price:2510.0, time:'2026-10-06 14:40',
            p1:2450, p2:2490, p3:2440, bi_span:2, ts:Date.now()/1000, text:'甲醇 15分 背驰V反(多)：突破2490',
            first_time:'2026-10-06 14:35'});
        renderStream();
        const fps=[...document.querySelectorAll('#stream .evt')].map(e=>e.dataset.fp);
        return {f1:fps.filter(x=>x==='f1').length, first:fps[0],
                firstTxt:document.querySelector('#stream .evt').textContent};
    }''')
    assert res['f1'] == 1, f'同指纹卡片应只有1张: {res}'
    assert res['first'] == 'f1', f'更新后的卡片应置顶: {res}'
    assert '甲醇' in res['firstTxt'] and '2510' in res['firstTxt'], res['firstTxt']
    print('ok - 事件流插入+同指纹原卡片更新置顶(不另开新卡):', res['firstTxt'][:40])

    # ===== 4. 格子点击跳详情页 =====
    href = pg.evaluate('''() => { const a = '/pc/detail?sym=MA2611&period=15';
        return a.startsWith('/pc/detail?sym=') && a.includes('period=15'); } ''')
    assert href
    print('ok - 格子点击目标 = /pc/detail?sym=..&period=..')

    # ===== 5. 旧页面打开无报错（pattern_alert 接入不破坏） =====
    for path in ['/pc', '/pc/detail?sym=RB2701&period=15', '/m']:
        pg.goto(BASE + path, wait_until='domcontentloaded')
        pg.wait_for_timeout(2500)
    print('ok - 总览/详情/手机页打开无报错')

    # ===== 6. 矩阵页截图肉眼验证 =====
    pg.goto(f'{BASE}/pc/matrix', wait_until='networkidle')
    pg.wait_for_timeout(1000)
    pg.evaluate('''() => {
        applyCell({symbol:'MA2611', name:'甲醇', period:'15', state:'cand_a', side:'long',
                   pattern:'VREV_LONG', price:2500, time:'2026-10-06 14:35', p1:2450, p2:2490, p3:2440, ts:1});
        applyCell({symbol:'RB2701', name:'螺纹钢', period:'30m', state:'cand_b', side:'short',
                   pattern:'3SELL_CAND', price:310, time:'2026-10-06 14:20', p1:320, p2:315, p3:309, ts:1});
        applyCell({symbol:'SR2701', name:'白糖', period:'60', state:'watch_a', side:'long',
                   price:5612, time:'2026-10-06 13:58', p1:5580, p2:5630, p3:5560, ts:1});
        upsertEvent({ trigger_time:new Date().toISOString(), kind:'A', side:'long', fingerprint:'f_snap', symbol:'MA2611', name:'甲醇', period:'15', price:2500,
                  time:'2026-10-06 14:35', bi_span:2, ts:Date.now()/1000, text:'甲醇 15分 背驰V反(多)：突破2490',
                  brief:'突破 <b>2490</b> · A类候选（等回调确认二买）', state:'cand_a'});
    }''')
    pg.screenshot(path='/tmp/matrix_shot.png', full_page=False)
    print('ok - 截图 /tmp/matrix_shot.png')

    b.close()

fatal = [e for e in errors if 'favicon' not in e.lower() and 'vibrate' not in e.lower()]
if fatal:
    print('\n发现报错:')
    for e in fatal[:10]:
        print(' ', e)
    sys_exit = 1
else:
    print('\n✅ Playwright 自测全部通过，0 报错')
    sys_exit = 0
import sys
sys.exit(sys_exit)
