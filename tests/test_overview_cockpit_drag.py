# -*- coding: utf-8 -*-
"""总览页按钮/自检入设置/品种名拖动排序 + 驾驶舱头部拖动 E2E（file:// 直载，stub fetch 零网络依赖）"""
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

    # 2) 行渲染（回退后定稿）：整行可拖 + 行点击进详情
    pg.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    assert pg.evaluate('document.querySelector("#tb tr[data-sym]").draggable') is True, '整行应可拖（回退后原样）'
    cur = pg.locator('#tb tr[data-sym]').first.locator('td').first.evaluate('e => getComputedStyle(e).cursor')
    assert cur == 'grab', f'序号格 cursor 应为 grab: {cur}'
    assert pg.evaluate('document.querySelector("#tb tr[data-sym]").onclick !== null'), '行点击进详情丢失'
    print('ok 2/5 整行可拖（回退原样）、行点击保留')

    # 3) 拖动排序：第1行拖到第3行 → 顺序 [LH,CY,JD] + localStorage 持久化
    pg.evaluate('''() => {
      const rows=[...document.querySelectorAll('#tb tr[data-sym]')];
      const dt=new DataTransfer();
      rows[0].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt,cancelable:true}));
      rows[2].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt,cancelable:true}));
      rows[2].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt,cancelable:true}));
      rows[0].dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
    }''')
    pg.wait_for_timeout(200)
    order = pg.evaluate('JSON.parse(localStorage.getItem("cl_symbol_order")||"[]")')
    assert order == ['LH2701', 'CY2701', 'JD2701'], f'排序未持久化: {order}'
    dom = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym]")].map(r=>r.dataset.sym)')
    assert dom == order, f'DOM 顺序与存储不一致: {dom}'
    seqs = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym] td:first-child")].map(t=>t.textContent)')
    assert seqs == ['01', '02', '03'], f'序号未重排: {seqs}'
    print('ok 3/5 拖动排序生效且持久化，序号重排正确')

    # 刷新后顺序保持
    pg.reload()
    pg.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    dom2 = pg.evaluate('[...document.querySelectorAll("#tb tr[data-sym]")].map(r=>r.dataset.sym)')
    assert dom2 == ['LH2701', 'CY2701', 'JD2701'], f'刷新后顺序丢失: {dom2}'
    print('ok 4/5 刷新后自定义顺序保持')
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

    # ---------- 总览页回退验证：恢复整行拖动 ----------
    ov = b.new_page()
    errs4 = []
    ov.on('pageerror', lambda e: errs4.append(f'overview pageerror: {e}'))
    ov.add_init_script(STUB)
    ov.goto(OVERVIEW)
    ov.wait_for_function('document.querySelectorAll("#tb tr[data-sym]").length === 3', timeout=5000)
    tr = ov.locator('#tb tr[data-sym]').first
    assert ov.evaluate('document.querySelector("#tb tr[data-sym]").draggable') is True, '回退失败：整行应可拖'
    assert tr.locator('td[draggable="true"]').count() == 0, '回退失败：品种名单元格不应带 draggable'
    print('ok 总览页已回退：整行拖动原样')
    if errs4:
        print('回退后总览页 JS 错误:', errs4)
        raise SystemExit(1)
    b.close()

print('ok - 全部通过：驾驶舱头部拖动 / 监控页品种列拖动 / 总览页回退')


print('ok - 总览页按钮/自检入设置/品种名拖动排序 + 驾驶舱头部拖动 全部通过')
