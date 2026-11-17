'use strict';
/* DMM Viewer front end.
 * Talks to the Python side either through pywebview (app window) or through
 * plain HTTP when opened with ?http=1 (python app.py --browser).
 */

const HTTP_MODE = new URLSearchParams(location.search).has('http');
const POLL_MS = 150;
const KEEP_SECONDS = 660;          // history kept for the 10 min graph
const STALE_MS = 5000;

const $ = (sel, root = document) => root.querySelector(sel);

function api(name, ...args) {
  if (HTTP_MODE) {
    return fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args),
    }).then(r => r.json()).then(r => {
      if ('error' in r) throw new Error(r.error);
      return r.result;
    });
  }
  return window.pywebview.api[name](...args);
}

function bridgeReady() {
  if (HTTP_MODE || (window.pywebview && window.pywebview.api)) return Promise.resolve();
  return new Promise(resolve => window.addEventListener('pywebviewready', resolve, { once: true }));
}

/* ---------------------------------------------------------------- formatting */

const PREFIXES = [[1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n'], [1e-12, 'p']];

function siPrefix(v) {
  const a = Math.abs(v);
  if (!a || !isFinite(a)) return [1, ''];
  for (const [m, p] of PREFIXES) if (a >= m * 0.9999995) return [m, p];
  return [1e-12, 'p'];
}

/** -> {num, unit}; digits = significant digits to show. null means overload. */
function formatValue(v, unit, digits) {
  if (v === null || v === undefined) return { num: 'OL', unit };
  const [m, p] = siPrefix(v);
  const s = v / m;
  const intDigits = Math.max(1, Math.floor(Math.log10(Math.abs(s) || 1)) + 1);
  return { num: s.toFixed(Math.max(0, digits - intDigits)), unit: p + unit };
}

/** Like formatValue, but with a fixed absolute resolution (e.g. 1e-6 V). Used for A − B. */
function formatRes(v, unit, res) {
  if (v === null || v === undefined) return { num: 'OL', unit };
  const [m, p] = siPrefix(Math.abs(v) >= res ? v : res);
  return { num: (v / m).toFixed(Math.max(0, Math.ceil(-Math.log10(res / m) - 1e-9))), unit: p + unit };
}

/** Smallest step a reading can show, from its size and the number of digits. */
const resolutionOf = (v, digits) => 10 ** (Math.floor(Math.log10(Math.abs(v) || 1)) - digits + 1);

const fmt = (v, unit, digits) => { const f = formatValue(v, unit, digits); return `${f.num} ${f.unit}`; };
const rangeLabel = (r, unit) => { const f = formatValue(parseFloat(r), unit, 4); return `${parseFloat(f.num)} ${f.unit}`; };

/* Unit text as the 34401A shows it, used by the VFD theme (prefix is added in front). */
const VFD_UNITS = { DCV: 'VDC', ACV: 'VAC', DCI: 'ADC', ACI: 'AAC', OHM2: 'OHM', OHM4: 'OHM', FREQ: 'Hz', PER: 'SEC' };
const isVfd = () => document.documentElement.dataset.theme === 'vfd';

function speedInfo(cfg) {
  const kind = S.info.functions[cfg.func].speed;
  return S.info.speeds[kind].find(s => s.value === cfg.speed) || S.info.speeds[kind][0];
}

/* ---------------------------------------------------------------- state */

const S = {
  info: null,
  found: [],
  panels: new Map(),          // id -> Panel
  seq: 0,
  polling: false,
  math: { buf: [], lastKey: '', format: null },
};

/* ---------------------------------------------------------------- UI helpers */

let toastTimer = 0;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5000);
}

function notice(msg, kind = 'warn') {
  const el = $('#notice');
  el.textContent = msg || '';
  el.className = 'notice' + (kind === 'error' ? ' error' : '');
  el.hidden = !msg;
}

