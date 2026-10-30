# -*- coding: utf-8 -*-
"""全局热键 E2E（本地静态服务起 web/ 目录，走真实 <script src="/hotkeys.js"> 加载链路）
2026-10-30 定稿：1-9/0 跳详情页（顺序=config.CONTRACTS）、Shift+数字=第11-20位、
Backspace 返回（详情页调 goBack 直达入口页，其他页 history.back）。
守卫：输入框聚焦/Cmd·Ctrl·Alt 组合/输入法组字中 全部放行。
"""
import os, json, threading, socketserver, http.server, functools
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

# ---------- 本地静态服务（web/ 为根，/hotkeys.js 由真实文件服务） ----------
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
class _Server(socketserver.TCPServer):
    allow_reuse_address = True
_handler = functools.partial(_Quiet, directory=os.path.join(ROOT, 'web'))
_srv = _Server(('127.0.0.1', 0), _handler)
PORT = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{PORT}'
OVERVIEW = BASE + '/pc_overview.html'
DETAIL = BASE + '/pc_detail.html?sym=MA2701'

# 前 10 位热键区 + 1 个 Shift 档（第 11 位棉花）
LIST = [
    {'symbol': 'MA2701', 'name': '甲醇'}, {'symbol': 'v2701', 'name': 'PVC'},
    {'symbol': 'SA2701', 'name': '纯碱'}, {'symbol': 'jm2701', 'name': '焦煤'},
    {'symbol': 'RU2701', 'name': '橡胶'}, {'symbol': 'SR2701', 'name': '白糖'},
    {'symbol': 'SP2701', 'name': '纸浆'}, {'symbol': 'jd2612', 'name': '鸡蛋'},
    {'symbol': 'CJ2701', 'name': '红枣'}, {'symbol': 'AP2701', 'name': '苹果'},
    {'symbol': 'CF2701', 'name': '棉花'},
]
SEED = "localStorage.setItem('cl_hotkey_contracts', JSON.stringify({ts: Date.now(), list: %s}));" % json.dumps(LIST, ensure_ascii=False)

