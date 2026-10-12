/* 右上角预警弹卡 · 全局组件（2026-10-12 用户定稿）
   —— 监控页 / 详情页 / 列表页共用一份代码一套样式（以监控页版为基准）：
   · 多卡叠叠乐：新事件往上叠、不顶掉老卡，每张卡完整存在 12 秒（各自独立倒计时）
   · 同指纹延续触发：原卡原位刷新内容并重新计时，不另开新卡
   · 卡片多时弹卡区可上下滚动（隐形滚动条）
   · 点卡片跳转由调用方通过 onClick 指定；× 直接关闭
   用法：PopupCards.push({ fp, title, info, tone:'up'|'down', onClick, ms })
        fp=去重键（无则每次新卡）；tone up=红(多/涨) down=青(空/跌) */
const PopupCards = (function () {
  const MS = 12000;
  let stack = null, pops = {};   // fp -> {el, timer}

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
.alert-pop .t{font-size:14px;font-weight:700;margin-bottom:4px;}
.alert-pop .t.up{color:#ef5350;} .alert-pop .t.down{color:#26a69a;}
.alert-pop .i{color:#9aa0b0;line-height:1.65;word-break:break-all;}
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

  function push(o) {
    if (!document.body) return;
    css();
    const box = ensureStack();
    const fp = o.fp || ('k_' + Math.random());
    let rec = pops[fp];
    if (rec) clearTimeout(rec.timer);          // 延续触发：原卡刷新、重新计时
    else {
      const el = document.createElement('div');
      el.className = 'alert-pop ' + (o.tone === 'down' ? 'down' : 'up');
      el.onclick = (o.onClick || function () {});
      el.innerHTML = '<span class="x" onclick="event.stopPropagation();this.parentElement.remove()">×</span>' +
        '<div class="t ' + (o.tone === 'down' ? 'down' : 'up') + '"></div><div class="i"></div>';
      box.prepend(el);                          // 新卡在最上，老卡依次下挤
      rec = pops[fp] = { el: el };
    }
    rec.el.querySelector('.t').textContent = o.title || '';
    rec.el.querySelector('.i').innerHTML = o.info || '';
    clearTimeout(rec.timer);
    rec.timer = setTimeout(function () { rec.el.remove(); delete pops[fp]; }, o.ms || MS);
  }

  return { push: push };
})();
