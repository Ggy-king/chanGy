# -*- coding: utf-8 -*-
"""持仓页右键一键止损线 E2E：照详情页交互（布防→点击图面布线→拖线微调→实时标题→落盘）"""
import os, json
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

PAGE = 'file://' + os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web', 'pc_positions.html')

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))
    pg.add_init_script("""
window._reqs = [];
window.fetch = async (u, o) => {
  window._reqs.push({u: String(u), body: o && o.body ? o.body : null, method: (o && o.method) || 'GET'});
  return {ok: true, json: async () => ({})};
};
""")
    pg.goto(PAGE, wait_until='domcontentloaded')
    pg.evaluate("""localStorage.setItem('cl_cockpit_watches', JSON.stringify([{symbol:'v2701',period:'5'},{symbol:'v2701',period:'15'}]));
localStorage.setItem('cl_calc_capital','100000');localStorage.setItem('cl_calc_risk_pct','0.02');
localStorage.removeItem('cl_cockpit_order');""")
    pg.reload()
    pg.wait_for_function('document.querySelectorAll("#wrap > .strip").length === 4', timeout=5000)

    # 注入假 candle（无 LightweightCharts 环境）：1px=1点，1450 在 y=100
    pg.evaluate('''() => {
      const mk = () => ({
        coordinateToPrice: y => 1450 + (100 - y),
        priceToCoordinate: p => 100 - (p - 1450),
        createPriceLine: o => { const L = {price: o.price, title: o.title,
            options: () => ({price: L.price, title: L.title}),
            applyOptions: x => { for (const k in x) L[k] = x[k]; }, removed: false};
          window._lines.push(L); return L; },
        removePriceLine: L => { L.removed = true; }
      });
      window._lines = [];
      CHARTS[0] = {sym: 'v2701', candle: mk(), slLine: null, chart: {applyOptions(){}}};
      CHARTS[1] = {sym: 'v2701', candle: mk(), slLine: null, chart: {applyOptions(){}}};
      QUOTES['v2701'] = {price: 1447.5};
    }''')

    # 1) 右键模块 → 菜单（一键止损线入口，无删除项）
    pg.evaluate('''() => {
      document.querySelectorAll('#wrap > .strip')[0]
        .dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:200,clientY:150}));
    }''')
    pg.wait_for_timeout(120)
    assert pg.locator('#slMenu').is_visible(), '右键菜单未弹出'
    assert pg.locator('text=🛡 一键止损线（按现价）').is_visible(), '缺一键止损线入口'
    assert pg.locator('#slMenuDel').is_hidden(), '无止损线时不应显示删除'
    print('ok 1/6 右键菜单弹出（一键止损线入口）')

    # 2) 点一键止损线 → 布防模式：crosshair 隐藏、提示语、等待图面点击
    pg.click('text=🛡 一键止损线（按现价）')
    pg.wait_for_timeout(120)
    st = pg.evaluate('({setting: _slSettingSlot, cross: CHARTS[0] ? "n/a" : "n/a", hint: document.getElementById("vtag_0").innerText})')
    assert st['setting'] == 0, f'布防模式未进入: {st}'
    assert '点击图面布线' in st['hint'], f'提示语未显示: {st["hint"]}'
    assert pg.locator('#slMenu').is_hidden(), '菜单未关闭'
    print('ok 2/6 布防模式进入（提示语上头、菜单关闭）')

    # 3) 点击图面 y=100 → 布线 1450 → POST {sym,price} → 提示语还原
    rect = pg.evaluate('JSON.stringify(document.getElementById("chart_0").getBoundingClientRect())')
    r = json.loads(rect)
    pg.mouse.click(r['left'] + 150, r['top'] + 100)
    pg.wait_for_timeout(200)
    post = pg.evaluate('window._reqs.filter(x=>x.method==="POST"&&x.u.includes("/api/sl_line"))')
    assert post, '布线未发出 POST'
    body = json.loads(post[-1]['body'])
    assert body == {'sym': 'v2701', 'price': 1450}, f'布线价格不对: {body}'
    st2 = pg.evaluate('({setting: _slSettingSlot, hint: document.getElementById("vtag_0").innerText, n: window._lines.length})')
    assert st2['setting'] == -1, '布防未退出'
    assert '点击图面布线' not in st2['hint'], '提示语未还原'
    assert st2['n'] == 2, f'红线未创建(同品种两槽位应各一条): {st2["n"]}'
    # 线上标题实时反算：入场=现价1447.5，止损1450 → 2.5点×乘10=25元/手；2%风险=2000元 → 80手
    t1 = pg.evaluate('window._lines[0].title')
    assert '2000元/80手/2.5点' in t1, f'标题反算不对: {t1}'
    print('ok 3/6 点击图面布线：POST 正确、提示还原、标题实时反算(2000元/80手/2.5点)')

    # 4) 拖线：按下(距线15px内) → 移到 y=60(=1490) → 松手吸附落盘
    pg.mouse.move(r['left'] + 150, r['top'] + 100)
    pg.mouse.down()
    pg.mouse.move(r['left'] + 150, r['top'] + 60, steps=3)
    pg.mouse.up()
    pg.wait_for_timeout(200)
    post2 = pg.evaluate('window._reqs.filter(x=>x.method==="POST"&&x.u.includes("/api/sl_line"))')
    assert len(post2) >= 2, '拖完未落盘'
    body2 = json.loads(post2[-1]['body'])
    assert body2['price'] == 1490, f'拖动后价格不对: {body2}'
    t2 = pg.evaluate('window._lines[0].price')
    assert t2 == 1490, f'线价格未更新: {t2}'
    # 新标题：现价1447.5 止损1490 → 42.5点×10=425元/手，4手=1700元(1.7%≤2% 无⚠)
    t2t = pg.evaluate('window._lines[0].title')
    assert '1700元/4手/42.5点' in t2t, f'拖后标题不对: {t2t}'
    print('ok 4/6 拖线微调：松手吸附落盘(1490)、标题实时反算(1700元/4手/42.5点)')

    # 5) 同品种两槽位同步（v2701 在 0/1 两格）
    t3 = pg.evaluate('window._lines[1] ? window._lines[1].price : null')
    assert t3 == 1490, f'同品种另一槽位未同步: {t3}'
    print('ok 5/6 同品种多槽位止损线自动同步')

    # 6) 右键 → 删除止损线 → DELETE + 线移除
    pg.evaluate('''() => {
      document.querySelectorAll('#wrap > .strip')[0]
        .dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:200,clientY:150}));
    }''')
    pg.wait_for_timeout(100)
    assert pg.locator('#slMenuDel').is_visible(), '已布线应显示删除'
    pg.click('#slMenuDel')
    pg.wait_for_timeout(150)
    dele = pg.evaluate('window._reqs.filter(x=>x.method==="DELETE"&&x.u.includes("/api/sl_line"))')
    assert dele and 'v2701' in dele[-1]['u'], '未发出 DELETE'
    assert pg.evaluate('window._lines[0].removed'), '红线未移除'
    assert pg.evaluate('window._lines[1].removed'), '另一槽位红线未移除'
    print('ok 6/6 删除止损线：DELETE + 两槽位红线同时移除')

    if errs:
        print('页面 JS 错误:', errs)
        raise SystemExit(1)
    b.close()
print('ok - 持仓页一键止损线（详情页同款交互）E2E 全部通过')
