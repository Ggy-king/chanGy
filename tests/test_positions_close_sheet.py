# -*- coding: utf-8 -*-
"""APK positions.html 平仓简报按钮点击自测（file:// 直载，无网络依赖）"""
import os
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

PAGE = 'file://' + os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                                'mobile-app', 'app', 'src', 'main', 'assets', 'positions.html')

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))

    pg.goto(PAGE)
    pg.wait_for_timeout(800)

    # 注入一条持仓 → 打开平仓简报
    pg.evaluate('''() => {
      POSITIONS.push({id:'t1', symbol:'v2701', name:'PVC', period:'5', entry:'二买', exit:'二卖',
        open_time:'10-12 09:00', weekday:'周一', open_iso:new Date().toISOString(), note:'', result:'', pnl:''});
      posDel('t1');
    }''')
    pg.wait_for_timeout(300)
    vis = pg.evaluate('document.getElementById("posCloseOv").style.display')
    assert vis == 'flex', f'弹层未打开: {vis}'

    # 点"止盈"label → 原生 radio 选中（纯 CSS :checked，零 JS 依赖）
    lbl = pg.locator('#mpcResults label[for="mpcR0"]')
    lbl.click()
    pg.wait_for_timeout(200)
    checked = pg.evaluate('document.getElementById("mpcR0").checked')
    col = lbl.evaluate('e => getComputedStyle(e).backgroundColor')
    print(f'点击止盈后: radio.checked={checked} label背景={col}')
    assert checked, 'radio 未选中'
    assert col == 'rgb(245, 184, 61)', f'金色选中态未生效: {col}'

    # 点"止损" → 原生单选切换（止盈自动退选）
    lbl2 = pg.locator('#mpcResults label[for="mpcR1"]')
    lbl2.click()
    pg.wait_for_timeout(200)
    assert pg.evaluate('document.getElementById("mpcR1").checked'), '止损未选中'
    assert not pg.evaluate('document.getElementById("mpcR0").checked'), '止盈未退选'
    col2 = lbl2.evaluate('e => getComputedStyle(e).backgroundColor')
    assert col2 == 'rgb(245, 184, 61)', f'止损金色未生效: {col2}'
    print('单选切换正确：止盈退选、止损金色选中')

    if errs:
        print('页面 JS 错误:', errs)
        raise SystemExit(1)
    print('ok - 平仓简报三按钮点击/选中/切换全部正常')
    b.close()
