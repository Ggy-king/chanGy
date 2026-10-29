# -*- coding: utf-8 -*-
"""总览页按钮/自检入设置/固定顺序（不拖动） + 驾驶舱头部拖动 E2E（file:// 直载，stub fetch 零网络依赖）
2026-10-29 定稿：列表页顺序固定跟服务端 config.CONTRACTS，拖动排序与 cl_symbol_order 覆盖已移除。
"""
import os, json
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
OVERVIEW = 'file://' + os.path.join(ROOT, 'web', 'pc_overview.html')
COCKPIT = 'file://' + os.path.join(ROOT, 'web', 'pc_positions.html')

ITEMS = {'items': [
    {'symbol': 'JD2701', 'name': '鸡蛋', 'last_price': 3276, 'change': -38,
     'last_signal': None, 'stats': {'trades': 47, 'win_rate': 97.9}, 'margin_per_lot': 3276, 'bi_count': 44},
    {'symbol': 'LH2701', 'name': '生猪', 'last_price': 1686, 'change': 6,
     'last_signal': None, 'stats': {'trades': 41, 'win_rate': 100}, 'margin_per_lot': 3372, 'bi_count': 40},
    {'symbol': 'CY2701', 'name': '棉纱', 'last_price': 15285, 'change': -95,
     'last_signal': None, 'stats': {'trades': 78, 'win_rate': 100}, 'margin_per_lot': 7642, 'bi_count': 39},
]}

