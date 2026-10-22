/* 右上角预警弹卡 · 全局组件（2026-10-12 用户定稿；2026-10-13 扩 4 色+持仓专属色；2026-10-16 跨页面存活；
   2026-10-22 升级为自给自足组件——跨浏览器/跨页面弹卡）：
   —— 任何页面只需 <script src="/popup_cards.js"></script> 一行，即获得全套弹卡：
   · 自带 WS 连接：pattern_alert / pattern_alert_update 广播自动弹卡（原逻辑在监控页，
     别的页面没有这段代码所以永远不弹——驾驶舱不弹卡即此原因）
   · 跨浏览器补弹：服务端 WS 会给每个新连接补发最近 30 条历史（init.history），
     组件消化其中 3 分钟内的事件 → 晚打开的标签页/另一台电脑也能看到卡（静默补弹不响铃）
   · 本浏览器"已看过"指纹存 localStorage(cl_popup_seen，10 分钟过期)——
     同一浏览器不重复轰炸，不同浏览器各弹一次；posbi 确认笔卡（服务端持久+必须确认）另有组件
   · 响铃/系统通知跟随监控页的静音按钮（cl_matrix_mute；监控页切换时广播 pc_mute 事件实时同步）
   · 多卡叠叠乐：新事件往上叠、每张卡完整存在 12 秒；同指纹原位刷新重新计时
   · 卡片多时弹卡区可滚动；点卡片跳转由调用方通过 onClick 或 href 指定；× 直接关闭
   · 跨页面存活：活卡写入 sessionStorage（本标签页），跳转/返回自动恢复，倒计时连续
   用法：PopupCards.push({ fp, title, info, tone, onClick, href, ms })
   tone 五色（与矩阵图例一致）：up=红(A多/涨) down=青(A空/跌) gold=金(B多/三买)
        purple=紫(B空/三卖) pos=蓝(持仓专属：离场预警/升级提示) */
