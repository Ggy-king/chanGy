/* 全局键盘热键（2026-10-30 定稿）：
 *  1-9 / 0        → 跳转详情页对应品种（顺序=config.CONTRACTS，第10位=0）
 *  Shift+1..9/0   → 第11-20位品种（未来扩容预留，超出当前清单不动作）
 *  Backspace      → 返回：详情页调 goBack()（直达入口页，跳过切品种历史链），其他页 history.back()
 *  T              → 全局跳持仓页（驾驶舱）
 *  A              → 一键止损布防：详情页直接；持仓页先点模块再按
 *  Enter          → 图表回到实时（详情页）
 *  ?              → 快捷键速查卡（任意页面）
 *  详情页专属（WASD）：Q/E 切级别（循环轮询），W/S 切品种；←→ 平移K线、↑↓ 缩放（连续加速，锚定中心）
 * 守卫：输入框/文本域/下拉/组字中/按住 Cmd·Ctrl·Alt 时全部放行，浏览器与输入法行为不受影响。
 * 自给自足：品种清单拉 /api/contracts 后存 localStorage（TTL 24h），按键零网络等待。
 */
(function(){
  if (window.__clHotkeys) return;
  window.__clHotkeys = true;

  var LIST_KEY = 'cl_hotkey_contracts';   // {ts, list:[{symbol,name}]}
  var TTL = 24 * 3600 * 1000;

  function loadLocal(){
    try{
      var v = JSON.parse(localStorage.getItem(LIST_KEY) || 'null');
      if (v && v.list && v.list.length && (Date.now() - v.ts) < TTL) return v.list;
    }catch(e){}
    return null;
  }
  function saveLocal(list){
    try{ localStorage.setItem(LIST_KEY, JSON.stringify({ts: Date.now(), list: list})); }catch(e){}
  }
  function refreshList(){
    if (location.protocol === 'file:') return;   // E2E 直载不拉网络
    fetch('/api/contracts').then(r => r.json()).then(s => {
      var list = (s.contracts || []).filter(c => c && c.symbol);
      if (list.length) saveLocal(list);
    }).catch(function(){});
  }
  function contracts(){ return loadLocal() || []; }

  function guard(e){
    if (e.metaKey || e.ctrlKey || e.altKey) return true;          // Cmd/Ctrl/Alt 组合键留给浏览器
    if (e.isComposing || e.keyCode === 229) return true;          // 中文输入法组字中
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return true;
    return false;   // false = 不放行，可以处理
  }

  // 序号→品种：1-9 = 第1-9位，0 = 第10位；Shift 档 = 第11-20位
  function symOf(digit, shift){
    var list = contracts();
    var idx = (digit === 0 ? 9 : digit - 1) + (shift ? 10 : 0);
    return (idx < list.length) ? list[idx] : null;
  }

  function goSym(c){
    location.href = '/pc/detail?sym=' + encodeURIComponent(c.symbol);   // 不带 period：自动继承 cl_chart_period
  }

  function goBack(){
    // 四窗开着：先退回单窗不导航（详情页 quadOpen/quadClose 维护 window.quadOn），再按一次才真正返回
    if (window.quadOn && typeof window.quadClose === 'function') { window.quadClose(); return; }
    // 详情页（有 ‹ 返回 按钮的页面）：调它的 goBack()——直达入口页、跳过上下切品种的历史链；
    // 其他页面：浏览器后退兜底，无历史则不动
    if (typeof window.goBack === 'function' && document.querySelector('a[href="javascript:goBack()"]')) {
      window.goBack(); return;
    }
    if (history.length > 1) history.back();
  }

  // ===== 详情页专属（WASD 手位 + 方向键图表平移缩放）=====
  // Q/E = 切级别（循环轮询：日线→30秒绕回）；W/S = 切品种；←→ = 平移K线，↑↓ = 缩放（锚定可视区中心）
  // 识别方式：页面有 #periodSeg 且有 switchPeriod 函数；点击 seg 按钮复用页面自身逻辑（含级别持久化）
  function cyclePeriod(dir){
    var btns = Array.prototype.slice.call(document.querySelectorAll('#periodSeg button'));
    if (!btns.length || typeof window.switchPeriod !== 'function') return false;
    var cur = 0;
    for (var i = 0; i < btns.length; i++) if (btns[i].classList.contains('on')) { cur = i; break; }
    var b = btns[(cur + dir + btns.length) % btns.length];
    if (b) b.click();
    return true;
  }

  // ===== 方向键平移/缩放：连续加速模型（游戏相机式——速度渐起、松手即停、帧率无关）=====
  // ABC 方案在详情页 ⚙ 图表设置选择（存 localStorage cl_arrow_scheme，点击即刻生效）；缩放一律锚定可视区中心
  var ZOOM_MIN = 8, ZOOM_MAX = 5000;
  var ARROW_SCHEMES = {
    A: { v0: 130, acc: 550,  vmax: 700,  rate: 0.7, desc: '起步慢 · 加速温和 · 顶速700px/s —— 精确定位用' },
    B: { v0: 260, acc: 1000, vmax: 1500, rate: 1.3, desc: '起步适中 · 顶速1500px/s —— 日常盯盘用' },
    C: { v0: 420, acc: 2200, vmax: 3200, rate: 2.2, desc: '起步快 · 顶速3200px/s —— 扫历史行情用' },
  };
  function arrowScheme(){
    try { var s = localStorage.getItem('cl_arrow_scheme'); if (s && ARROW_SCHEMES[s]) return s; } catch (e) {}
    return 'B';
  }
  function getChart(){
    if (window.__clGetChart) return window.__clGetChart();   // 测试钩子
    try { return (typeof chart !== 'undefined' && chart && chart.timeScale) ? chart : null; }
    catch (e) { return null; }   // 页面脚本未跑完时 let 声明处于 TDZ
  }
  var _arr = {left:false, right:false, up:false, down:false, raf:0, t0:0, last:0, cfg:null};
  function arrowTick(){
    var c = getChart();
    if (!c || !( _arr.left || _arr.right || _arr.up || _arr.down )) { _arr.raf = 0; return; }
    var now = performance.now(), dt = Math.min(0.06, (now - _arr.last) / 1000);   // 帧率无关
    _arr.last = now;
    var ts = c.timeScale();
    // 平移：px 速度按 barSpacing 折算成根数；K线跟着箭头方向滑——← = K线左移露出更新的数据，→ = K线右移看历史（到头自动停）
    if (_arr.left || _arr.right) {
      var v = Math.min(_arr.cfg.vmax, _arr.cfg.v0 + _arr.cfg.acc * (now - _arr.t0) / 1000);
      var bars = v * dt / Math.max(1, (ts.options ? (ts.options().barSpacing || 8) : 8));
      var dir = (_arr.right ? 1 : 0) - (_arr.left ? 1 : 0);
      ts.scrollToPosition((ts.scrollPosition() || 0) + dir * bars, false);
    }
    // 缩放：可视根数按 e 指数变化；↑ = 放大（根数变少），↓ = 缩小；锚定可视区中心
    if (_arr.up || _arr.down) {
      var r = ts.getVisibleLogicalRange();
      if (r) {
        var zdir = (_arr.up ? 1 : 0) - (_arr.down ? 1 : 0);
        var span = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, (r.to - r.from) * Math.exp(-zdir * _arr.cfg.rate * dt)));
        var center = (r.from + r.to) / 2;
        ts.setVisibleLogicalRange({ from: center - span / 2, to: center + span / 2 });
      }
    }
  }
  function arrowLoop(){
    arrowTick();
    if (_arr.raf) _arr.raf = requestAnimationFrame(arrowLoop);   // arrowTick 无按键时已清 raf=0 → 循环自然退出
    else _arr.raf = 0;
  }
  function arrowStart(){ if (!_arr.raf) { _arr.cfg = ARROW_SCHEMES[arrowScheme()]; _arr.t0 = _arr.last = performance.now(); _arr.raf = requestAnimationFrame(arrowLoop); } }

  // ===== `?` 快捷键速查卡（任意页面）=====
  function toggleCheat(){
    var ov = document.getElementById('clCheat');
    if (ov) { ov.remove(); return; }
    ov = document.createElement('div');
    ov.id = 'clCheat';
    ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;';
    var rows = [
      ['1-9 / 0', '跳转热键区品种'],
      ['Shift+1 ~ 0', '跳转 11-20 位品种'],
      ['T', '跳转持仓页（驾驶舱）'],
      ['退格', '返回入口页'],
      ['A', '一键止损布防（详情页直接；持仓页先点模块）'],
      ['Q / E', '切级别（循环轮询）'],
      ['W / S', '切品种'],
      ['V', '四窗 · 多级别同屏（详情页，60分/15分/3分/30秒）'],
      ['← / →', '平移K线（长按加速）'],
      ['↑ / ↓', '缩放（长按加速，锚定中心）'],
      ['Enter', '图表回到实时'],
      ['?', '本速查卡'],
      ['Esc', '关面板 / 取消'],
    ];
    var html = '<div style="background:#1e222d;border:1px solid #2a2e39;border-radius:14px;padding:18px 22px;min-width:360px;box-shadow:0 18px 60px rgba(0,0,0,.7);">'
      + '<div style="font-size:15px;font-weight:800;color:#f0b90b;letter-spacing:1px;margin-bottom:12px;">⌨ 快捷键速查 <span style="font-size:11px;color:#787b86;font-weight:400">Esc 或点空白处关闭</span></div>';
    rows.forEach(function(r){
      html += '<div style="display:flex;justify-content:space-between;gap:24px;padding:5px 0;border-bottom:1px solid rgba(42,46,57,.6);font-size:12.5px;">'
        + '<span style="color:#f0b90b;font-weight:700;font-family:Menlo,monospace;white-space:nowrap;">' + r[0] + '</span>'
        + '<span style="color:#d1d4dc;text-align:right;">' + r[1] + '</span></div>';
    });
    html += '</div>';
    ov.innerHTML = html;
    ov.addEventListener('click', function(ev){ if (ev.target === ov) toggleCheat(); });
    document.body.appendChild(ov);
  }

  // 持仓页槽位点选追踪：点过哪个模块，A 就布防哪个
  var _lastSlot = -1;
  document.addEventListener('click', function(e){
    var w = document.getElementById('wrap');
    if (!w) { _lastSlot = -1; return; }
    var strips = w.querySelectorAll(':scope > .strip');
    for (var i = 0; i < strips.length; i++) if (strips[i].contains(e.target)) { _lastSlot = i; return; }
  }, true);

  document.addEventListener('keydown', function(e){
    if (e.key === '?' && !guard(e)) { toggleCheat(); e.preventDefault(); return; }
    if (e.key === 'Escape' && document.getElementById('clCheat')) { document.getElementById('clCheat').remove(); return; }
    // T = 全局跳持仓页（已在持仓页则不动）
    if ((e.key === 't' || e.key === 'T') && !guard(e) && location.pathname !== '/pc/positions') {
      location.href = '/pc/positions'; e.preventDefault(); return;
    }
    if (document.getElementById('periodSeg') && !guard(e)) {
      if (e.key === 'q' || e.key === 'Q') { if (cyclePeriod(-1)) { e.preventDefault(); return; } }
      if (e.key === 'e' || e.key === 'E') { if (cyclePeriod(1)) { e.preventDefault(); return; } }
      if ((e.key === 'w' || e.key === 'W') && typeof window.prevContract === 'function') { window.prevContract(); e.preventDefault(); return; }
      if ((e.key === 's' || e.key === 'S') && typeof window.nextContract === 'function') { window.nextContract(); e.preventDefault(); return; }
      // V = 四窗多级别同屏（详情页）：60分/15分/3分/30秒 2×2，再按 V 或退格退出
      if ((e.key === 'v' || e.key === 'V') && typeof window.quadToggle === 'function') { window.quadToggle(); e.preventDefault(); return; }
      // A = 一键止损布防：详情页直接触发；持仓页布防最后点选的槽位
      if (e.key === 'a' || e.key === 'A') {
        if (typeof window.startSlSet === 'function' && document.getElementById('ctxMenu')) { window.startSlSet(); e.preventDefault(); return; }
        if (typeof window.slStartSetKeyed === 'function' && _lastSlot >= 0 && window.slStartSetKeyed(_lastSlot)) { e.preventDefault(); return; }
      }
      // Enter = 图表回到实时
      var _c = getChart();
      if (e.key === 'Enter' && _c && _c.timeScale().scrollToRealTime) { _c.timeScale().scrollToRealTime(); e.preventDefault(); return; }
      // 方向键：连续平移/缩放（有图表才拦，避免吃掉无图页面的滚动）
      var hasChart = !!getChart();
      if (hasChart && (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        if (e.key === 'ArrowLeft') _arr.left = true;
        else if (e.key === 'ArrowRight') _arr.right = true;
        else if (e.key === 'ArrowUp') _arr.up = true;
        else _arr.down = true;
        e.preventDefault();
        arrowStart();
        return;
      }
    }
    if (e.key === 'Backspace' && !guard(e)) { goBack(); e.preventDefault(); return; }
    // 注意：Shift+1 的 e.key 是 '!'，必须用 e.code（主键盘 Digit1-0 / 小键盘 Numpad0-9）
    var m = /^(?:Digit|Numpad)([0-9])$/.exec(e.code);
    if (!m) return;
    if (guard(e)) return;
    var c = symOf(+m[1], e.shiftKey);
    if (c) { goSym(c); e.preventDefault(); }
  }, false);

  // 松手即停：清方向键状态，rAF 循环自行退出
  document.addEventListener('keyup', function(e){
    if (e.key === 'ArrowLeft') _arr.left = false;
    else if (e.key === 'ArrowRight') _arr.right = false;
    else if (e.key === 'ArrowUp') _arr.up = false;
    else if (e.key === 'ArrowDown') _arr.down = false;
  }, false);

  refreshList();
})();