STUB = """
navigator._stubItems = %s;
window.fetch = async (url, opts) => {
  const u = String(url);
  if (u.startsWith('/api/overview')) {
    return {ok: true, json: async () => navigator._stubItems};
  }
  return {ok: true, json: async () => ({})};
};
""" % json.dumps(ITEMS)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)

    # ---------- 总览页 ----------
    pg = b.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'overview pageerror: {e}'))
    pg.add_init_script(STUB)
    pg.goto(OVERVIEW)
    pg.wait_for_timeout(1000)

    # 1) 头部按钮：持仓在、自检不在头部、自检在设置菜单里
    assert pg.locator('header a[href="/pc/positions"]').count() == 1, '头部缺 持仓 按钮'
    txt = pg.locator('header a[href="/pc/positions"]').inner_text()
    assert '持仓' in txt, f'持仓按钮文案不对: {txt}'
    assert pg.locator('header button[onclick*="/pc/health"]').count() == 0, '头部自检按钮未移除'
    pg.click('#settingsBtn')
    assert pg.locator('#setMenu a[href="/pc/health"]').count() == 1, '设置菜单缺 全绿自检'
    assert pg.locator('#setMenu a[href="/pc/tqstats"]').count() == 1, '设置菜单连接诊断丢失'
    print('ok 1/5 头部：持仓按钮在、自检已入设置菜单')

    # 2) 行渲染（2026-10-29 定稿）：不可拖 + 行点击进详情
    pg.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    assert pg.evaluate('document.querySelector("#tb tr[data-sym]").draggable') is not True, '列表页不应可拖（顺序已定稿）'
    assert pg.evaluate('document.querySelector("#tb tr[data-sym]").onclick !== null'), '行点击进详情丢失'
    print('ok 2/5 列表页不可拖、行点击保留')

    # 3) 固定顺序：严格跟服务端返回序（stub 顺序），老 cl_symbol_order 存在也不生效
    # 模拟老用户残留的拖动排序
    pg.evaluate('''() => { localStorage.setItem("cl_symbol_order",
      JSON.stringify(["LH2701","CY2701","JD2701"])); }''')
    pg.reload()
    pg.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    dom = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym]")].map(r=>r.dataset.sym)')
    assert dom == ['JD2701', 'LH2701', 'CY2701'], f'顺序应跟服务端而非 cl_symbol_order: {dom}'
    seqs = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym] td:first-child")].map(t=>t.textContent)')
    assert seqs == ['01', '02', '03'], f'序号未按服务端序渲染: {seqs}'
    print('ok 3/5 固定顺序生效：老 cl_symbol_order 残留被忽略，严格跟服务端')

    # 刷新后顺序保持
    pg.reload()
    pg.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    dom2 = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym]")].map(r=>r.dataset.sym)')
    assert dom2 == ['JD2701', 'LH2701', 'CY2701'], f'刷新后顺序丢失: {dom2}'
    print('ok 4/5 刷新后固定顺序保持')
    if errs:
        print('总览页 JS 错误:', errs)
        raise SystemExit(1)
    pg.close()

    # ---------- 驾驶舱 ----------
    pc = b.new_page()
    errs2 = []
    pc.on('pageerror', lambda e: errs2.append(f'cockpit pageerror: {e}'))
    pc.add_init_script(STUB)
    pc.goto(COCKPIT)
    pc.wait_for_timeout(1000)

    # 4) 槽位头部整体可拖
    r = pc.evaluate('''() => {
      const div = slotEl({watch:{symbol:'v2701', period:'5'}}, 0);
      document.body.appendChild(div);
      const shead = div.querySelector('.shead');
      return {
        draggable: shead.draggable,
        title: shead.getAttribute('title') || '',
      };
    }''')
    assert r['draggable'], 'shead 未设 draggable'
    print('ok 5a/5 驾驶舱槽位头部整体 draggable=true')

    # 5) 头部拖动换位逻辑：按在按钮上不启动拖动；按在头部空白处启动
    pc.evaluate('''() => {
      const div = slotEl({watch:{symbol:'v2701', period:'5'}}, 0);
      div.id='t_slot'; document.body.appendChild(div);
      window._dt = new DataTransfer();
    }''')
    # 按在"移除"按钮上 → 不应进入拖动
    pc.evaluate('''() => {
      const btn = document.querySelector('#t_slot .shead button');
      btn.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:window._dt,cancelable:true}));
    }''')
    idx_btn = pc.evaluate('typeof _dragIdx!=="undefined" ? _dragIdx : -99')
    assert idx_btn == -1, f'按在按钮上不应启动拖动, _dragIdx={idx_btn}'
    # 按在头部（品种名区域）→ 启动拖动
    pc.evaluate('''() => {
      const shead = document.querySelector('#t_slot .shead');
      shead.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:window._dt,cancelable:true}));
    }''')
    idx_head = pc.evaluate('_dragIdx')
    assert idx_head == 0, f'按在头部应启动拖动, _dragIdx={idx_head}'
    pc.evaluate('dragEnd()')
    print('ok 5b/5 头部按钮上拖动被拦、头部空白处拖动生效')

    # 5c) 零刷新换位：真实槽位上拖动 → 只改 CSS order，图表零重建、DOM 原地不动
    pc.evaluate('''() => {
      localStorage.setItem('cl_cockpit_watches', JSON.stringify([
        {symbol:'v2701',period:'5'},{symbol:'m2701',period:'15'},{symbol:'ta2701',period:'15'}]));
      localStorage.removeItem('cl_cockpit_order');
    }''')
    pc.reload()
    pc.wait_for_function('document.querySelectorAll("#wrap > .strip").length === 4', timeout=5000)
    pc.evaluate('''() => {
      window._destroyed = 0;
      const oc = destroyChart;
      window.destroyChart = function(i){ window._destroyed++; oc(i); };
      window._before = Array.from(document.querySelectorAll('#wrap > .strip'));
      window._sigBefore = _slotSig;
    }''')
    pc.evaluate('''() => {
      const kids = document.querySelectorAll('#wrap > .strip');
      const dt = new DataTransfer();
      kids[0].querySelector('.shead').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[1].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[1].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[0].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    }''')
    pc.wait_for_timeout(300)
    r = pc.evaluate('''() => ({
      destroyed: window._destroyed,
      same0: window._before[0] === document.querySelectorAll('#wrap > .strip')[0],
      same1: window._before[1] === document.querySelectorAll('#wrap > .strip')[1],
      o0: document.querySelectorAll('#wrap > .strip')[0].style.order,
      o1: document.querySelectorAll('#wrap > .strip')[1].style.order,
      saved: localStorage.getItem('cl_cockpit_order'),
      sigSame: window._sigBefore === _slotSig,
    })''')
    assert r['destroyed'] == 0, f'拖动不应重建任何图表, 销毁 {r["destroyed"]} 个'
    assert r['same0'] and r['same1'], '拖动后 DOM 节点应原地不动'
    assert r['o0'] == '1' and r['o1'] == '0', f'视觉顺序未换位: {r["o0"]}/{r["o1"]}'
    assert r['saved'] and 'wm2701|15' in r['saved'], f'顺序未持久化: {r["saved"]}'
    assert r['sigSame'], '拖动不应触发 render 重算'
    print('ok 5c/5 零刷新换位：0 次图表重建、DOM 原地不动、视觉顺序+持久化正确')

    # 5d) 跨位拖动=交换语义：第1卡拖到第3卡，中间卡（第2）必须原地不动
    pc.evaluate('''() => {
      window._destroyed = 0;
      const kids = document.querySelectorAll('#wrap > .strip');
      const dt = new DataTransfer();
      kids[0].querySelector('.shead').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[2].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[2].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[0].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    }''')
    pc.wait_for_timeout(200)
    r2 = pc.evaluate('''() => ({
      destroyed: window._destroyed,
      o: [...document.querySelectorAll('#wrap > .strip')].map(x => x.style.order),
      saved: JSON.parse(localStorage.getItem('cl_cockpit_order') || '[]'),
    })''')
    assert r2['destroyed'] == 0, '跨位拖动也不应重建图表'
    # 5c 后视觉=[m,v,ta,'']；第1卡(v,视觉2)拖到第3卡(ta,视觉3)互换 → [m,ta,v,'']，中间 m 不动
    assert r2['saved'] == ['wm2701|15', 'wta2701|15', 'wv2701|5', 'e3'], \
        f'跨位应为交换而非插队: {r2["saved"]}'
    assert r2['o'][0] == '2' and r2['o'][1] == '0' and r2['o'][2] == '1', \
        f'视觉序不对: {r2["o"]}'
    print('ok 5d/5 跨位拖动=纯交换：中间卡不动，空槽不挤位')

    # 5e) 关键回归（用户实踩场景）：存序生效后 DOM 序≠视觉序，拖动仍按视觉位正确交换
    pc.evaluate('''() => {
      localStorage.setItem('cl_cockpit_order', JSON.stringify(['wta2701|15','wv2701|5','wm2701|15','e3']));
    }''')
    pc.reload()
    pc.wait_for_function('document.querySelectorAll("#wrap > .strip").length === 4', timeout=5000)
    vis = pc.evaluate('''() => {
      const kids=[...document.querySelectorAll('#wrap > .strip')];
      return kids.map(x=>x.style.order);
    }''')
    # DOM=[v,m,ta,e]，存序=[ta,v,m,e] → 视觉序分别 1/2/0/3
    assert vis == ['1','2','0','3'], f'存序应用后视觉位不对: {vis}'
    pc.evaluate('''() => {
      const kids=document.querySelectorAll('#wrap > .strip');
      const dt=new DataTransfer();
      kids[1].querySelector('.shead').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[2].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[2].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt,cancelable:true}));
      kids[1].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    }''')
    pc.wait_for_timeout(200)
    r3 = pc.evaluate('''() => ({
      o: [...document.querySelectorAll('#wrap > .strip')].map(x => x.style.order),
      saved: JSON.parse(localStorage.getItem('cl_cockpit_order') || '[]'),
    })''')
    # 视觉=[ta,v,m]：拖 m(视觉3) 到 ta(视觉1) 互换 → 视觉 [m,v,ta]，中间 v 不动
    assert r3['saved'] == ['wm2701|15', 'wv2701|5', 'wta2701|15', 'e3'], \
        f'DOM≠视觉时交换错乱: {r3["saved"]}'
    assert r3['o'] == ['1', '0', '2', '3'], f'视觉位不对: {r3["o"]}'
    print('ok 5e/5 存序生效(DOM序≠视觉序)时拖动仍按视觉位正确交换')

    if errs2:
        print('驾驶舱 JS 错误:', errs2)
        raise SystemExit(1)
    pc.close()

    # ---------- 监控页矩阵：品种列拖动排序 ----------
    MX = 'file://' + os.path.join(ROOT, 'web', 'pc_matrix.html')
    mx = b.new_page()
    errs3 = []
    mx.on('pageerror', lambda e: errs3.append(f'matrix pageerror: {e}'))
    mx.add_init_script("""
navigator._mx = %s;
window.fetch = async (url) => {
  const u = String(url);
  if (u.startsWith('/api/pattern_matrix')) return {ok: true, json: async () => navigator._mx};
  return {ok: true, json: async () => ({})};
};
""" % json.dumps({
        'periods': ['5', '15'],
        'contracts': [{'symbol': 'JD2701', 'name': '鸡蛋'},
                      {'symbol': 'LH2701', 'name': '生猪'},
                      {'symbol': 'CY2701', 'name': '棉纱'}],
        'cells': [], 'events': [], 'mon_enabled': True,
    }))
    mx.goto(MX)
    mx.wait_for_function('document.querySelectorAll("#matrix th.colhead").length === 3', timeout=5000)

    cols = mx.evaluate('[...document.querySelectorAll("#matrix th.colhead")].map(t=>t.dataset.sym)')
    assert cols == ['JD2701', 'LH2701', 'CY2701'], f'初始列序不对: {cols}'
    # 列头点击进详情保留
    oc = mx.evaluate('document.querySelector("#matrix th.colhead").getAttribute("onclick")')
    assert oc and oc.startswith('go('), f'列头 onclick 丢失: {oc}'
    print('ok 6/7 矩阵列头渲染正确、点击进详情保留')

    # 拖列头：第1列拖到第3列 → [LH,CY,JD] + 持久化
    mx.evaluate('''() => {
      const ths=[...document.querySelectorAll('#matrix th.colhead')];
      const dt=new DataTransfer();
      ths[0].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt,cancelable:true}));
      ths[2].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt,cancelable:true}));
      ths[2].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt,cancelable:true}));
      ths[0].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    }''')
    mx.wait_for_timeout(200)
    saved = mx.evaluate('JSON.parse(localStorage.getItem("cl_matrix_order")||"[]")')
    assert saved == ['LH2701', 'CY2701', 'JD2701'], f'矩阵列序未持久化: {saved}'
    cols2 = mx.evaluate('[...document.querySelectorAll("#matrix th.colhead")].map(t=>t.dataset.sym)')
    assert cols2 == saved, f'DOM 列序与存储不一致: {cols2}'
    print('ok 7/7 矩阵品种列拖动排序生效且持久化')

    # 刷新后顺序保持
    mx.reload()
    mx.wait_for_function('document.querySelectorAll("#matrix th.colhead").length === 3', timeout=5000)
    cols3 = mx.evaluate('[...document.querySelectorAll("#matrix th.colhead")].map(t=>t.dataset.sym)')
    assert cols3 == ['LH2701', 'CY2701', 'JD2701'], f'刷新后矩阵列序丢失: {cols3}'
    print('ok 矩阵列序刷新后保持')
    if errs3:
        print('监控页 JS 错误:', errs3)
        raise SystemExit(1)
    mx.close()

    # ---------- 总览页二次确认：无任何残留拖动行为 ----------
    ov = b.new_page()
    errs4 = []
    ov.on('pageerror', lambda e: errs4.append(f'overview pageerror: {e}'))
    ov.add_init_script(STUB)
    ov.goto(OVERVIEW)
    ov.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    assert ov.evaluate('document.querySelector("#tb tr[data-sym]").draggable') is not True, '不应有残留 draggable'
    assert ov.evaluate('typeof initDragSort === "undefined"'), 'initDragSort 应已移除'
    print('ok 总览页拖动已彻底移除（固定顺序定稿）')
    if errs4:
        print('回退后总览页 JS 错误:', errs4)
        raise SystemExit(1)
    b.close()

print('ok - 全部通过：总览页固定顺序 / 驾驶舱头部拖动 / 监控页品种列拖动')
