/* 右上角预警弹卡 · 全局组件（2026-10-12 用户定稿；2026-10-13 扩 4 色+持仓专属色；2026-10-16 跨页面存活）
   —— 监控页 / 详情页 / 列表页共用一份代码一套样式（以监控页版为基准）：
   · 多卡叠叠乐：新事件往上叠、不顶掉老卡，每张卡完整存在 12 秒（各自独立倒计时）
   · 同指纹延续触发：原卡原位刷新内容并重新计时，不另开新卡
   · 卡片多时弹卡区可上下滚动（隐形滚动条）
   · 点卡片跳转由调用方通过 onClick 或 href 指定；× 直接关闭
   · 跨页面存活（2026-10-16 用户定稿）：活卡写入 sessionStorage（本标签页），
     跳转/返回任何页面时自动恢复未到期卡片，倒计时连续计算、到期照常消失、
     手动 × 同步清除；恢复的卡片不重播提示音。onClick 是闭包无法跨页面保存，
     需要恢复后仍可点击的卡请传 href（优先级低于 onClick）。
   用法：PopupCards.push({ fp, title, info, tone, onClick, href, ms })
   tone 五色（与矩阵图例一致）：up=红(A多/涨) down=青(A空/跌) gold=金(B多/三买)
        purple=紫(B空/三卖) pos=蓝(持仓专属：离场预警/升级提示，2026-10-13 定稿) */
const PopupCards = (function () {
  const MS = 12000;
  const KEY = 'pc_popups';
  let stack = null, pops = {};   // fp -> {el, timer}

  /* —— sessionStorage 存取（本标签页内跨页面；新标签页不恢复，避免重复轰炸）—— */
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

  function _kill(fp) {            // 到期 / 手动 ×：卡片与存档一起清
    const rec = pops[fp];
    if (rec) { clearTimeout(rec.timer); rec.el.remove(); delete pops[fp]; }
    _unstore(fp);
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
    x.onclick = function (e) { e.stopPropagation(); _kill(fp); };
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

  return { push: push };
})();
