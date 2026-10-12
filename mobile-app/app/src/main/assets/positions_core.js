/* 移动端持仓共享逻辑层（2026-10-12 用户定稿：PC 端不动，移动端 /m 与 APK 离线壳一套代码）。
   收敛范围：常量/工具/平仓简报弹层/平仓流程（数据仍由各页经 gh 直连 Gitee 自管）。
   全局暴露：POS_EXIT、POS_LIMIT、POS_PNAME、posLabel、periodLabel、b64enc、b64dec、
             _delPend、PosCore{initSheet, open, confirm, cancel}
   页面用法：PosCore.open(id, rec, GH, hooks) 弹简报；
     hooks = { bgJob(fn,label), getPositions(), removeLocal(id), done(id), toast(msg) }
   注意：本文件同时被 /m（服务器伺服）与 APK assets 引用，改完两端要同步部署。 */
(function () {
  window.POS_EXIT = { '二买': '二卖', '二卖': '二买', '三买': '三卖', '三卖': '三买' };
  window.POS_LIMIT = 4;
  window.POS_PNAME = { '30': '30秒', '3': '3分', '5': '5分', '15': '15分', '30m': '30分', '60': '60分', 'daily': '日线' };
  window.posLabel = function (v) { v = v || ''; return v.startsWith('二') ? ('A类' + v) : v.startsWith('三') ? ('B类' + v) : v; };
  window.periodLabel = function (p) { return POS_PNAME[p] || p + '分'; };
  window.b64enc = function (s) { return btoa(unescape(encodeURIComponent(s))); };
  window.b64dec = function (b) { return new TextDecoder().decode(Uint8Array.from(atob(b), c => c.charCodeAt(0))); };
  window._delPend = new Set();   // 已本地删除但还没推到远端的 id（防止 posLoad 拉回）

  let _id = null, _rec = null, _gh = null, _hooks = null;

  function initSheet() {
    if (document.getElementById('posCloseOv')) return;
    const mask = document.createElement('div');
    mask.id = 'posCloseOv'; mask.className = 'sheet-mask'; mask.style.display = 'none';
    mask.innerHTML = `
      <div class="sheet">
        <b style="font-size:15px;">平仓 · 交易简报</b>
        <p id="mpcInfo" style="margin:12px 0;font-size:12.5px;line-height:1.9;color:var(--t2);"></p>
        <div id="mpcResults" style="display:flex;gap:9px;margin-bottom:12px;">
          <!-- 原生 radio+label：选中由浏览器原生机制完成，不依赖任何 JS/点击事件 -->
          <input type="radio" name="mpcR" id="mpcR0" value="止盈" class="mpc-r"><label for="mpcR0">止盈</label>
          <input type="radio" name="mpcR" id="mpcR1" value="止损" class="mpc-r"><label for="mpcR1">止损</label>
          <input type="radio" name="mpcR" id="mpcR2" value="保本" class="mpc-r"><label for="mpcR2">保本</label>
        </div>
        <input id="mpcPnl" placeholder="盈亏金额，如 +800 或 -300（选填）" style="width:100%;background:var(--app);border:1px solid var(--line);color:var(--t1);border-radius:9px;padding:10px 12px;font-size:13.5px;">
        <div class="btns">
          <button class="btn-cancel" onclick="PosCore.cancel()">取消</button>
          <button class="btn-ok" onclick="PosCore.confirm()">确认平仓</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    const s = document.createElement('style');
    s.textContent = `#mpcResults .mpc-r{position:absolute;opacity:0;width:0;height:0;}
      #mpcResults label{flex:1;text-align:center;padding:9px 0;border-radius:9px;
        border:1px solid var(--line);background:var(--app);color:var(--t1);
        font-size:13.5px;cursor:pointer;}
      #mpcResults input:checked+label{background:var(--gold);border-color:var(--gold);
        color:#241A05;font-weight:700;}`;
    document.head.appendChild(s);
  }

  function open(id, rec, gh, hooks) {
    initSheet();
    // rec 在打开时捕获（后台 posLoad 可能在确认前刷新掉 POSITIONS 数组）
    _id = id; _rec = rec; _gh = gh; _hooks = hooks || {};
    const now = new Date();
    const opened = rec.open_iso ? new Date(rec.open_iso) : now;
    const hold = Math.max(0, Math.round((now - opened) / 60000));
    const hm = hold >= 60 ? `${Math.floor(hold / 60)}小时${hold % 60}分` : `${hold}分钟`;
    document.getElementById('mpcInfo').innerHTML = `<b style="color:var(--t1);">${rec.name}（${rec.symbol}）</b> ${periodLabel(rec.period)}<br>
      进场 <b style="color:var(--t1);">${posLabel(rec.entry)}</b> → 离场 <b style="color:var(--t1);">${posLabel(rec.exit)}</b><br>
      开仓 ${rec.open_time} ${rec.weekday} · 持仓 <b style="color:var(--gold);">${hm}</b><br>
      <span style="font-size:11px;">简报全部选填，直接确认就是平仓删除</span>`;
    document.querySelectorAll('#mpcResults input').forEach(x => x.checked = false);
    document.getElementById('mpcPnl').value = '';
    document.getElementById('posCloseOv').style.display = 'flex';
  }

  function cancel() { _id = null; document.getElementById('posCloseOv').style.display = 'none'; }

  async function confirm() {
    if (!_id) return;
    const selB = document.querySelector('#mpcResults input:checked');   // 原生 radio 读取选中项
    const result = selB ? selB.value : '';
    const pnl = document.getElementById('mpcPnl').value.trim();
    const id = _id; _id = null;
    const rec = _rec;   // 打开时捕获的快照，不依赖页面数组（已从 hooks 移除 getPositions）
    // 乐观：本地立即移除，后台推 Gitee（失败自动重试）
    _hooks.removeLocal(id);
    cancel();
    _hooks.toast('已平仓');
    const now = new Date();
    const t = `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    _hooks.bgJob(async () => {
      const d = await _gh.get('positions.json');
      const list = d.data.positions || [];
      const r = list.find(x => x.id === id);
      if (result || pnl) {   // 空简报=试验单，纯删除不进 history
        const src = r || rec;
        const opened = src && src.open_iso ? new Date(src.open_iso) : now;
        const hold = Math.max(0, Math.round((now - opened) / 60000));
        let hist = [];
        try { const h = await _gh.get('history.json'); hist = h.data.history || []; } catch (e) {}
        hist.unshift({...src, close_result: result, close_pnl: pnl,
          close_time: t, close_iso: now.toISOString ? now.toISOString() : String(now),
          weekday_close: '周' + '日一二三四五六'[now.getDay()], hold_min: hold});
        await _gh.put('history.json', { history: hist }, '平仓简报 ' + (src ? src.name : '') + ' ' + t);
      }
      const left = list.filter(x => x.id !== id);
      await _gh.put('positions.json', { positions: left }, '平仓删除 ' + (r ? r.name : '') + ' ' + t);
      if (_hooks.done) _hooks.done(id);
    }, '平仓');
  }

  window.PosCore = { initSheet, open, confirm, cancel };
})();