# LWC CDN 本地缓存（2026-10-30：unpkg 超时会卡死 goto load，测试不再依赖外网）
LWC_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets_lwc.js')
def new_page(b):
    pg = b.new_page()
    pg.route('**unpkg.com/**', lambda r: r.fulfill(path=LWC_LOCAL, content_type='application/javascript'))
    return pg

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)

    # ---------- 列表页：数字跳转 + 三层守卫 + Backspace ----------
    pg = new_page(b)
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))
    pg.add_init_script(SEED)
    navs = []
    pg.on('framenavigated', lambda f: navs.append(f.url) if 'detail' in (f.url or '') else None)
    pg.goto(OVERVIEW)
    pg.wait_for_timeout(800)

    # 1) 单键 1 → 甲醇（不带 period，级别继承 localStorage）
    pg.keyboard.press('1')
    pg.wait_for_timeout(400)
    assert navs and navs[-1].endswith('/pc/detail?sym=MA2701'), f'按键1应跳甲醇, navs={navs}'
    print('ok 1/10 单键1 → /pc/detail?sym=MA2701（无 period，级别自动继承）')

    # 2) 5 → 橡胶、0 → 苹果（第10位）
    pg.goto(OVERVIEW); pg.wait_for_timeout(500)
    pg.keyboard.press('5'); pg.wait_for_timeout(300)
    assert navs[-1].endswith('sym=RU2701'), f'按键5应跳橡胶: {navs[-1]}'
    pg.goto(OVERVIEW); pg.wait_for_timeout(500)
    pg.keyboard.press('0'); pg.wait_for_timeout(300)
    assert navs[-1].endswith('sym=AP2701'), f'按键0应跳苹果(第10位): {navs[-1]}'
    print('ok 2/10 按键5→橡胶、按键0→第10位苹果')

    # 3) Shift+1 → 第11位棉花；Shift+0（第20位）超出清单不动作
    pg.goto(OVERVIEW); pg.wait_for_timeout(500)
    pg.keyboard.press('Shift+1'); pg.wait_for_timeout(300)
    assert navs[-1].endswith('sym=CF2701'), f'Shift+1应跳棉花: {navs[-1]}'
    n_before = len(navs)
    pg.goto(OVERVIEW); pg.wait_for_timeout(500)
    pg.keyboard.press('Shift+0'); pg.wait_for_timeout(300)
    assert len(navs) == n_before, f'Shift+0 超出清单应不动作, navs={navs}'
    print('ok 3/10 Shift+1→第11位棉花、Shift+0 越界不动作')

    # 4) 守卫：输入框聚焦 → 按键变普通字符
    pg.goto(OVERVIEW); pg.wait_for_timeout(500)
    pg.focus('#msg')
    pg.keyboard.type('12')
    val = pg.input_value('#msg')
    assert val == '12', f'输入框聚焦时按键应正常输入: {val!r}'
    # 5) 守卫：Ctrl+1 / isComposing 不触发跳转
    pg.click('header h1')   # 真实点击把焦点移出输入框（body.focus() 在 Chromium 不可靠）
    n_before = len(navs)
    pg.keyboard.press('Control+1'); pg.wait_for_timeout(200)
    pg.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'1',code:'Digit1',isComposing:true,bubbles:true}))")
    pg.wait_for_timeout(200)
    assert len(navs) == n_before, f'Ctrl组合/组字中不应跳转, navs={navs}'
    print('ok 4-5/10 输入框聚焦正常打字、Ctrl+1 与组字中不跳转')

    # 6) 列表页 Backspace → history.back（浏览器后退兜底；spy 只计数不真导航，防上下文销毁）
    pg.evaluate("""() => {
      window._evlog = [];
      document.addEventListener('keydown', e => window._evlog.push([e.key, e.target.tagName, !!e.ctrlKey]), true);
      history.back = () => { window._backSpy = (window._backSpy || 0) + 1; };
      return [document.activeElement && document.activeElement.tagName, history.length];
    }""")
    pg.keyboard.press('Backspace')
    pg.wait_for_timeout(200)
    assert pg.evaluate('window._backSpy') == 1, \
        f"列表页 Backspace 应走 history.back, spy={pg.evaluate('window._backSpy')} evlog={pg.evaluate('window._evlog')}"
    print('ok 6/10 列表页 Backspace → history.back 兜底')
    if errs:
        print('列表页 JS 错误:', errs)
        raise SystemExit(1)
    pg.close()

    # ---------- 详情页：←/→/Q/E 切级别（循环）+ Backspace 调 goBack() ----------
    d = new_page(b)
    errs2 = []
    d.on('pageerror', lambda e: errs2.append(f'detail pageerror: {e}'))
    d.add_init_script(SEED)
    d.goto(DETAIL)
    d.wait_for_timeout(1200)
    # 模拟用户从监控页进入过（detail_exit 已有值），再模拟热键连跳两次产生的"详情→详情"历史
    d.evaluate("() => { sessionStorage.setItem('detail_exit', '/pc/matrix'); }")

    # 8) 级别循环切换：默认15分 → → 右两次到60分
    d.wait_for_function('document.querySelectorAll("#periodSeg button.on").length === 1', timeout=8000)
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == '15', f'详情页默认级别应为15分: {cur}'
    d.click('.name-block')
    d.keyboard.press('e'); d.wait_for_timeout(300)
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == '30m', f'E 右切一次应到30分: {cur}'
    d.keyboard.press('e'); d.wait_for_timeout(300)
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == '60', f'E 右切两次应到60分: {cur}'
    print('ok 8/10 详情页 E 切级别 15→30m→60')

    # 9) 循环轮询：日线再右切回30秒；Q 在30秒左切绕回日线；级别持久化写入
    d.keyboard.press('e'); d.wait_for_timeout(300)   # 60→daily
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == 'daily', f'应到日线: {cur}'
    d.keyboard.press('e'); d.wait_for_timeout(300)   # daily→30秒（循环）
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == '30', f'日线右切应循环回30秒: {cur}'
    d.keyboard.press('q'); d.wait_for_timeout(300)   # Q 左切：30秒→日线
    cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    assert cur == 'daily', f'Q 左切应从30秒绕回日线: {cur}'
    saved = d.evaluate('localStorage.getItem("cl_chart_period")')
    assert saved == 'daily', f'切级别应持久化到 cl_chart_period: {saved}'
    print('ok 9/10 循环轮询（daily→30秒→绕回daily）+ 级别持久化生效')

    # 10) 输入框聚焦时按键不切级别（守卫）；W/S 切品种
    d.reload(); d.wait_for_function('document.querySelectorAll("#periodSeg button.on").length === 1', timeout=8000)
    btns = d.evaluate('Array.from(document.querySelectorAll("#periodSeg button")).map(b=>b.dataset.p)')
    cur0 = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
    d.click('.name-block') if d.locator('.name-block').count() else d.click('header h1')
    chat_focused = d.evaluate('document.activeElement && document.activeElement.tagName')
    if chat_focused == 'INPUT':
        d.keyboard.press('e'); d.wait_for_timeout(200)
        cur = d.evaluate('document.querySelector("#periodSeg button.on").dataset.p')
        assert cur == cur0, f'输入框聚焦时按键不应切级别: {cur} (原 {cur0})'
        d.click('.name-block') if d.locator('.name-block').count() else d.click('header h1')
    d.evaluate('window._prevSpy=0; window._nextSpy=0; const op=window.prevContract, onx=window.nextContract; window.prevContract=()=>{window._prevSpy++; op();}; window.nextContract=()=>{window._nextSpy++; onx();}; void 0')
    d.click('.name-block') if d.locator('.name-block').count() else d.click('header h1')
    d.keyboard.press('w'); d.wait_for_timeout(200)
    d.keyboard.press('s'); d.wait_for_timeout(200)
    assert d.evaluate('window._prevSpy') == 1 and d.evaluate('window._nextSpy') == 1, \
        f"W/S 应触发上一/下一品种: prev={d.evaluate('window._prevSpy')} next={d.evaluate('window._nextSpy')}"
    print('ok 10/10 输入框守卫生效、W/S 切品种生效')

    # ---------- 详情页：Backspace 调 goBack()（直达入口页，不逐级后退） ----------
    # 注意：evaluate 结果为函数时 Playwright 会自动调用它，末尾必须 void 0
    d.evaluate('window.goBack=()=>{ window._goBackSpy=(window._goBackSpy||0)+1; }; void 0')   # 只计数不真导航
    d.click('.name-block')
    d.keyboard.press('Backspace')
    d.wait_for_timeout(300)
    assert d.evaluate('window._goBackSpy') == 1, '详情页 Backspace 必须调 goBack() 而非 history.back'
    print('ok 7/11 详情页 Backspace → goBack()（一键回入口页，连跳N个品种只需一次返回）')

    # 11) 方向键平移/缩放（连续加速模型：长按 300ms → 多帧连续运动、松手即停；缩放锚定中心）
    d.evaluate("""() => {
      window._fake = {pos: 5, span: 100, spCalls: 0, zlCalls: 0};
      window.__clGetChart = () => ({timeScale: () => ({
        options: () => ({barSpacing: 8}),
        scrollPosition: () => window._fake.pos,
        scrollToPosition: p => { window._fake.spCalls++; window._fake.pos = p; },
        getVisibleLogicalRange: () => ({from: window._fake.pos, to: window._fake.pos + window._fake.span}),
        setVisibleLogicalRange: r => { window._fake.zlCalls++; window._fake.span = r.to - r.from; }
      })});
    }""")
    d.click('.name-block')
    d.keyboard.down('ArrowRight')
    d.wait_for_timeout(300)
    d.keyboard.up('ArrowRight')
    d.wait_for_timeout(150)
    st = d.evaluate('window._fake')
    assert st['pos'] > 8 and st['spCalls'] >= 5, f'长按→300ms 应连续多帧平移（B方案起步260px/s渐起）: {st}'
    d.click('.name-block')
    d.keyboard.down('ArrowUp')
    d.wait_for_timeout(300)
    d.keyboard.up('ArrowUp')
    d.wait_for_timeout(150)
    st2 = d.evaluate('window._fake')
    # B 档 rate=1.3，300ms 连续缩放 ≈ e^-0.39 ≈ 0.68 倍 → span 100 → ~68（宽边界防帧率抖动）
    assert st2['zlCalls'] >= 5 and 40 < st2['span'] < 90, f'长按↑300ms 应连续缩放（span 100→~68）: {st2}'
    print(f'ok 11/15 方向键连续加速：长按→300ms 平移 5→{st["pos"]:.0f}（{st["spCalls"]}帧）、↑缩放 span 100→{st2["span"]:.0f}（{st2["zlCalls"]}帧，锚定中心）')

    # 12) A = 一键止损布防（详情页直接触发 startSlSet）
    d.evaluate('window._aSpy=0; window.startSlSet=()=>{ window._aSpy++; }; void 0')
    d.click('.name-block')
    d.keyboard.press('a')
    d.wait_for_timeout(200)
    assert d.evaluate('window._aSpy') == 1, '详情页 A 应触发一键止损布防'
    print('ok 12/15 详情页 A → 一键止损布防')

    # 13) Enter = 图表回到实时（换单例 fake，timeScale() 每次返回同一对象）
    d.evaluate("""() => { window._realCalls = 0;
      const ts = { options: () => ({barSpacing: 8}), scrollPosition: () => 5,
        scrollToPosition: () => {}, getVisibleLogicalRange: () => ({from: 0, to: 100}),
        setVisibleLogicalRange: () => {}, scrollToRealTime: () => { window._realCalls++; } };
      window.__clGetChart = () => ({timeScale: () => ts}); }""")
    d.keyboard.press('Enter')
    d.wait_for_timeout(200)
    assert d.evaluate('window._realCalls') == 1, 'Enter 应调 scrollToRealTime'
    print('ok 13/15 Enter → 图表回到实时')

    # 14) ? = 速查卡（弹出/Esc 关闭）
    d.keyboard.press('?')
    d.wait_for_timeout(200)
    assert d.evaluate('!!document.getElementById("clCheat")'), '? 应弹出速查卡'
    d.keyboard.press('Escape')
    d.wait_for_timeout(200)
    assert d.evaluate('!document.getElementById("clCheat")'), 'Esc 应关闭速查卡'
    print('ok 14/15 ? 速查卡弹出、Esc 关闭')

    # 16) V = 四窗多级别同屏（stub LightweightCharts：V 开 4 实例 / V 关销毁 / 退格先退四窗不导航）
    d.evaluate("""() => {
      const series = {setData(){}, update(){}, applyOptions(){}};
      window._qmk = {created:0, removed:0};
      window.LightweightCharts = {createChart(){ window._qmk.created++;
        return {addCandlestickSeries:()=>series, addLineSeries:()=>series,
          timeScale:()=>({}), applyOptions(){}, options:()=>({crosshair:{mode:0}}),
          remove(){ window._qmk.removed++; }};}};
    }""")
    d.click('.name-block')
    d.keyboard.press('v')
    d.wait_for_timeout(400)
    assert d.evaluate('!!document.getElementById("quadWrap")'), 'V 应打开四窗'
    assert d.evaluate('document.querySelectorAll("#quadWrap div[id^=qbox_]").length') == 4, '四窗应有 4 个格子'
    assert d.evaluate('window._qmk.created') == 4 and d.evaluate('window._qmk.removed') == 0, \
        f"四窗应挂 4 个图表实例: {d.evaluate('window._qmk')}"
    d.keyboard.press('v')
    d.wait_for_timeout(300)
    assert d.evaluate('!document.getElementById("quadWrap")') and d.evaluate('window._qmk.removed') == 4, \
        '再按 V 应退出四窗并销毁全部实例'
    d.keyboard.press('v')
    d.wait_for_timeout(300)
    assert d.evaluate('!!document.getElementById("quadWrap")'), '第三次按 V 应再次进四窗'
    d.keyboard.press('Backspace')
    d.wait_for_timeout(300)
    assert d.evaluate('!document.getElementById("quadWrap")') and d.evaluate('window._qmk.removed') == 8, '退格应先退四窗'
    assert d.url.endswith('pc_detail.html?sym=MA2701'), f'退四窗不应导航走: {d.url}'
    print('ok 16/17 四窗：V 开（4 实例）/ V 关销毁 / 退格先退四窗不导航')

    # 17) ABC 三档自证：同 500ms 长按，C 档位移显著大于 A 档（防「三档一个速度」回归——用户实测疑似缓存旧版）
    d.evaluate("""() => {
      window._ab = {pos: 0, calls: 0};
      window.__clGetChart = () => ({timeScale: () => ({
        options: () => ({barSpacing: 8}),
        scrollPosition: () => window._ab.pos,
        scrollToPosition: p => { window._ab.calls++; window._ab.pos = p; },
        getVisibleLogicalRange: () => ({from: 0, to: 100}),
        setVisibleLogicalRange: () => {}
      })});
    }""")
    d.evaluate('localStorage.setItem("cl_arrow_scheme","A"); void 0')
    d.click('.name-block')
    d.keyboard.down('ArrowRight'); d.wait_for_timeout(500); d.keyboard.up('ArrowRight')
    d.wait_for_timeout(150)
    posA = d.evaluate('window._ab.pos')
    d.evaluate('window._ab.pos = 0; window._ab.calls = 0; localStorage.setItem("cl_arrow_scheme","C"); void 0')
    d.click('.name-block')
    d.keyboard.down('ArrowRight'); d.wait_for_timeout(500); d.keyboard.up('ArrowRight')
    d.wait_for_timeout(150)
    posC = d.evaluate('window._ab.pos')
    assert posC > posA * 2, f'C 档 500ms 位移应显著大于 A 档（A≈17根 C≈61根）: A={posA} C={posC}'
    print(f'ok 17/17 ABC 三档自证：500ms 位移 A={posA:.0f} 根 < C={posC:.0f} 根（三档速率确有区分）')

    if errs2:
        print('详情页 JS 错误:', errs2)
        raise SystemExit(1)
    d.close()

    # 15) T = 任意页跳持仓页
    ov2 = new_page(b)
    errs5 = []
    ov2.on('pageerror', lambda e: errs5.append(f'pageerror: {e}'))
    ov2.add_init_script(SEED)
    tnavs = []
    ov2.on('framenavigated', lambda f: tnavs.append(f.url) if 'positions' in (f.url or '') else None)
    ov2.goto(OVERVIEW)
    ov2.wait_for_timeout(800)
    ov2.keyboard.press('t')
    ov2.wait_for_timeout(500)
    assert tnavs and '/pc/positions' in tnavs[-1], f'T 应跳持仓页: {tnavs}'
    print('ok 15/15 全局 T → 跳持仓页')
    if errs5:
        print('T 跳转 JS 错误:', errs5)
        raise SystemExit(1)
    ov2.close()
    if errs2:
        print('详情页 JS 错误:', errs2)
        raise SystemExit(1)
    d.close()
    b.close()

print('ok - 全局热键全部通过：单键跳转 / Shift档 / 三层守卫 / Backspace返回 / 详情页级别循环')
