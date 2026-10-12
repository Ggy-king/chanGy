/* 结构区间浮层（2026-10-12 用户定稿）——独立于中枢模块：
   - 独立 canvas（#structOverlay），不跟"显示中枢"开关走；
   - 只在从监控页（格子/卡片）带着结构参数进详情页时画一块色带：
     A类橙 = 1-2 被跌破再收复的区间；B类紫 = 5-6 回踩参考区间（准中枢）；
   - 从结构起点 t1 延伸到图表右缘，虚线框 + 淡填充 + 角标，随缩放平移重画；
   - 显示开关存 localStorage('cl_struct_visible')，默认开；开关 UI 在监控页设置里。
   依赖（由 pc_detail.html 提供，均为调用期取值）：chart、candle、TZ_OFFSET、$、
   引擎 raw 时间 = 真 UTC 纪元，ISO(+08:00) 解析后 +TZ_OFFSET 对齐图表时间轴（实测差值 0）。 */
const STRUCT_COL = { A: '#ff9800', B: '#b388ff' };

const StructZone = {
  el: null, cv: null, ctx: null, zone: null,
  _raf: false,
  visible: (function () { try { return localStorage.getItem('cl_struct_visible') !== '0'; } catch (e) { return true; } })(),

  init(el) {
    this.el = el;
    const c = document.createElement('canvas');
    c.id = 'structOverlay';
    c.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:11;';
    c.style.display = this.visible ? 'block' : 'none';
    el.appendChild(c);
    this.cv = c; this.ctx = c.getContext('2d');
    // 独立订阅视图变化（与中枢浮层互不依赖）
    try {
      chart.timeScale().subscribeVisibleTimeRangeChange(() => this.schedule());
      candle.priceScale().subscribeVisibleRangeChange(() => this.schedule());
    } catch (e) {}
    this.schedule();
  },

  setVisible(v) {
    this.visible = !!v;
    try { localStorage.setItem('cl_struct_visible', v ? '1' : '0'); } catch (e) {}
    if (this.cv) this.cv.style.display = this.visible ? 'block' : 'none';
    this.schedule();
  },

  /* zone: {p1,p2,t1(ISO),kind:'A'|'B',side} 或 null（清除） */
  set(z) {
    if (!z || z.p1 == null || z.p2 == null || !z.t1 || !STRUCT_COL[z.kind]) { this.zone = null; this.schedule(); return; }
    const ms = Date.parse(String(z.t1).replace(' ', 'T'));
    if (isNaN(ms)) { this.zone = null; this.schedule(); return; }
    this.zone = {
      st: ms / 1000 + TZ_OFFSET,
      high: Math.max(+z.p1, +z.p2), low: Math.min(+z.p1, +z.p2),
      color: STRUCT_COL[z.kind],
      label: z.kind + '类' + (z.side === 'short' ? '(空)' : '(多)'),
    };
    this.schedule();
  },

  schedule() {
    if (this._raf) return;
    this._raf = true;
    requestAnimationFrame(() => { this._raf = false; this.draw(); });
  },

  draw() {
    if (!this.cv || !this.ctx || !this.visible) return;
    const box = this.el;
    const dpr = window.devicePixelRatio || 1;
    const w = box.clientWidth, h = box.clientHeight;
    if (w <= 0 || h <= 0) return;
    const cw = Math.floor(w * dpr), ch = Math.floor(h * dpr);
    if (this.cv.width !== cw || this.cv.height !== ch) {
      this.cv.width = cw; this.cv.height = ch;
      this.cv.style.width = w + 'px'; this.cv.style.height = h + 'px';
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const z = this.zone;
    if (!z || !chart || !candle) return;
    const ts = chart.timeScale();
    const yH = candle.priceToCoordinate(z.high), yL = candle.priceToCoordinate(z.low);
    if (yH == null || yL == null) return;
    let xL = ts.timeToCoordinate(z.st);
    if (xL == null) xL = 0;                                   // 结构起点在可视区左侧之外 → 从左缘画起
    let xR = w;
    try { xR = w - (chart.priceScale('right').width() || 0); } catch (e) {}   // 主图右缘（价格轴之前）
    if (xR <= xL) return;
    ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.lineCap = 'butt';
    ctx.strokeStyle = z.color; ctx.fillStyle = z.color;
    ctx.globalAlpha = .09; ctx.fillRect(xL, yH, xR - xL, yL - yH); ctx.globalAlpha = 1;
    ctx.strokeRect(xL + 0.5, yH + 0.5, xR - xL, yL - yH);
    ctx.font = '11px sans-serif';
    ctx.fillText(z.label, xL + 6, Math.max(yH + 13, 13));
  },
};
