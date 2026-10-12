# -*- coding: utf-8 -*-
"""m_list.html（服务器 /m 页）平仓简报 radio 点击自测——模拟手机在线模式的真实场景"""
import os
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width': 400, 'height': 900})
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))

    pg.goto('http://127.0.0.1:8000/m')
    pg.wait_for_timeout(1500)

    # 切到持仓 tab 并注入一条持仓 → 打开平仓简报
    pg.evaluate('''() => {
      switchTab(1);
      POSITIONS.push({id:'t1', symbol:'v2701', name:'PVC', period:'3', entry:'二买', exit:'二卖',
        open_time:'10-12 12:53', weekday:'周一', open_iso:new Date().toISOString(), note:'', result:'', pnl:''});
      posDel('t1');
    }''')
    pg.wait_for_timeout(400)
    vis = pg.evaluate('document.getElementById("posCloseOv").style.display')
    assert vis == 'flex', f'弹层未打开: {vis}'

    # 点"止盈"label → 原生 radio 选中
    lbl = pg.locator('#mpcResults label[for="mpcR0"]')
    lbl.click()
    pg.wait_for_timeout(200)
    checked = pg.evaluate('document.getElementById("mpcR0").checked')
    col = lbl.evaluate('e => getComputedStyle(e).backgroundColor')
    print(f'点击止盈后: radio.checked={checked} label背景={col}')
    assert checked, 'radio 未选中'
    assert col == 'rgb(245, 184, 61)', f'金色选中态未生效: {col}'

    # 点"保本" → 单选切换
    lbl2 = pg.locator('#mpcResults label[for="mpcR2"]')
    lbl2.click()
    pg.wait_for_timeout(200)
    assert pg.evaluate('document.getElementById("mpcR2").checked'), '保本未选中'
    assert not pg.evaluate('document.getElementById("mpcR0").checked'), '止盈未退选'
    print('单选切换正确')

    # 确认平仓读取选中值（拦截 ghPut 避免真删云端数据；简报进 history.json 的 close_result）
    pg.evaluate('window._cap = {}; ghPut = async (p, d) => { window._cap[p] = d; return ""; }')
    pg.evaluate('PosCore.confirm()')
    pg.wait_for_timeout(600)
    cap = pg.evaluate('window._cap["history.json"]')
    res = ((cap or {}).get('history') or [{}])[0].get('close_result', '(无)')
    print(f'确认平仓 → 简报写入: "{res}"')
    assert res == '保本', f'简报读取错误: {res}'

    if errs:
        print('页面 JS 错误:', errs)
        raise SystemExit(1)
    print('ok - /m 页平仓简报 radio 全链路正常（选中/切换/确认读取）')
    b.close()