const PopupCards = (function () {
  const MS = 12000;
  const KEY = 'pc_popups';
  let stack = null, pops = {};   // fp -> {el, timer}

  /* —— sessionStorage 存取（本标签页内跨页面）—— */
  function _load() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) { return []; }
  }
  function _save(arr) {
    try { sessionStorage.setItem(KEY, JSON.stringify(arr)); } catch (e) {}
  }
  function _store(rec) {          // 按指纹 upsert，顺手清掉已过期条目
    const arr = _load().filter(r => r.fp !== rec.fp && r.until > Date.now());
    arr.push(rec); _save(arr);
  }
  function _unstore(fp) {
    _save(_load().filter(r => r.fp !== fp));
  }

  function css() {
    if (document.getElementById('pcPopStyle')) return;
    const s = document.createElement('style');
    s.id = 'pcPopStyle';
    s.textContent = `
#popStack{position:fixed;top:20px;right:20px;z-index:150;display:flex;flex-direction:column;gap:10px;
  pointer-events:none;max-width:min(340px,calc(100vw - 24px));max-height:calc(100vh - 40px);
  overflow-y:auto;scrollbar-width:none;}
#popStack::-webkit-scrollbar{display:none;}
.alert-pop{pointer-events:auto;position:relative;background:#1e222d;border:2px solid #ef5350;border-radius:12px;
  padding:11px 30px 11px 16px;box-shadow:0 8px 30px #000a;cursor:pointer;font-size:12.5px;
  animation:pcSlide .25s ease;}
.alert-pop.down{border-color:#26a69a;}
.alert-pop.gold{border-color:#f0b90b;}
.alert-pop.purple{border-color:#b388ff;}
.alert-pop.pos{border-color:#4a9eff;background:#16202e;box-shadow:0 8px 30px #000a,0 0 0 1px rgba(74,158,255,.25);}
.alert-pop .t{font-size:14px;font-weight:700;margin-bottom:4px;}
.alert-pop .t.up{color:#ef5350;} .alert-pop .t.down{color:#26a69a;}
.alert-pop .t.gold{color:#f0b90b;} .alert-pop .t.purple{color:#b388ff;}
.alert-pop .t.pos{color:#4a9eff;}
.alert-pop .i{color:#9aa0b0;line-height:1.65;word-break:break-all;}
.alert-pop .pp-main{font-size:13px;color:#d1d4dc;}
.alert-pop .pp-pill{display:inline-block;font-size:10.5px;padding:1px 8px;border-radius:9px;margin-left:6px;vertical-align:1px;font-weight:700;}
.alert-pop .pp-g{background:rgba(240,185,11,.16);border:1px solid #f0b90b;color:#f0b90b;}
.alert-pop .pp-b{background:rgba(74,158,255,.14);border:1px solid #4a9eff;color:#4a9eff;}
.alert-pop .pp-txt{color:#787b86;font-size:11.5px;}
.alert-pop .x{position:absolute;top:5px;right:10px;color:#787b86;cursor:pointer;font-size:16px;}
@keyframes pcSlide{from{transform:translateX(110%);opacity:0;}to{transform:translateX(0);opacity:1;}}`;
    document.head.appendChild(s);
  }

  function ensureStack() {
    if (stack && document.body.contains(stack)) return stack;
    stack = document.getElementById('popStack');
    if (!stack) { stack = document.createElement('div'); stack.id = 'popStack'; document.body.appendChild(stack); }
    return stack;
  }

  let _onDismiss = null;          // 全局叉掉回调（组件层设置 → 经服务端广播同步所有端）
  function _kill(fp, byUser) {    // 到期 / 手动 ×：卡片与存档一起清
    const rec = pops[fp];
    if (rec) { clearTimeout(rec.timer); rec.el.remove(); delete pops[fp]; }
    _unstore(fp);
    if (byUser && _onDismiss) { try { _onDismiss(fp); } catch (e) {} }
  }

  function _render(fp, tone, title, info, href, ms) {
    css();                                    // 恢复路径也要注入样式（push 之外的唯一入口）
    const box = ensureStack();
    const el = document.createElement('div');
    el.className = 'alert-pop ' + tone;      // 未知 tone 无边框色 → 兜底 up
    if (!/^(up|down|gold|purple|pos)$/.test(tone)) el.classList.add('up');
    el.onclick = href ? function () { location.href = href; } : function () {};
    const x = document.createElement('span');
    x.className = 'x'; x.textContent = '×';
    x.onclick = function (e) { e.stopPropagation(); _kill(fp, true); };
    el.appendChild(x);
    const t = document.createElement('div'); t.className = 't ' + tone;
    const i = document.createElement('div'); i.className = 'i';
    t.textContent = title || ''; i.innerHTML = info || '';
    el.appendChild(t); el.appendChild(i);
    box.prepend(el);                          // 新卡在最上，老卡依次下挤
    const rec = pops[fp] = { el: el };
    rec.timer = setTimeout(function () { _kill(fp); }, ms);
    return rec;
  }

  function push(o) {
    if (!document.body) return;
    css();
    const fp = o.fp || ('k_' + Math.random());
    const ms = o.ms || MS;
    _store({ fp: fp, tone: o.tone || 'up', title: o.title || '', info: o.info || '',
             href: o.href || '', until: Date.now() + ms });
    const rec = pops[fp];
    if (rec) {                                // 延续触发：原卡刷新、重新计时
      clearTimeout(rec.timer);
      rec.el.querySelector('.t').textContent = o.title || '';
      rec.el.querySelector('.i').innerHTML = o.info || '';
      rec.timer = setTimeout(function () { _kill(fp); }, ms);
    } else {
      _render(fp, o.tone, o.title, o.info, o.href || '', ms);
    }
  }

  /* —— 页面加载时恢复未到期卡片（不响铃；点击走 href）—— */
  function _restore() {
    const now = Date.now();
    const arr = _load();
    const alive = arr.filter(r => r.until > now);
    if (alive.length !== arr.length) _save(alive);   // 顺手清掉已过期
    alive.forEach(function (r) {
      _render(r.fp, r.tone, r.title, r.info, r.href, Math.max(1000, r.until - now));
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _restore);
  else _restore();

  return { push: push,
           close: function (fp) { _kill(fp, false); },       // 远端同步关闭（不触发 dismiss 回调）
           setDismiss: function (fn) { _onDismiss = fn; } };
})();

/* ==================== 跨浏览器自给自足层（2026-10-22） ====================
   自带 WS：收 pattern_alert / pattern_alert_update 自动弹卡（弹卡+响铃+闪标题+系统通知
   +持仓离场预警/可升级弹卡，原逻辑从监控页收编）。
   init.history 消化：晚打开的页面/浏览器把 3 分钟内的事件静默补弹（本地已看指纹去重）。 */