function confirmModal(title, text) {
  return new Promise(resolve => {
    const modal = $('#modal');
    $('#modalTitle').textContent = title;
    $('#modalText').textContent = text;
    modal.hidden = false;
    $('#modalOk').focus();
    const done = ok => {
      modal.hidden = true;
      $('#modalOk').onclick = $('#modalCancel').onclick = modal.onkeydown = null;
      resolve(ok);
    };
    $('#modalOk').onclick = () => done(true);
    $('#modalCancel').onclick = () => done(false);
    modal.onkeydown = e => { if (e.key === 'Escape') done(false); };
  });
}

const FUNC_WARNINGS = {
  ohms: {
    funcs: ['OHM2', 'OHM4'],
    title: 'Switch to resistance?',
    text: 'Resistance mode drives a test current out of the HI and LO terminals into whatever is connected. Only measure resistance on unpowered circuits.',
  },
  current: {
    funcs: ['DCI', 'ACI'],
    title: 'Switch to current?',
    text: 'Current is measured through the I terminal (3 A fused) with the meter in series with the circuit. Check the leads are in the I and LO jacks before continuing.',
  },
};

function fillSelect(sel, options, value) {
  sel.replaceChildren(...options.map(([v, label]) => new Option(label, v)));
  if (value !== undefined) sel.value = value;
}

/* ---------------------------------------------------------------- stats */

class Stats {
  constructor() { this.reset(); }
  reset() { this.n = 0; this.sum = 0; this.min = Infinity; this.max = -Infinity; }
  add(v) {
    if (v === null) return;
    this.n++; this.sum += v;
    if (v < this.min) this.min = v;
    if (v > this.max) this.max = v;
  }
}

/* ---------------------------------------------------------------- chart */

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

