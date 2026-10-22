/* 左上角持仓确认笔强制提醒 · 全局组件（2026-10-21 定稿）
   —— 与右上角 popup_cards 独立的一套：无过期时间、跨页面/跨设备存活、必须点"确认"才消。
   · 数据存服务端 data/posbi.json（alerts=未确认列表），任何页面打开即拉取渲染，
     所以切换页面、跳详情页、重开浏览器，卡都在——直到你在任意一端点"确认"。
   · WS 实时：posbi_new 新卡 / posbi_ack 任意一端确认后全网消失 / posbi_switch 开关同步。
   · 点卡片本体 → 该品种详情页；点"✔ 确认" → POST /api/posbi/ack。
   · 开关在持仓面板头部（pc_matrix）：开=扫描并提醒；关=停止扫描并清空未确认卡。
   用法：页面 <script src="/posbi_cards.js"></script> 一行引入即可，无需其它代码。 */
const PosBI = (function () {
  let stack = null, cards = {}, ws = null, sw = false, closed = false;
  const P_LAB = { '3': '3分', '5': '5分', '15': '15分', '30m': '30分', '60': '60分' };

  function css() {
    if (document.getElementById('posbiStyle')) return;
    const s = document.createElement('style');
    s.id = 'posbiStyle';
    s.textContent = `
#posbiStack{position:fixed;top:20px;left:20px;z-index:160;display:flex;flex-direction:column;gap:10px;
  pointer-events:none;max-width:min(320px,calc(100vw - 24px));max-height:calc(100vh - 40px);
  overflow-y:auto;scrollbar-width:none;}
#posbiStack::-webkit-scrollbar{display:none;}
.posbi-pop{pointer-events:auto;position:relative;background:#1e222d;border:2px solid #f0b90b;border-radius:12px;
  padding:11px 14px 12px;box-shadow:0 8px 30px #000a;cursor:pointer;font-size:12.5px;
  animation:pbSlide .25s ease;}
.posbi-pop.up{border-color:#ef5350;}
.posbi-pop.down{border-color:#26a69a;}
.posbi-pop .t{font-size:14px;font-weight:800;margin-bottom:4px;color:#f0b90b;}
.posbi-pop.up .t{color:#ef5350;} .posbi-pop.down .t{color:#26a69a;}
.posbi-pop .i{color:#9aa0b0;line-height:1.7;word-break:break-all;}
.posbi-pop .i b{color:#d1d4dc;font-size:14px;}
.posbi-pop .mut{color:#56606f;font-size:11px;}
.posbi-pop .ack{margin-top:8px;width:100%;border:none;border-radius:8px;padding:7px 0;
  background:#2a2340;border:1px solid #b388ff;color:#b388ff;font-size:12.5px;font-weight:800;cursor:pointer;}
.posbi-pop .ack:hover{background:#3a2f5c;color:#d1c4ff;}
@keyframes pbSlide{from{transform:translateX(-110%);opacity:0;}to{transform:translateX(0);opacity:1;}}`;
    document.head.appendChild(s);
  }

  function ensureStack() {
    if (stack && document.body.contains(stack)) return stack;
    stack = document.getElementById('posbiStack');
    if (!stack) { stack = document.createElement('div'); stack.id = 'posbiStack'; document.body.appendChild(stack); }
    return stack;
  }

  function kill(id) {
    if (cards[id]) { cards[id].remove(); delete cards[id]; }
  }

  function render(a) {
    if (!a || !a.id || cards[a.id] || !document.body) return;
    css();
    const el = document.createElement('div');
    el.className = 'posbi-pop ' + (a.dir === 'up' ? 'up' : 'down');
    el.onclick = function () {
      location.href = '/pc/detail?sym=' + encodeURIComponent(a.sym) + '&period=' + a.period;
    };
    const t = document.createElement('div');
    t.className = 't';
    t.textContent = '🆕 确认笔 · ' + (a.name || a.sym) + ' ' + (P_LAB[a.period] || a.period);
    const i = document.createElement('div');
    i.className = 'i';
    i.innerHTML = (a.dir === 'up' ? '▲ 向上笔成立' : '▼ 向下笔成立') +
      '　端点 <b>' + a.price + '</b><br><span class="mut">' + (a.time || '') + ' · 点卡片看图 · 点"确认"消卡</span>';
    const ack = document.createElement('button');
    ack.className = 'ack';
    ack.textContent = '✔ 确认（看完点我，卡才消）';
    ack.onclick = function (e) {
      e.stopPropagation();
      kill(a.id);
      fetch('/api/posbi/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: a.id }) }).catch(function () {});
    };
    el.appendChild(t); el.appendChild(i); el.appendChild(ack);
    ensureStack().appendChild(el);
    cards[a.id] = el;
  }

  function clearAll() { Object.keys(cards).forEach(kill); }

  function applySwitch(on, alerts) {
    sw = !!on;
    if (!sw) clearAll();                       // 关闭 → 未确认卡一并清掉（功能都关了别再飘着）
    else (alerts || []).forEach(render);
    paintBtn();
  }

  function paintBtn() {
    const btn = document.getElementById('posbiBtn');
    if (!btn) return;
    btn.textContent = sw ? '🖊 确认笔提醒 开' : '🖊 确认笔提醒 关';
    btn.style.color = sw ? '#26a69a' : '#787b86';
    btn.style.borderColor = sw ? 'rgba(38,166,154,.5)' : '#2a2e39';
  }

  function connect() {
    if (closed) return;
    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + '/ws?role=pc');
      ws.onmessage = function (ev) {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.type === 'posbi_new' && m.alert) {
          render(m.alert);
          try {   // 跨应用系统横幅（2026-10-22）：确认笔卡必须确认才消，人不在页面时也得知道
            if ('Notification' in window && Notification.permission === 'granted')
              new Notification('🖊 确认笔 · ' + m.alert.name + ' ' + (P_LAB[m.alert.period] || m.alert.period),
                { body: (m.alert.dir === 'up' ? '▲ 向上笔' : '▼ 向下笔') + ' 端点 ' + m.alert.price + ' —— 点确认才消', tag: 'posbi_' + m.alert.id });
          } catch (e) {}
        }
        else if (m.type === 'posbi_ack' && m.id) kill(m.id);        // 任意一端确认 → 全部消
        else if (m.type === 'posbi_switch') applySwitch(m.on, m.alerts);
      };
      ws.onclose = function () { setTimeout(connect, 5000); };      // 断线自动重连
    } catch (e) { setTimeout(connect, 5000); }
  }

  async function init() {
    try {
      const r = await fetch('/api/posbi');
      const d = await r.json();
      applySwitch(!!d.switch, d.alerts || []);
    } catch (e) {}
    connect();
  }

  async function toggle(btn) {
    const on = !sw;
    try {
      const r = await fetch('/api/posbi/switch', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: on }) });
      const d = await r.json();
      applySwitch(!!d.switch, d.alerts || []);
    } catch (e) { alert('切换失败: ' + e); }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
  window.addEventListener('beforeunload', function () { closed = true; });

  return { toggle: toggle, isOn: function () { return sw; } };
})();