(function () {
  if (window.__popupCardsLive) return;
  window.__popupCardsLive = true;

  const P_LAB = {'3': '3分', '5': '5分', '15': '15分', '30m': '30分', '60': '60分'};
  const P_RANK = {'3': 3, '5': 5, '15': 15, '30m': 30, '60': 60};
  const fmtT = t => t ? String(t).replace('T', ' ').slice(5, 16) : '';
  const isLong = p => (p.entry || '').includes('买');

  /* —— 静音：沿用监控页的键，监控页切换时广播 pc_mute 事件实时同步 —— */
  let muted = false;
  try { muted = localStorage.getItem('cl_matrix_mute') === '1'; } catch (e) {}
  window.addEventListener('pc_mute', function (e) { muted = !!(e.detail); });
  let snd = { conf: 1200, cand: 700 };
  try { const s = JSON.parse(localStorage.getItem('cl_matrix_snd') || 'null'); if (s && s.conf && s.cand) snd = s; } catch (e) {}

  /* —— 本浏览器已看指纹（10 分钟过期；跨标签页去重）—— */
  const SEEN = 'cl_popup_seen';
  function seenGet() { try { return JSON.parse(localStorage.getItem(SEEN) || '{}'); } catch (e) { return {}; } }
  function seenMark(fp) {
    const o = seenGet(), now = Date.now();
    o[fp] = now + 600000;
    for (const k in o) if (o[k] < now) delete o[k];
    try { localStorage.setItem(SEEN, JSON.stringify(o)); } catch (e) {}
  }
  function seenHas(fp) { return !!seenGet()[fp]; }

  /* —— 时效闸门（与监控页同哲学）：触发时刻距今超 3 分钟 = 断线窗口的补发，不弹 —— */
  function evIsFresh(m) {
    try {
      const t = String(m.trigger_time || m.time || '').replace(' ', 'T');
      if (!t) return true;
      return (Date.now() - new Date(t).getTime()) / 1000 <= 180;
    } catch (e) { return true; }
  }

  /* —— 声音锁（2026-10-22）：同一浏览器多标签只有一个声源，同一事件不叠加响 ——
     指纹锁 2.5 秒有效 + 随机抖动错峰；不同电脑各自响（物理上就是两个声源）—— */
  function beepLock(fp) {
    try {
      const K = 'cl_beep_lock', now = Date.now();
      const o = JSON.parse(localStorage.getItem(K) || '{}');
      if (o.fp === fp && now - o.ts < 2500) return false;
      localStorage.setItem(K, JSON.stringify({ fp: fp, ts: now }));
      return true;
    } catch (e) { return true; }
  }

  /* —— 提示音（确认/触发=上行双音，候选=单长音）；force=true 供监控页声音测试面板绕过静音 —— */
  function beepTone(freq, conf, force) {
    if (muted && !force) return;
    try {
      const ctx = window.__audio || (window.__audio = new (window.AudioContext || window.webkitAudioContext)());
      const seq = conf ? [[0, freq], [0.16, freq * 1.3]] : [[0, freq]];
      seq.forEach(([dt, fr]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = conf ? 'square' : 'sine'; o.frequency.value = fr;
        const t0 = ctx.currentTime + dt;
        g.gain.setValueAtTime(.001, t0);
        g.gain.exponentialRampToValueAtTime(.25, t0 + .02);
        g.gain.exponentialRampToValueAtTime(.001, t0 + (conf ? .14 : .3));
        o.start(t0); o.stop(t0 + (conf ? .16 : .32));
      });
    } catch (e) {}
  }
  const isConf = m => (m.pattern || '').indexOf('CONF') >= 0 || (m.pattern || '').indexOf('TRIG') >= 0;
  function beepFor(m) { beepTone(isConf(m) ? snd.conf : snd.cand, isConf(m)); }

  /* —— 标题闪烁 + 系统通知 —— */
  let _titleTimer = null, _baseTitle = '';
  function flashTitle(txt) {
    if (_titleTimer) clearInterval(_titleTimer);
    _baseTitle = _baseTitle || document.title;
    let on = true;
    _titleTimer = setInterval(function () {
      document.title = (on ? '🔴 ' + txt : _baseTitle); on = !on;
    }, 700);
    setTimeout(function () { clearInterval(_titleTimer); document.title = _baseTitle; _titleTimer = null; }, 10000);
  }
  /* —— 系统通知：tag=指纹（延续触发原位更新同一条系统通知，不堆叠）；测试通知带时间戳（每次都是新条）——
     用户 2026-10-22 发现固定 tag 会让第二条覆盖第一条：连续预警各占一条，不互相覆盖 —— */
  function notifyTxt(title, body, tag) {
    if (muted || !('Notification' in window) || Notification.permission !== 'granted') return;
    try { new Notification(title || '形态预警', { body: (body || '').replace(/<[^>]+>/g, ''), tag: tag || ('changy_' + Date.now() + Math.random()) }); } catch (e) {}
  }
  function notify(m) { notifyTxt('形态预警', m.text, 'changy_' + m.fingerprint); }

  /* —— 持仓弹卡：离场预警 / 可升级（原监控页 posPopups 收编，持仓列表 30s 刷新）—— */
  let POSITIONS = [];
  function loadPositions() {
    try {
      fetch('/api/positions').then(r => r.json()).then(d => { POSITIONS = d.positions || []; }).catch(function () {});
    } catch (e) {}
  }
  function posPopups(m, live) {
    if (!POSITIONS.length || !isConf(m)) return;
    POSITIONS.forEach(function (p) {
      const long = isLong(p), exitSide = long ? 'short' : 'long', side = (m.side || 'long');
      const det = function () { location.href = '/pc/detail?sym=' + encodeURIComponent(m.symbol); };
      const silent = !live;   // 补弹的持仓卡同样静默
      if (m.symbol === p.symbol && String(m.period) === String(p.period) && side === exitSide) {
        PopupCards.push({ fp: 'posx_' + m.fingerprint, tone: 'pos', onClick: det,
          href: '/pc/detail?sym=' + encodeURIComponent(m.symbol),
          title: '📌 离场预警 · ' + p.name + ' ' + p.symbol + '（' + (P_LAB[p.period] || p.period) + '持仓）',
          info: (m.kind === 'A' ? 'A类' : 'B类') + (long ? '卖点' : '买点') + (m.kind === 'A' ? '确认' : '触发（无条件全平）') + (m.bi_formed ? ' · 已成笔' : '') + '<br>价 <b>' + m.price + '</b>　' + fmtT(m.trigger_time || m.time) });
        if (!silent && beepLock('posx_' + m.fingerprint)) {
          beepFor(m);
          notifyTxt('📌 离场预警 · ' + p.name + ' ' + p.symbol, (m.kind === 'A' ? 'A类' : 'B类') + (long ? '卖点' : '买点') + (m.kind === 'A' ? '确认' : '触发（无条件全平）') + ' @' + m.price);
        }
      }
      const r = P_RANK[m.period] || 0;
      if (m.symbol === p.symbol && r > (P_RANK[p.period] || 0) && side === (long ? 'long' : 'short')) {
        PopupCards.push({ fp: 'posu_' + m.fingerprint, tone: 'pos', onClick: det,
          href: '/pc/detail?sym=' + encodeURIComponent(m.symbol),
          title: '📌 可升级 · ' + p.name + ' → ' + (P_LAB[m.period] || m.period),
          info: '更大级别同向' + (m.kind === 'A' ? 'A类确认' : 'B类触发') + '，本级别卖点后可留底仓过户' + (P_LAB[m.period] || m.period) + '<br>价 <b>' + m.price + '</b>　' + fmtT(m.trigger_time || m.time) });
        if (!silent && beepLock('posu_' + m.fingerprint)) {
          beepFor(m);
          notifyTxt('📌 可升级 · ' + p.name + ' → ' + (P_LAB[m.period] || m.period), '更大级别同向' + (m.kind === 'A' ? 'A类确认' : 'B类触发') + ' @' + m.price);
        }
      }
    });
  }

  /* —— 组装通用形态卡（原监控页 showPatternPopup）—— */
  function showPatternPopup(m, isUpdate) {
    const long = (m.side || 'long') === 'long';
    const st = (m.pattern || '').indexOf('CONF') >= 0 ? '确认' : (m.pattern || '').indexOf('TRIG') >= 0 ? '触发' : '候选';
    const kd = (m.kind === 'A' ? 'A类' : 'B类') + (long ? '买点' : '卖点');
    PopupCards.push({
      fp: m.fingerprint,
      tone: (m.kind === 'B') ? (long ? 'gold' : 'purple') : (long ? 'up' : 'down'),
      title: (m.name || m.symbol || '') + ' ' + (P_LAB[m.period] || m.period || ''),
      info: '<b class="pp-main">' + kd + ' · ' + st + '</b>' +
        (m.bi_formed ? '<span class="pp-pill pp-g">✅已成笔</span>' : '') +
        (isUpdate ? '<span class="pp-pill pp-b">🔔延续</span>' : '') +
        '<br>价 <b>' + m.price + '</b>　触发 ' + fmtT(m.trigger_time || m.time) +
        '<br><span class="pp-txt">' + (m.text || '') + '</span>',
      href: '/pc/detail?sym=' + encodeURIComponent(m.symbol)
    });
  }

  /* —— 候选类开关（监控页提示音面板，2026-10-22）：只影响候选/观察类；确认/触发类始终弹+响 —— */
  function lsOn(k) { try { return localStorage.getItem(k) !== '0'; } catch (e) { return true; } }

  /* —— 事件入口：live=WS 实时（有声），否则=init.history 补弹（静默）—— */
  function ingest(m, live, isUpdate) {
    if (!m || !m.fingerprint) return;
    const fresh = evIsFresh(m);
    const conf = isConf(m);
    if (live) {
      if (m.type === 'pattern_alert' && m.first === false) return;   // 服务端标记的非首推
      if (!fresh && !(m.type === 'pattern_alert_update' && m.bi_formed)) return;  // 成笔补报绕过时效闸门
      seenMark(m.fingerprint);
      const showCard = conf || lsOn('cl_popup_cand_card');
      const doBeep = conf || lsOn('cl_popup_cand_beep');
      if (showCard) showPatternPopup(m, isUpdate);
      posPopups(m, true);
      const fp = m.fingerprint;
      setTimeout(function () {   // 同浏览器多标签只有一个声源：指纹锁 + 随机抖动错峰
        if (doBeep && beepLock(fp)) { beepFor(m); notify(m); flashTitle((m.text || '').replace(/<[^>]+>/g, '') || '形态预警'); }
      }, Math.random() * 180);
    } else {
      if (seenHas(m.fingerprint) || !fresh) return;   // 本浏览器看过/太旧：不补
      if (!conf && !lsOn('cl_popup_cand_card')) return;   // 候选类弹卡关：补弹也跳过
      seenMark(m.fingerprint);
      showPatternPopup(m, m.type === 'pattern_alert_update');
      posPopups(m, false);
    }
  }
  PopupCards._ingest = ingest;   // 测试钩子（E2E 直接注入事件验证弹卡链路）
  PopupCards.beep = function (freq, conf) { beepTone(freq, conf, true); };   // 监控页声音测试面板用

  /* —— 全局叉掉（微信式单一数据源）：任一端 × → 服务端广播 → 所有端同步消失 —— */
  function applyDismiss(fp) {
    [fp, 'posx_' + fp, 'posu_' + fp].forEach(function (f) { seenMark(f); PopupCards.close(f); });
  }
  PopupCards.setDismiss(function (fp) {
    seenMark(fp); seenMark('posx_' + fp); seenMark('posu_' + fp);
    try {
      fetch('/api/popups/dismiss', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fp: fp }) }).catch(function () {});
    } catch (e) {}
  });

  /* —— 自带 WS（role=pc，4 秒重连）—— */
  let ws = null;
  function connect() {
    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + '/ws?role=pc');
      ws.onmessage = function (e) {
        let m; try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.type === 'pattern_alert') ingest(m, true, false);
        else if (m.type === 'pattern_alert_update') ingest(m, true, true);
        else if (m.type === 'popup_dismiss') applyDismiss(m.fp);
        else if (m.type === 'init' && m.history) {
          m.history.filter(function (x) { return x.type === 'popup_dismiss'; })
            .forEach(function (x) { applyDismiss(x.fp); });   // 先消化叉掉记录，再补弹（被叉的卡不复活）
          m.history.forEach(function (x) {
            if (x.type === 'pattern_alert' || x.type === 'pattern_alert_update') ingest(x, false);
          });
        }
      };
      ws.onclose = function () { setTimeout(connect, 4000); };
    } catch (e) { setTimeout(connect, 4000); }
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', function () { loadPositions(); connect(); });
  else { loadPositions(); connect(); }
  setInterval(loadPositions, 30000);
})();