/** Scrolling time chart. pts = [[tMs, value|null], ...] sorted by time; label(v) formats the axis. */
function drawChart(canvas, pts, windowSec, label, nowMs) {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = '11px ' + cssVar('--mono');

  const padL = 86, padR = 10, padT = 8, padB = 18;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const t1 = nowMs, t0 = t1 - windowSec * 1000;

  let start = 0, lo = 0, hi = pts.length;               // first point inside the window
  while (lo < hi) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t0) lo = mid + 1; else hi = mid; }
  start = Math.max(0, lo - 1);

  let vmin = Infinity, vmax = -Infinity;
  for (let i = start; i < pts.length; i++) {
    const v = pts[i][1];
    if (v === null) continue;
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }

  ctx.fillStyle = cssVar('--chart-text');
  if (vmin === Infinity) {
    ctx.textAlign = 'center';
    ctx.fillText(pts.length ? 'Overload' : 'Waiting for readings…', padL + plotW / 2, padT + plotH / 2);
    return;
  }
  if (vmin === vmax) { const d = Math.abs(vmin) * 1e-6 || 1e-9; vmin -= d; vmax += d; }
  const margin = (vmax - vmin) * 0.1;
  vmin -= margin; vmax += margin;

  const x = t => padL + ((t - t0) / (t1 - t0)) * plotW;
  const y = v => padT + ((vmax - v) / (vmax - vmin)) * plotH;

  // grid + labels
  ctx.strokeStyle = cssVar('--chart-grid');
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const v = vmin + ((vmax - vmin) * i) / 4;
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
    ctx.fillText(label(v), padL - 6, yy);
  }
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  ctx.fillText(windowSec >= 120 ? `−${windowSec / 60} min` : `−${windowSec} s`, padL, h - 4);
  ctx.textAlign = 'right';
  ctx.fillText('now', w - padR, h - 4);

  // trace: one min/max segment per pixel column, so long histories stay fast and spikes stay visible
  ctx.save();
  ctx.beginPath(); ctx.rect(padL, padT - 2, plotW + 2, plotH + 4); ctx.clip();
  ctx.strokeStyle = cssVar('--chart-line');
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  const overloads = [];
  let col = null, cmin = 0, cmax = 0, penDown = false;
  const flush = () => {
    if (col === null) return;
    if (!penDown) { ctx.moveTo(col, y(cmin)); penDown = true; } else ctx.lineTo(col, y(cmin));
    if (cmax !== cmin) ctx.lineTo(col, y(cmax));
  };
  for (let i = start; i < pts.length; i++) {
    const [t, v] = pts[i];
    if (v === null) { flush(); col = null; penDown = false; overloads.push(x(t)); continue; }
    const px = Math.round(x(t));
    if (px !== col) { flush(); col = px; cmin = cmax = v; }
    else { if (v < cmin) cmin = v; if (v > cmax) cmax = v; }
  }
  flush();
  ctx.stroke();

  ctx.strokeStyle = cssVar('--chart-ol');
  for (const ox of overloads) { ctx.beginPath(); ctx.moveTo(ox, padT); ctx.lineTo(ox, padT + 8); ctx.stroke(); }

  const last = pts[pts.length - 1];
  if (last && last[1] !== null) {
    ctx.fillStyle = cssVar('--chart-line');
    ctx.beginPath(); ctx.arc(x(last[0]), y(last[1]), 3, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
}

/* ---------------------------------------------------------------- panel */

class Panel {
  constructor(data) {
    this.id = data.id;
    this.data = data;
    this.buf = [];
    this.stats = new Stats();
    this.last = null;            // {t (s), v}
    this.el = $('#panelTpl').content.firstElementChild.cloneNode(true);
    this.q = sel => $(sel, this.el);

    this.q('.name').textContent = data.name;
    const kind = data.serial ? 'RS-232' : 'GPIB';
    this.q('.sub').textContent = `Firmware ${data.firmware || '?'} · ${kind}`;
    this.q('.sub').title = `${data.manufacturer} ${data.model}, ${data.resource}`;

    fillSelect(this.q('.func'), Object.entries(S.info.functions).map(([k, f]) => [k, f.label]));
    this.q('.func').onchange = () => this.onFunc();
    this.q('.range').onchange = () => this.send({ ...this.data.config, range: this.q('.range').value });
    this.q('.speed').onchange = () => this.send({ ...this.data.config, speed: this.q('.speed').value });
    this.q('.reset').onclick = () => { this.stats.reset(); this.renderStats(); };
    this.q('.remove').onclick = () => removePanel(this.id);
    this.q('.run').onclick = () => this.toggleRun();
    this.q('.local').onclick = () => api('go_local', this.id).catch(e => toast(e.message));
    this.q('.window').onchange = () => draw(true);

    this.view = { graph: true, ...(data.view || {}) };
    this.q('.graph-btn').onclick = () => this.setGraph(!this.view.graph, true);
    this.setGraph(this.view.graph, false);
    this.q('.readout')._panel = this;
    readoutObserver.observe(this.q('.readout'));

    this.showConfig();
    this.setStatus(data);
  }

  setGraph(on, save) {
    this.view.graph = on;
    this.el.classList.toggle('no-graph', !on);
    const btn = this.q('.graph-btn');
    btn.setAttribute('aria-pressed', String(on));
    btn.title = on ? 'Hide graph' : 'Show graph';
    if (save) {
      api('set_panel_view', this.id, this.view).catch(e => toast(e.message));
      draw(true);
    }
  }

  /** Make the number as big as its box allows (box size depends on window size and the graph toggle). */
  fitReadout() {
    const box = this.q('.readout'), val = this.q('.readout .value'), unit = this.q('.readout .unit');
    const cs = getComputedStyle(box);
    const annun = this.q('.annun').offsetHeight;
    const W = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 4;
    const H = box.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - (annun ? annun + 6 : 0);
    if (W <= 0 || H <= 0) return;
    const key = `${W}x${H}|${val.textContent.length}|${unit.textContent}|${document.documentElement.dataset.theme}`;
    if (key === this.fitKey) return;
    this.fitKey = key;
    box.style.fontSize = '100px';                       // measure at a known size, then scale
    const w = val.offsetWidth + unit.offsetWidth + 10;  // 10px = gap between number and unit
    const h = val.offsetHeight;
    const size = Math.max(22, Math.min(340, 100 * Math.min(W / w, H / h) * 0.97));
    box.style.fontSize = size.toFixed(1) + 'px';
  }

  get unit() { return S.info.functions[this.data.config.func].unit; }
  get digits() { return speedInfo(this.data.config).digits; }

  showConfig() {
    const cfg = this.data.config, f = S.info.functions[cfg.func];
    this.q('.func').value = cfg.func;
    fillSelect(this.q('.range'), [['AUTO', 'Auto'], ...f.ranges.map(r => [r, rangeLabel(r, f.unit)])], cfg.range);
    this.q('.range').disabled = !f.ranges.length;
    fillSelect(this.q('.speed'), S.info.speeds[f.speed].map(s => [s.value, s.label]), cfg.speed);
    this.renderReadout();
    this.updateAnnunciators();
  }

  /** Big number + unit. The VFD theme uses the meter's style: "+5.00012 VDC", "OVLD". */
  renderReadout() {
    const vfd = isVfd();
    let num, unit;
    if (!this.last) {
      num = vfd ? '!!!!!!!!' : '——';            // '!' is a blank digit in the segment font
      unit = this.unit;
    } else {
      const f = formatValue(this.last.v, this.unit, this.digits);
      if (this.last.v !== null) this.lastUnit = f.unit;
      unit = this.lastUnit || f.unit;
      num = !vfd ? f.num : this.last.v === null ? 'OVLD' : (this.last.v >= 0 ? '+' : '') + f.num;
    }
    if (vfd) unit = unit.slice(0, unit.length - this.unit.length) + VFD_UNITS[this.data.config.func];
    const el = this.q('.readout .value');
    el.textContent = num;
    el.dataset.ghost = num.replace(/[^.]/g, '~');  // '~' lights every segment: the unlit background
    this.q('.readout .unit').textContent = unit;
    this.fitReadout();
  }

  updateAnnunciators() {
    const cfg = this.data.config, st = this.data;
    const set = (key, on) => this.q(`.annun [data-a="${key}"]`).classList.toggle('on', !!on);
    set('rmt', ['running', 'paused', 'connecting'].includes(st.status));
    set('man', cfg.range !== 'AUTO' && S.info.functions[cfg.func].ranges.length > 0);
    set('4w', cfg.func === 'OHM4');
    set('rear', st.terminals === 'Rear');
    set('err', st.status === 'error' || (st.status === 'running' && st.message));
  }

  async onFunc() {
    const next = this.q('.func').value, prev = this.data.config.func;
    for (const w of Object.values(FUNC_WARNINGS)) {
      if (w.funcs.includes(next) && !w.funcs.includes(prev) && !(await confirmModal(w.title, w.text))) {
        this.q('.func').value = prev;
        return;
      }
    }
    await this.send({ func: next, range: 'AUTO', speed: null });
  }

  async send(cfg) {
    try {
      const applied = await api('configure', this.id, cfg);
      const funcChanged = applied.func !== this.data.config.func;
      this.data.config = applied;
      if (funcChanged) this.clear();
      this.showConfig();
      this.renderStats();
      updateMath(true);
    } catch (e) {
      toast(e.message);
      this.showConfig();
    }
  }

  clear() {
    this.buf = [];
    this.last = null;
    this.lastUnit = null;
    this.stats.reset();
    this.el.classList.remove('overload');
    this.renderReadout();
  }

  async toggleRun() {
    try { await api('set_running', this.id, !this.data.running); }
    catch (e) { toast(e.message); }
  }

  ingest(rows) {
    let got = false;
    for (const [t, v, func] of rows) {
      if (func !== this.data.config.func) continue;     // reading taken before a function change
      this.buf.push([t * 1000, v]);
      this.stats.add(v);
      this.last = { t, v };
      got = true;
    }
    if (!got) return false;
    const cutoff = Date.now() - KEEP_SECONDS * 1000;
    if (this.buf.length && this.buf[0][0] < cutoff) {
      let i = 0;
      while (i < this.buf.length && this.buf[i][0] < cutoff) i++;
      this.buf.splice(0, i);
    }
    this.renderReadout();
    this.el.classList.toggle('overload', this.last.v === null);
    this.renderStats();
    return true;
  }

  renderStats() {
    const s = this.stats, u = this.unit, d = this.digits;
    this.q('.s-min').textContent = s.n ? fmt(s.min, u, d) : '—';
    this.q('.s-max').textContent = s.n ? fmt(s.max, u, d) : '—';
    this.q('.s-avg').textContent = s.n ? fmt(s.sum / s.n, u, d) : '—';
    this.q('.s-n').textContent = s.n;
  }

  setStatus(st) {
    Object.assign(this.data, st);
    const dot = this.q('.dot');
    dot.className = 'dot ' + st.status;
    dot.title = st.status;
    const msg = this.q('.message');
    const text = st.message || { connecting: 'Connecting…', paused: 'Paused', local: 'Front panel unlocked' }[st.status] || '';
    msg.textContent = text;
    msg.className = 'message small' + (st.status === 'error' ? ' error' : st.message && st.status === 'running' ? ' warn' : '');
    const term = this.q('.term');
    term.textContent = st.terminals ? `${st.terminals} terminals` : '';
    term.hidden = !st.terminals;
    term.classList.toggle('rear', st.terminals === 'Rear');
    this.q('.run').textContent = st.running ? 'Pause' : 'Resume';
    this.q('.local').disabled = st.status === 'local';
    this.updateAnnunciators();
    const stale = st.status !== 'running' || !this.last || Date.now() - this.last.t * 1000 > STALE_MS + 2000 * this.slowness();
    this.el.classList.toggle('stale', stale && !!this.last);
  }

  slowness() { return this.data.config.speed === '100' || this.data.config.speed === '3' ? 4 : 1; }

  draw(now) {
    if (!this.view.graph) return;
    const unit = this.unit, digits = this.digits;
    drawChart(this.q('.chart'), this.buf, +this.q('.window').value, v => fmt(v, unit, digits), now);
  }
}

/* Refit a readout whenever its box changes size (window resize, graph toggled, panels added). */
const readoutObserver = new ResizeObserver(entries => {
  for (const e of entries) if (e.target._panel) e.target._panel.fitReadout();
});

function refitAll() {
  for (const p of S.panels.values()) { p.fitKey = null; p.fitReadout(); }
}

/* ---------------------------------------------------------------- panels + picker */

function addPanelEl(data) {
  const p = new Panel(data);
  S.panels.set(p.id, p);
  $('#panels').append(p.el);
  refreshLayout();
  return p;
}

async function removePanel(id) {
  try {
    await api('remove_panel', id);
  } catch (e) { toast(e.message); }
  const p = S.panels.get(id);
  if (p) {
    readoutObserver.unobserve(p.q('.readout'));
    p.el.remove();
  }
  S.panels.delete(id);
  refreshLayout();
}

function refreshLayout() {
  $('#empty').hidden = S.panels.size > 0;
  fillPicker();
  updateMathOptions();
}

function fillPicker() {
  const sel = $('#meterSelect');
  const inUse = new Set([...S.panels.values()].map(p => p.data.resource));
  const opts = S.found.map(m => {
    let label = `${m.model || 'Unknown'} @ ${m.address}`;
    if (!m.supported) label += ' (not a 34401A)';
    else if (inUse.has(m.resource)) label += ' (open)';
    const o = new Option(label, m.resource);
    o.disabled = !m.supported || inUse.has(m.resource);
    return o;
  });
  const keep = sel.value;
  if (!opts.length) {
    const o = new Option('No 34401A found', '');
    o.disabled = true;
    opts.push(o);
  }
  sel.replaceChildren(...opts);
  const free = opts.find(o => !o.disabled && o.value === keep) || opts.find(o => !o.disabled);
  if (free) sel.value = free.value;
  $('#addBtn').disabled = !free;
}

function showScan(result) {
  S.found = result.meters || [];
  $('#backend').textContent = result.backend && result.backend !== 'demo' ? `via ${result.backend}` : '';
  const problems = [...(result.problems || [])];
  if (result.missing && result.missing.length) {
    problems.push(`Saved panel${result.missing.length > 1 ? 's' : ''} not restored, meter not found: ${result.missing.join(', ')}`);
  }
  notice(problems.join('  ·  '), result.backend ? 'warn' : 'error');
  fillPicker();
}

async function rescan() {
  const btn = $('#rescanBtn');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try { showScan(await api('scan', $('#extraScan').value)); }
  catch (e) { notice(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Rescan'; }
}

async function addSelected() {
  const res = $('#meterSelect').value;
  if (!res) return;
  $('#addBtn').disabled = true;
  try { addPanelEl(await api('add_panel', res)); }
  catch (e) { toast(e.message); }
  finally { fillPicker(); }
}

/* ---------------------------------------------------------------- math view (two meters) */

/* same: both meters must read the same unit. Keys must match MATH_OPS in app.py. */
const MATH_OPS = {
  sub: { label: 'A − B', same: true },
  add: { label: 'A + B', same: true },
  mul: { label: 'A × B' },
  div: { label: 'A ÷ B' },
  pct: { label: '% deviation (A − B) / B', same: true },
  db:  { label: 'dB 20·log₁₀(A / B)', same: true },
};

/** Fixed decimals, no SI prefix (for %, dB and plain ratios). */
function formatPlain(v, unit, res) {
  if (v === null || v === undefined) return { num: 'OL', unit };
  return { num: v.toFixed(Math.min(6, Math.max(0, Math.ceil(-Math.log10(res) - 1e-9)))), unit };
}

const productUnit = (a, b) => ([a, b].sort().join('') === 'AV' ? 'W' : `${a}·${b}`);
const quotientUnit = (a, b) =>
  a === b ? '' : a === 'V' && b === 'A' ? 'Ω' : a === 'A' && b === 'V' ? 'S' : `${a}/${b}`;

/** A, B = {v (null = overload), unit, res}. Returns {v, format(x) -> {num, unit}} or {error}.
 *  The shown precision follows the meters' own resolution, so no false digits. */
function computeMath(op, A, B) {
  if (MATH_OPS[op].same && A.unit !== B.unit) return { error: 'Both meters must read the same unit for this operation' };
  const ovl = A.v === null || B.v === null;
  const rel = ovl ? 1e-6 : Math.max(A.res / Math.max(Math.abs(A.v), A.res), B.res / Math.max(Math.abs(B.v), B.res));
  const relRes = v => Math.max(Math.abs(v || 0) * rel, 1e-15);
  let v = null, format;
  if (op === 'sub' || op === 'add') {
    const res = Math.max(A.res, B.res, 1e-15);
    if (!ovl) v = op === 'sub' ? A.v - B.v : A.v + B.v;
    format = x => formatRes(x, A.unit, res);
  } else if (op === 'mul') {
    const unit = productUnit(A.unit, B.unit);
    if (!ovl) v = A.v * B.v;
    const res = relRes(v);
    format = x => formatRes(x, unit, res);
  } else if (op === 'div') {
    const unit = quotientUnit(A.unit, B.unit);
    if (!ovl) v = A.v / B.v;
    const res = relRes(v);
    format = unit ? x => formatRes(x, unit, res) : x => formatPlain(x, '', res);
  } else if (op === 'pct') {
    if (!ovl) v = ((A.v - B.v) / B.v) * 100;
    format = x => formatPlain(x, '%', Math.max(200 * rel, 1e-6));
  } else {
    if (!ovl) v = 20 * Math.log10(Math.abs(A.v / B.v));
    format = x => formatPlain(x, 'dB', Math.max(8.69 * rel, 1e-6));
  }
  if (v !== null && !isFinite(v)) return { error: 'B reads zero' };
  return { v, format };
}

function mathSettings() {
  S.info.math = { enabled: true, op: 'sub', a: null, b: null, graph: true, ...S.info.math };
  if (!MATH_OPS[S.info.math.op]) S.info.math.op = 'sub';
  return S.info.math;
}

function updateMathOptions() {
  const m = mathSettings(), list = [...S.panels.values()];
  const btn = $('#mathBtn');
  btn.classList.toggle('on', m.enabled);
  btn.setAttribute('aria-pressed', String(m.enabled));
  $('#math').classList.toggle('no-graph', !m.graph);
  $('#mathGraphBtn').setAttribute('aria-pressed', String(m.graph));
  $('#mathGraphBtn').title = m.graph ? 'Hide graph' : 'Show graph';
  $('#math').hidden = !m.enabled || list.length < 2;
  if ($('#math').hidden) return;
  $('#mathOp').value = m.op;
  const pick = (sel, wanted, fallback) => {
    const cur = sel.value;
    fillSelect(sel, list.map(p => [p.id, p.data.name]));
    const byRes = list.find(p => p.data.resource === wanted);
    sel.value = list.some(p => p.id === cur) ? cur : byRes ? byRes.id : fallback.id;
  };
  pick($('#mathA'), m.a, list[0]);
  pick($('#mathB'), m.b, list.find(p => p.id !== $('#mathA').value) || list[1]);
  updateMath(true);
}

function mathPanels() {
  const a = S.panels.get($('#mathA').value), b = S.panels.get($('#mathB').value);
  return a && b && a !== b ? [a, b] : [null, null];
}

function saveMath() {
  api('set_math', mathSettings()).catch(e => toast('Could not save math settings: ' + e.message));
}

function onMathChange() {
  const m = mathSettings(), [a, b] = mathPanels();
  m.op = $('#mathOp').value;
  if (a) { m.a = a.data.resource; m.b = b.data.resource; }
  saveMath();
  updateMath(true);
}

function toggleMath(show) {
  mathSettings().enabled = show;
  saveMath();
  updateMathOptions();
  if (show && S.panels.size < 2) toast('Math needs two meters. Add a second DMM to see it.');
}

function updateMath(reset = false) {
  if ($('#math').hidden) return;
  const [a, b] = mathPanels(), op = $('#mathOp').value;
  const num = $('#mathNum'), unit = $('#mathUnit'), note = $('#mathNote');
  const key = a ? `${op}|${a.id}|${b.id}|${a.data.config.func}|${b.data.config.func}` : '';
  if (reset || key !== S.math.lastKey) { S.math.buf = []; S.math.lastKey = key; }
  const blank = msg => { num.textContent = '—'; unit.textContent = ''; note.textContent = msg; S.math.format = null; };
  if (!a) return blank('Pick two different meters');
  if (!a.last || !b.last) return blank('Waiting for readings…');
  const side = p => ({ v: p.last.v, unit: p.unit, res: p.last.v === null ? 0 : resolutionOf(p.last.v, p.digits) });
  const r = computeMath(op, side(a), side(b));
  if (r.error) { S.math.buf = []; return blank(r.error); }
  S.math.format = r.format;
  const f = r.format(r.v);
  num.textContent = f.num;
  unit.textContent = f.unit;
  note.textContent = `readings ${Math.round(Math.abs(a.last.t - b.last.t) * 1000)} ms apart`;
  const t = Math.max(a.last.t, b.last.t) * 1000;
  const tail = S.math.buf[S.math.buf.length - 1];
  if (!tail || tail[0] !== t) S.math.buf.push([t, r.v]);
  const cutoff = Date.now() - 120 * 1000;
  while (S.math.buf.length && S.math.buf[0][0] < cutoff) S.math.buf.shift();
}

/* ---------------------------------------------------------------- logging */

function showLog(state) {
  const btn = $('#logBtn');
  btn.textContent = state.active ? 'Stop CSV log' : 'Start CSV log';
  btn.classList.toggle('recording', state.active);
  const name = state.path ? state.path.split(/[\\/]/).pop() : '';
  $('#logInfo').textContent = state.active ? `${name} · ${state.rows} rows` : name ? `Saved ${name}` : '';
  $('#logInfo').title = state.path || '';
}

async function toggleLog() {
  try {
    const active = $('#logBtn').classList.contains('recording');
    showLog(await api(active ? 'stop_log' : 'start_log'));
  } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- loops */

async function poll() {
  if (S.polling) return;
  S.polling = true;
  try {
    const u = await api('get_updates', S.seq);
    S.seq = u.seq;
    let changed = false;
    for (const [id, rows] of Object.entries(u.readings)) {
      const p = S.panels.get(id);
      if (p && p.ingest(rows)) changed = true;
    }
    for (const [id, st] of Object.entries(u.panels)) {
      const p = S.panels.get(id);
      if (p) p.setStatus(st);
    }
    if (changed) updateMath();
    showLog(u.log);
  } catch (e) {
    console.warn('poll failed', e);
  } finally {
    S.polling = false;
  }
}

let lastDraw = 0;
function draw(force = false) {
  const now = Date.now();
  if (!force && now - lastDraw < 100) return;
  lastDraw = now;
  for (const p of S.panels.values()) p.draw(now);
  if (!$('#math').hidden && S.math.format && mathSettings().graph) {
    const format = S.math.format;
    drawChart($('#mathChart'), S.math.buf, 120, v => { const f = format(v); return `${f.num} ${f.unit}`; }, now);
  }
}

function frame() {
  if (!document.hidden) draw();
  requestAnimationFrame(frame);
}

/* ---------------------------------------------------------------- theme */

/* The saved theme lives in settings.json (pywebview clears browser storage on exit).
   localStorage is only a quick first guess so browser mode doesn't flash the default. */
const THEMES = ['dark', 'light', 'vfd'];

function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = 'dark';
  document.documentElement.dataset.theme = theme;
  $('#themeSelect').value = theme;
  try { localStorage.setItem('dmm-theme', theme); } catch { /* storage unavailable */ }
  for (const p of S.panels.values()) p.renderReadout();
  if (document.fonts) document.fonts.ready.then(refitAll);   // the segment font may load after the switch
  draw(true);
}

function initTheme() {
  let guess = 'dark';
  try { guess = localStorage.getItem('dmm-theme') || 'dark'; } catch { /* storage unavailable */ }
  applyTheme(guess);
  $('#themeSelect').onchange = () => {
    applyTheme($('#themeSelect').value);
    api('set_theme', $('#themeSelect').value).catch(e => toast('Could not save theme: ' + e.message));
  };
}

/* ---------------------------------------------------------------- start */

async function init() {
  initTheme();
  await bridgeReady();
  S.info = await api('get_info');
  applyTheme(S.info.theme);
  $('#demoBadge').hidden = !S.info.demo;
  $('#extraScan').value = S.info.extra_scan || '';
  $('#rescanBtn').onclick = rescan;
  $('#addBtn').onclick = addSelected;
  $('#extraScan').onkeydown = e => { if (e.key === 'Enter') rescan(); };
  $('#logBtn').onclick = toggleLog;
  $('#logFolderBtn').onclick = () => api('open_log_folder').catch(e => toast(e.message));
  $('#logFolderBtn').title = `Open ${S.info.log_dir}`;
  fillSelect($('#mathOp'), Object.entries(MATH_OPS).map(([k, o]) => [k, o.label]));
  $('#mathOp').onchange = $('#mathA').onchange = $('#mathB').onchange = onMathChange;
  $('#mathBtn').onclick = () => toggleMath(!mathSettings().enabled);
  $('#mathClose').onclick = () => toggleMath(false);
  $('#mathGraphBtn').onclick = () => {
    const m = mathSettings();
    m.graph = !m.graph;
    saveMath();
    updateMathOptions();
  };
  window.addEventListener('resize', () => draw(true));

  const result = await api('startup');
  showScan(result);
  for (const p of result.panels) addPanelEl(p);
  refreshLayout();

  setInterval(poll, POLL_MS);
  requestAnimationFrame(frame);
}

init().catch(e => notice('Could not start: ' + (e && e.message ? e.message : e), 'error'));
