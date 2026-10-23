(() => {
'use strict';

/* ------------------------------------------------------------------ helpers */
const main = document.getElementById('main');
const state = { cfg: null, demo: false, tz: 'America/Boise' };
let timers = [];
const charts = new Map();

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k in node && k !== 'list' && k !== 'form') node[k] = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  const add = (kid) => {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) kid.forEach(add);
    else node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  };
  kids.forEach(add);
  return node;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* not json */ }
  if (!res.ok) {
    let msg = body && body.detail;
    if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join('; ');
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return body;
}

const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const alpha = (hex, a) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex;
};
const TIER_LABEL = { on: 'On-peak', mid: 'Mid-peak', off: 'Off-peak', flat: 'Flat rate' };
const tierColor = (t) => css(`--${t}`);

const num = (v, d = 2) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (v) => (v < -0.005 ? '-$' : '$') + num(Math.abs(v), 2);
const cents = (dollars, d = 4) => {
  const s = (dollars * 100).toFixed(d).replace(/0+$/, '').replace(/\.$/, '');
  return s + '¢';
};
const kwh = (v) => num(v, v >= 100 ? 0 : v >= 10 ? 1 : 2) + ' kWh';
function power(w) {
  if (w == null) return ['—', ''];
  return Math.abs(w) >= 1000 ? [num(w / 1000, 2), 'kW'] : [String(Math.round(w)), 'W'];
}
const powerText = (w) => power(w).join(' ').trim();

const tzFmt = (opts) => new Intl.DateTimeFormat('en-US', { timeZone: state.tz, ...opts });
const fmtTime = (ts) => tzFmt({ hour: 'numeric', minute: '2-digit' }).format(new Date(ts * 1000));
const fmtDayTime = (ts) => tzFmt({ weekday: 'short', hour: 'numeric', minute: '2-digit' }).format(new Date(ts * 1000));
const todayStr = () => new Intl.DateTimeFormat('en-CA', { timeZone: state.tz }).format(new Date());
function addDays(str, n) {
  const [y, m, d] = str.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}
const prettyDate = (str) => new Date(str + 'T12:00:00Z').toLocaleDateString('en-US',
  { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const shortDate = (str) => new Date(str + 'T12:00:00Z').toLocaleDateString('en-US',
  { month: 'short', day: 'numeric', timeZone: 'UTC' });
function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const hourWord = (hr) => {
  const x = ((hr % 24) + 24) % 24;
  return (x % 12 || 12) + (x < 12 ? ' am' : ' pm');
};

function makeChart(key, canvas, config) {
  if (charts.has(key)) charts.get(key).destroy();
  const c = new Chart(canvas, config);
  charts.set(key, c);
  return c;
}
function clearView() {
  timers.forEach(clearInterval);
  timers = [];
  if (typeof liveCleanup === 'function') { liveCleanup(); liveCleanup = null; }
  charts.forEach((c) => c.destroy());
  charts.clear();
}
function setChartDefaults() {
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--line');
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 12;
  Chart.defaults.animation = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? false : { duration: 250 };
}
const errBox = (e) => h('div', { class: 'notice err', role: 'alert' }, e.message || String(e));
const loading = () => h('p', { class: 'muted' }, 'Loading…');
const ICONS = {
  bolt: '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>',
  curve: '<path d="M4 4v16h16"/><path d="M6 6c1 8 4 11 12 11"/>',
  receipt: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
};
function icon(name) {
  const span = document.createElement('span');
  span.innerHTML = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return span.firstChild;
}

/* ------------------------------------------------------------------ chart plugin: rate-tier shading */
const bandPlugin = {
  id: 'bands',
  beforeDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    if (!opts || !opts.bands) return;
    ctx.save();
    for (const b of opts.bands) {
      const x0 = Math.max(scales.x.getPixelForValue(b.s), chartArea.left);
      const x1 = Math.min(scales.x.getPixelForValue(b.e), chartArea.right);
      if (x1 <= x0) continue;
      ctx.fillStyle = opts.colors[b.tier] || 'transparent';
      ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
    }
    if (opts.now != null) {
      const x = scales.x.getPixelForValue(opts.now);
      if (x >= chartArea.left && x <= chartArea.right) {
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = css('--muted');
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
      }
    }
    ctx.restore();
  },
};

/* ------------------------------------------------------------------ small UI parts */
function tile(label, dot, valueNode, sub) {
  return h('div', { class: 'tile' },
    h('div', { class: 'label' }, dot ? h('i', { class: 'dot', style: { background: dot } }) : null, label),
    h('div', { class: 'value' }, valueNode),
    sub ? h('div', { class: 'sub' }, sub) : null);
}
function powerValue(w, abs = false) {
  const [v, u] = power(abs && w != null ? Math.abs(w) : w);
  return h('span', null, v, u ? h('small', null, u) : null);
}
function hbars(items, fmtRight, color) {
  const max = Math.max(...items.map((i) => i.value), 1e-9);
  return items.map((i) => h('div', { class: 'hbar' },
    h('span', null, i.dot ? h('i', { class: 'dot', style: { background: i.dot, marginRight: '6px' } }) : null, i.label),
    h('span', { class: 'muted' }, fmtRight(i)),
    h('div', { class: 'bar', role: 'presentation' }, h('i', { style: { width: `${Math.max(2, (i.value / max) * 100)}%`, background: i.dot || color || '' } }))));
}

/* ------------------------------------------------------------------ LIVE GRAPH (1 s refresh) */
function liveGraph() {
  let win = 300;                 // seconds shown
  let paused = false;
  let lastT = 0;
  let skew = 0;                  // server clock minus this browser's clock, in seconds
  let busy = false;
  let pts = [];
  const seen = { solar: false, battery: false };
  const nowEst = () => Date.now() / 1000 + skew;
  const clock = (ts) => tzFmt({ hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(new Date(ts * 1000));

  const canvas = h('canvas', { role: 'img', 'aria-label': 'Live power for the last few minutes' });
  const readout = h('div', { class: 'live-readout', 'aria-live': 'off' });
  const note = h('p', { class: 'muted small' });
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Time window' });
  const pauseBtn = h('button', { type: 'button', 'aria-pressed': 'false', onclick: () => {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    pauseBtn.setAttribute('aria-pressed', String(paused));
    if (!paused) tick();
  } }, 'Pause');
  const WINDOWS = [[60, '1 min'], [300, '5 min'], [900, '15 min']];
  const drawSeg = () => seg.replaceChildren(...WINDOWS.map(([w, label]) => h('button', {
    type: 'button', class: w === win ? 'active' : '', 'aria-pressed': String(w === win),
    onclick: () => { win = w; drawSeg(); redraw(); },
  }, label)));
  drawSeg();

  const defs = [
    { key: 'load', label: 'Home', color: css('--c-load'), fill: true },
    { key: 'grid', label: 'Grid', color: css('--c-grid') },
    { key: 'solar', label: 'Solar', color: css('--c-solar') },
    { key: 'battery', label: 'Battery', color: css('--c-battery') },
  ];
  let chart = null;
  const buildChart = () => makeChart('live', canvas, {
    type: 'line',
    data: { datasets: defs.map((d) => ({
      label: d.label, data: [], borderColor: d.color, backgroundColor: alpha(d.color, 0.14), fill: d.fill ? 'origin' : false,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, tension: 0.15, spanGaps: false, hidden: d.key === 'solar' || d.key === 'battery',
    })) },
    options: {
      responsive: true, maintainAspectRatio: false, parsing: false, animation: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      scales: {
        x: { type: 'linear', ticks: { maxTicksLimit: 6, callback: (v) => clock(v) }, grid: { display: false } },
        y: { title: { display: true, text: 'kW' }, grid: { color: css('--line') } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: (items) => (items[0] ? clock(items[0].parsed.x) : ''),
          label: (c) => `${c.dataset.label}: ${powerText(c.parsed.y * 1000)}`,
        } },
      },
    },
  });

  function redraw() {
    if (!chart) return;
    const now = nowEst();
    const from = now - win;
    chart.options.scales.x.min = from;
    chart.options.scales.x.max = now;
    chart.options.scales.x.ticks.stepSize = win / 5;
    defs.forEach((d, i) => {
      const data = [];
      let prev = null;
      for (const p of pts) {
        if (p.t < from - 5) continue;
        const v = p[d.key];
        if (v == null) continue;
        if (prev != null && p.t - prev > 60) data.push({ x: prev + 1, y: null });
        data.push({ x: p.t, y: v / 1000 });
        prev = p.t;
      }
      chart.data.datasets[i].data = data;
      chart.data.datasets[i].hidden = (d.key === 'solar' || d.key === 'battery') ? !seen[d.key] : false;
    });
    chart.update('none');

    const last = pts[pts.length - 1];
    const cells = defs.filter((d) => d.key === 'load' || d.key === 'grid' || seen[d.key]).map((d) => h('div', { class: 'live-cell' },
      h('span', { class: 'label' }, h('i', { class: 'dot', style: { background: d.color } }), d.label),
      h('span', { class: 'live-num' }, last && last[d.key] != null ? powerText(d.key === 'load' ? last[d.key] : last[d.key]) : '—')));
    readout.replaceChildren(...cells);

    if (!last) note.textContent = 'Waiting for readings. The graph fills in as they arrive.';
    else {
      const age = Math.round(now - last.t);
      const recent = pts.slice(-6);
      const gap = recent.length > 1 ? Math.round((recent[recent.length - 1].t - recent[0].t) / (recent.length - 1)) : null;
      note.textContent = age > 20 ? `No new reading for ${age} s. Check the collector on the Devices page.`
        : (gap ? `New reading about every ${gap} s${gap > 1 ? '. Set interval_seconds to 1 in the collector for a per-second graph.' : '.'}` : '');
    }
  }

  async function tick() {
    if (paused || busy || document.hidden) return;
    busy = true;
    try {
      const r = await api(`/api/live/history?since=${lastT}&seconds=900`);
      skew = r.now - Date.now() / 1000;
      for (const p of r.points) {
        pts.push(p);
        lastT = Math.max(lastT, p.t);
        if (p.solar != null) seen.solar = true;
        if (p.battery != null) seen.battery = true;
      }
      const cut = r.now - 900;
      if (pts.length && pts[0].t < cut) pts = pts.filter((p) => p.t >= cut);
      redraw();
    } catch (_) { /* keep the last picture; try again next second */ }
    busy = false;
  }

  setTimeout(() => { chart = buildChart(); tick(); }, 0);   // after the card is in the page, so it can size itself
  timers.push(setInterval(tick, 1000));
  const onVis = () => { if (!document.hidden) tick(); };
  document.addEventListener('visibilitychange', onVis);
  liveCleanup = () => document.removeEventListener('visibilitychange', onVis);

  return h('section', { class: 'card' },
    h('div', { class: 'row between' },
      h('h2', null, h('span', { class: 'live-dot', 'aria-hidden': 'true' }), 'Live'),
      h('div', { class: 'row' }, seg, pauseBtn)),
    readout,
    h('div', { class: 'chart-box short' }, canvas),
    note);
}
let liveCleanup = null;

/* ------------------------------------------------------------------ OVERVIEW */
async function viewOverview() {
  let day = todayStr();
  const banner = h('div');
  const features = h('div', { class: 'features' });
  const kpis = h('div', { class: 'kpis' });
  const dayTitle = h('h2', null, 'Power');
  const dateInput = h('input', { type: 'date', 'aria-label': 'Day to show', value: day, onchange: () => { if (dateInput.value) { day = dateInput.value; loadDay(); } } });
  const canvas = h('canvas', { role: 'img', 'aria-label': 'Power over the day, shaded by electricity rate tier' });
  const legend = h('div', { class: 'legend' });
  const daySummary = h('div', { class: 'grid tiles' });
  const tierBox = h('div');
  const circBox = h('div');
  const dayNote = h('div');

  const liveNode = liveGraph();
  const shift = (n) => { day = addDays(day, n); dateInput.value = day; loadDay(); };
  const hour = Number(tzFmt({ hour: 'numeric', hour12: false }).format(new Date())) % 24;
  const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  main.replaceChildren(h('div', { class: 'stack' },
    banner,
    h('div', { class: 'dash' },
      h('section', { class: 'card welcome' },
        h('h1', null, `${greeting}!`),
        h('p', { class: 'lede' }, 'Take a look at your home energy today'),
        features),
      kpis,
      liveNode,
      h('section', { class: 'card' },
        h('div', { class: 'row between' },
          dayTitle,
          h('div', { class: 'row' },
            h('button', { type: 'button', 'aria-label': 'Previous day', onclick: () => shift(-1) }, '‹'),
            dateInput,
            h('button', { type: 'button', 'aria-label': 'Next day', onclick: () => shift(1) }, '›'),
            h('button', { type: 'button', onclick: () => { day = todayStr(); dateInput.value = day; loadDay(); } }, 'Today'))),
        dayNote,
        h('div', { class: 'chart-box' }, canvas),
        legend)),
    h('section', { class: 'grid tiles' }, daySummary),
    h('div', { class: 'grid cols2' },
      h('section', { class: 'card' }, h('h2', null, 'Cost by rate tier'), tierBox),
      h('section', { class: 'card' }, h('h2', null, 'Where the energy went'), circBox))));

  async function loadLive() {
    try {
      const L = await api('/api/live');
      const t = L.tier;
      banner.replaceChildren();
      if (!L.online) {
        banner.append(h('div', { class: 'notice warn' }, L.last_update
          ? `No live readings in the last 3 minutes. Last data arrived ${ago(L.last_update)}.`
          : 'No readings yet. Open Devices to see how to connect the collector, or start the app in demo mode.'));
      }
      const gridSub = L.grid == null ? '' : L.grid > 20 ? 'Importing from the utility' : L.grid < -20 ? 'Exporting to the utility' : 'Idle';
      const battSub = L.battery == null ? '' : L.battery > 20 ? 'Charging' : L.battery < -20 ? 'Discharging' : 'Idle';
      const tot = L.today.totals;
      const feature = (ico, label, value, sub) => h('div', { class: 'feature' }, icon(ico), h('span', { class: 'f-label' }, label), h('div', { class: 'f-value' }, value), sub ? h('div', { class: 'f-sub' }, sub) : null);
      features.replaceChildren(
        feature('bolt', 'Home using', powerValue(L.load), L.load == null ? 'Waiting for data' : 'Right now'),
        feature('curve', 'Rate now', h('span', null, cents(t.rate, 2), h('small', null, '/kWh')),
          h('span', null, h('span', { class: `pill ${t.tier}` }, t.label), t.holiday ? ' Holiday' : '')),
        feature('receipt', 'Today so far', money(tot.estimated_bill), `${kwh(tot.import_kwh)} bought from the grid`));
      kpis.replaceChildren(...[
        tile('Grid', css('--c-grid'), powerValue(L.grid, true), gridSub),
        L.solar != null ? tile('Solar', css('--c-solar'), powerValue(L.solar), L.solar > 20 ? 'Producing' : 'Not producing') : null,
        L.battery != null ? tile('Battery', css('--c-battery'), powerValue(L.battery, true), battSub) : null,
        t.next ? tile('Next rate change', tierColor(t.next.tier), h('span', null, TIER_LABEL[t.next.tier]), `${fmtDayTime(t.next.at)} at ${cents(t.next.rate, 2)}/kWh`) : null,
      ].filter(Boolean));
    } catch (e) {
      banner.replaceChildren(errBox(e));
    }
  }

  async function loadDay() {
    const isToday = day === todayStr();
    dayTitle.textContent = isToday ? 'Power today' : `Power on ${prettyDate(day)}`;
    try {
      const D = await api(`/api/day?date=${encodeURIComponent(day)}`);
      const s = D.series;
      const hours = (D.end - D.start) / 3600;
      const pts = (k, scale = 1000) => {
        const out = [];
        let prev = null;
        for (const p of s.points) {
          if (prev && p.t - prev.t > s.res_min * 60 * 3) out.push({ x: (prev.t - D.start) / 3600 + 0.01, y: null, t: prev.t });
          out.push({ x: (p.t - D.start) / 3600, y: p[k] / scale, t: p.t });
          prev = p;
        }
        return out;
      };
      const hasSolar = s.points.some((p) => p.solar > 1);
      const ds = [{ label: 'Home', key: 'load', color: css('--c-load'), fill: true }];
      if (hasSolar) ds.push({ label: 'Solar', key: 'solar', color: css('--c-solar'), fill: true });
      ds.push({ label: 'Grid', key: 'grid', color: css('--c-grid') });
      if (s.has_battery) ds.push({ label: 'Battery', key: 'battery', color: css('--c-battery') });
      const colors = { on: css('--band-on'), mid: css('--band-mid'), off: css('--band-off'), flat: css('--band-flat') };
      makeChart('power', canvas, {
        type: 'line',
        plugins: [bandPlugin],
        data: {
          datasets: ds.map((d) => ({
            label: d.label, data: pts(d.key), borderColor: d.color, backgroundColor: alpha(d.color, 0.15),
            fill: d.fill ? 'origin' : false, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.2, spanGaps: false,
          })),
        },
        options: {
          responsive: true, maintainAspectRatio: false, parsing: false,
          interaction: { mode: 'nearest', axis: 'x', intersect: false },
          scales: {
            x: { type: 'linear', min: 0, max: hours, ticks: { stepSize: 3, callback: (v) => hourWord(v).replace(' ', '') }, grid: { display: false } },
            y: { title: { display: true, text: 'kW' }, grid: { color: css('--line') } },
          },
          plugins: {
            legend: { display: false },
            bands: { bands: D.bands.map((b) => ({ s: (b.start - D.start) / 3600, e: (b.end - D.start) / 3600, tier: b.tier })), colors, now: isToday ? (Date.now() / 1000 - D.start) / 3600 : null },
            tooltip: {
              callbacks: {
                title: (items) => (items[0] ? fmtTime(items[0].raw.t) : ''),
                label: (c) => `${c.dataset.label}: ${powerText(c.parsed.y * 1000)}`,
              },
            },
          },
        },
      });
      const tiersHere = [...new Set(D.bands.map((b) => b.tier))];
      legend.replaceChildren(
        ...ds.map((d) => h('span', null, h('i', { style: { background: d.color } }), d.label)),
        ...tiersHere.map((t) => h('span', null, h('i', { style: { background: alpha(tierColor(t), 0.35) } }), `${TIER_LABEL[t]} (${cents(D.bands.find((b) => b.tier === t).rate, 2)})`)));
      dayNote.replaceChildren(D.summary.has_data ? '' : h('p', { class: 'muted' }, 'No readings for this day. The shading still shows when each rate applies.'));

      const T = D.summary.totals;
      daySummary.replaceChildren(...[
        tile('Estimated cost', null, money(T.estimated_bill), `${D.plan}`),
        tile('Bought from grid', css('--c-grid'), kwh(T.import_kwh)),
        tile('Home used', css('--c-load'), kwh(T.load_kwh)),
        T.solar_kwh > 0 ? tile('Solar made', css('--c-solar'), kwh(T.solar_kwh), `Saved about ${money(T.solar_value)}`) : null,
      ].filter(Boolean));

      const tiers = Object.entries(D.summary.by_tier);
      tierBox.replaceChildren(...(T.import_kwh > 0
        ? hbars(tiers.map(([k, v]) => ({ label: TIER_LABEL[k], value: v.cost, kwh: v.kwh, cost: v.cost, dot: tierColor(k) })),
          (i) => `${money(i.cost)} · ${kwh(i.kwh)}`)
        : [h('p', { class: 'muted' }, 'Nothing bought from the grid this day.')]));
      const circ = D.summary.circuits;
      circBox.replaceChildren(...(circ.length
        ? hbars(circ.map((c) => ({ label: c.label, value: c.kwh, kwh: c.kwh, cost: c.cost })),
          (i) => `${kwh(i.kwh)} · ${money(i.cost)}`, css('--c-load'))
        : [h('p', { class: 'muted' }, 'No circuit or device channels reporting yet.')]));
    } catch (e) {
      dayNote.replaceChildren(errBox(e));
    }
  }

  loadLive();
  loadDay();
  timers.push(setInterval(loadLive, 5000));
  timers.push(setInterval(() => { if (day === todayStr()) loadDay(); }, 60000));
}

/* ------------------------------------------------------------------ REPORTS */
const PRESETS = [
  ['week', 'Last 7 days'], ['month', 'This month'], ['lastmonth', 'Last month'],
  ['30', 'Last 30 days'], ['ytd', 'Year to date'], ['custom', 'Custom'],
];
function presetRange(id) {
  const today = todayStr();
  const [y, m] = today.split('-').map(Number);
  const pad = (n) => String(n).padStart(2, '0');
  switch (id) {
    case 'week': return [addDays(today, -6), today];
    case '30': return [addDays(today, -29), today];
    case 'month': return [`${y}-${pad(m)}-01`, today];
    case 'lastmonth': {
      const ly = m === 1 ? y - 1 : y; const lm = m === 1 ? 12 : m - 1;
      return [`${ly}-${pad(lm)}-01`, addDays(`${y}-${pad(m)}-01`, -1)];
    }
    case 'ytd': return [`${y}-01-01`, today];
    default: return null;
  }
}

async function viewReports() {
  let preset = 'month';
  let [start, end] = presetRange('month');
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Period' });
  const startIn = h('input', { type: 'date', 'aria-label': 'Start date', value: start, onchange: () => { start = startIn.value; custom(); } });
  const endIn = h('input', { type: 'date', 'aria-label': 'End date', value: end, onchange: () => { end = endIn.value; custom(); } });
  const csv = h('a', { class: 'btn', href: '#' }, 'Download CSV');
  const out = h('div', { class: 'stack' });

  function custom() { preset = 'custom'; drawSeg(); load(); }
  function drawSeg() {
    seg.replaceChildren(...PRESETS.map(([id, label]) => h('button', {
      type: 'button', class: id === preset ? 'active' : '', 'aria-pressed': String(id === preset),
      onclick: () => {
        preset = id;
        const r = presetRange(id);
        if (r) { [start, end] = r; startIn.value = start; endIn.value = end; }
        drawSeg(); if (r) load();
      },
    }, label)));
  }
  drawSeg();
  main.replaceChildren(h('div', { class: 'stack' },
    h('section', { class: 'card' }, h('div', { class: 'row between' }, seg,
      h('div', { class: 'row' }, startIn, h('span', { class: 'muted' }, 'to'), endIn, csv))),
    out));

  let seq = 0;
  async function load() {
    if (!start || !end) return;
    if (end < start) [start, end] = [end, start];
    startIn.value = start; endIn.value = end;
    csv.href = `/api/export.csv?start_date=${start}&end_date=${end}`;
    csv.setAttribute('download', `energy_${start}_${end}.csv`);
    const mine = ++seq;
    out.replaceChildren(loading());
    let R;
    try { R = await api(`/api/range?start_date=${start}&end_date=${end}`); } catch (e) { if (mine === seq) out.replaceChildren(errBox(e)); return; }
    if (mine !== seq) return;
    charts.forEach((c) => c.destroy()); charts.clear();
    renderReport(R);
  }

  function renderReport(R) {
    const S = R.summary; const T = S.totals;
    if (!S.has_data) {
      out.replaceChildren(h('div', { class: 'card empty' }, 'No readings between these dates.'));
      return;
    }
    const tiers = Object.keys(S.by_tier);
    const bar1 = h('canvas', { role: 'img', 'aria-label': 'Daily cost, split by rate tier' });
    const bar2 = h('canvas', { role: 'img', 'aria-label': 'Daily energy used and produced' });

    const billRows = [
      ['Energy charges', T.energy_cost],
      ['Riders and adders', T.adders],
      ['Fixed monthly charge (prorated)', T.fixed],
    ];
    if (T.export_credit > 0) billRows.push(['Export credit', -T.export_credit]);

    const cmp = R.compare.plans;
    const cheapest = cmp.reduce((a, b) => (b.energy_cost < a.energy_cost ? b : a));
    const active = cmp.find((p) => p.active);
    let cmpNote = '';
    if (cmp.length > 1) {
      if (cheapest.id === active.id) cmpNote = `${active.name} was the cheapest of your plans for this usage.`;
      else {
        const diff = active.energy_cost - cheapest.energy_cost;
        cmpNote = `${cheapest.name} would have cost ${money(diff)} less in energy charges (${num((diff / active.energy_cost) * 100, 0)}%).`;
      }
    }

    out.replaceChildren(...[
      h('div', { class: 'grid tiles' },
        tile('Estimated bill', null, money(T.estimated_bill), `${S.plan} · ${shortDate(R.start_date)} to ${shortDate(R.end_date)}`),
        tile('Bought from grid', css('--c-grid'), kwh(T.import_kwh), T.import_kwh > 0 ? `Average ${cents(T.energy_cost / T.import_kwh, 2)}/kWh` : ''),
        tile('Home used', css('--c-load'), kwh(T.load_kwh)),
        T.solar_kwh > 0 ? tile('Solar made', css('--c-solar'), kwh(T.solar_kwh), `${kwh(T.solar_used_kwh)} used at home`) : null,
        T.solar_kwh > 0 ? tile('Solar saved', css('--c-solar'), money(T.solar_value), 'Value at the rate you would have paid') : null,
        T.export_kwh > 0 ? tile('Sent to grid', css('--c-grid'), kwh(T.export_kwh), T.export_credit > 0 ? `Credit ${money(T.export_credit)}` : 'No export credit set') : null),
      h('section', { class: 'card' }, h('h2', null, 'Cost per day'),
        h('div', { class: 'chart-box' }, bar1),
        h('div', { class: 'legend', style: { marginTop: '8px' } }, tiers.map((t) => h('span', null, h('i', { style: { background: tierColor(t) } }), TIER_LABEL[t])))),
      h('section', { class: 'card' }, h('h2', null, 'Energy per day'),
        h('div', { class: 'chart-box short' }, bar2)),
      h('div', { class: 'grid cols2' },
        h('section', { class: 'card' }, h('h2', null, 'By rate tier'),
          h('div', { class: 'table-wrap' }, h('table', null,
            h('thead', null, h('tr', null, h('th', null, 'Tier'), h('th', { class: 'num' }, 'kWh bought'), h('th', { class: 'num' }, 'Cost'), h('th', { class: 'num' }, 'Share'))),
            h('tbody', null, tiers.map((t) => {
              const v = S.by_tier[t];
              return h('tr', null,
                h('td', null, h('i', { class: 'dot', style: { background: tierColor(t), marginRight: '6px' } }), TIER_LABEL[t]),
                h('td', { class: 'num' }, num(v.kwh, 1)), h('td', { class: 'num' }, money(v.cost)),
                h('td', { class: 'num' }, T.energy_cost > 0 ? num((v.cost / T.energy_cost) * 100, 0) + '%' : '—'));
            }))))),
        h('section', { class: 'card' }, h('h2', null, 'Bill estimate'),
          h('div', { class: 'table-wrap' }, h('table', null, h('tbody', null,
            billRows.map(([k, v]) => h('tr', null, h('td', null, k), h('td', { class: 'num' }, money(v)))),
            h('tr', null, h('td', null, h('strong', null, 'Estimated total')), h('td', { class: 'num' }, h('strong', null, money(T.estimated_bill))))))),
          h('p', { class: 'muted small', style: { marginTop: '8px' } }, 'Set the fixed charge and riders on the Rates page to match your real bill. Taxes and other fees may not be included.'))),
      S.circuits.length ? h('section', { class: 'card' }, h('h2', null, 'By circuit and device'),
        h('div', { class: 'table-wrap' }, h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Name'), h('th', { class: 'num' }, 'kWh'), h('th', { class: 'num' }, 'Cost'), h('th', null, ''))),
          h('tbody', null, (() => {
            const max = Math.max(...S.circuits.map((c) => c.kwh), 1e-9);
            return S.circuits.map((c) => h('tr', null,
              h('td', null, c.label), h('td', { class: 'num' }, num(c.kwh, 1)), h('td', { class: 'num' }, money(c.cost)),
              h('td', { style: { width: '30%' } }, h('div', { class: 'bar' }, h('i', { style: { width: `${(c.kwh / max) * 100}%`, background: css('--c-load') } })))));
          })()))),
        h('p', { class: 'muted small', style: { marginTop: '8px' } }, 'Costs use the rate in force during the hours each circuit ran. Plug and circuit channels can overlap, so do not add them up to the home total.')) : null,
      cmp.length > 1 ? h('section', { class: 'card' }, h('h2', null, 'Plan comparison'),
        h('p', null, cmpNote),
        h('div', { class: 'table-wrap' }, h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Plan'), h('th', { class: 'num' }, 'Energy charges'), h('th', { class: 'num' }, 'Difference'))),
          h('tbody', null, cmp.map((p) => h('tr', null,
            h('td', null, p.name, p.active ? h('span', { class: 'badge', style: { marginLeft: '8px' } }, 'In use') : null),
            h('td', { class: 'num' }, money(p.energy_cost)),
            h('td', { class: 'num' }, p.id === R.compare.baseline ? 'baseline' : `${p.vs_standard > 0 ? '+' : ''}${money(p.vs_standard)}`)))))),
        h('p', { class: 'muted small', style: { marginTop: '8px' } }, 'Same usage priced on each plan, energy charges only. Fixed charges, riders and any trial protection on a plan, such as a bill cap, are not modeled. The Standard plan here is a flat estimate unless you add its usage blocks on the Rates page.')) : null,
    ].filter(Boolean));

    const days = S.by_day;
    makeChart('cost', bar1, {
      type: 'bar',
      data: {
        labels: days.map((d) => shortDate(d.date)),
        datasets: tiers.map((t) => ({ label: TIER_LABEL[t], data: days.map((d) => d.tiers[t]), backgroundColor: tierColor(t), borderRadius: 2 })),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { x: { stacked: true, grid: { display: false }, ticks: { maxTicksLimit: 12, autoSkip: true } }, y: { stacked: true, ticks: { callback: (v) => '$' + v } } },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } } },
      },
    });
    const hasSolar = T.solar_kwh > 0;
    makeChart('energy', bar2, {
      type: 'bar',
      data: {
        labels: days.map((d) => shortDate(d.date)),
        datasets: [
          { label: 'Home used', data: days.map((d) => d.load_kwh), backgroundColor: css('--c-load'), borderRadius: 2 },
          { label: 'Bought from grid', data: days.map((d) => d.import_kwh), backgroundColor: css('--c-grid'), borderRadius: 2 },
          hasSolar ? { label: 'Solar made', data: days.map((d) => d.solar_kwh), backgroundColor: css('--c-solar'), borderRadius: 2 } : null,
        ].filter(Boolean),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12, autoSkip: true } }, y: { title: { display: true, text: 'kWh' } } },
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 12 } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${num(c.parsed.y, 1)} kWh` } } },
      },
    });
  }
  load();
}

/* ------------------------------------------------------------------ RATES */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function describeWindows(windows) {
  return windows.map(([a, b]) => `${hourWord(a)} to ${hourWord(b)}`).join(' and ');
}

async function viewRates() {
  const draft = JSON.parse(JSON.stringify(state.cfg));
  const status = h('span', { role: 'status', class: 'small' });
  const round6 = (v) => Math.round(v * 1e6) / 1e6;

  const centsInput = (get, set, label) => h('input', {
    type: 'number', step: '0.0001', min: '0', value: String(+(get() * 100).toFixed(4)), 'aria-label': label,
    oninput: (e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) set(round6(v / 100)); },
  });
  const field = (label, input, hint) => h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'small', style: { fontWeight: 400 } }, hint) : null);

  function touCard(pid, plan) {
    const seasons = [['summer', 'Summer'], ['non_summer', 'Non-summer']];
    const sm = (plan.summer_months || []).map((m) => MONTHS[m - 1]).join(', ');
    return h('section', { class: 'card' },
      h('h2', null, plan.name),
      h('p', { class: 'muted small' }, `Summer months: ${sm || 'none'}. Rates are cents per kWh.`),
      h('div', { class: 'grid cols2' }, seasons.map(([sk, sl]) => h('div', null,
        h('h3', { style: { marginBottom: '8px' } }, sl),
        h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))' } },
          Object.keys(plan.tiers[sk]).map((tier) => field(`${TIER_LABEL[tier]} ¢/kWh`,
            centsInput(() => plan.tiers[sk][tier], (v) => { plan.tiers[sk][tier] = v; }, `${sl} ${TIER_LABEL[tier]} rate in cents`))) ),
        h('p', { class: 'muted small', style: { marginTop: '8px' } },
          Object.entries((plan.schedule || {})[sk] || {}).map(([tier, w]) => `${TIER_LABEL[tier]}: ${describeWindows(w)}, Mon to Sat. `),
          'Everything else, Sundays and holidays are off-peak.')))),
      h('p', { class: 'muted small', style: { marginTop: '8px' } }, 'To change the hours or months, edit the plan in the advanced JSON below.'));
  }

  function blocksCard(pid, plan) {
    const body = h('tbody');
    const draw = () => {
      body.replaceChildren(...plan.blocks.map((b, i) => {
        const last = i === plan.blocks.length - 1;
        return h('tr', null,
          h('td', null, last ? 'All remaining usage' : h('span', null, 'Up to ',
            h('input', { type: 'number', min: '0', step: '1', value: String(b.up_to_kwh ?? ''), 'aria-label': `Block ${i + 1} limit in kWh per month`,
              oninput: (e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) b.up_to_kwh = v; } }), ' kWh/month')),
          h('td', null, centsInput(() => b.rate, (v) => { b.rate = v; }, `Block ${i + 1} rate in cents`), ' ¢/kWh'),
          h('td', null, plan.blocks.length > 1 ? h('button', { type: 'button', 'aria-label': `Remove block ${i + 1}`, onclick: () => {
            plan.blocks.splice(i, 1); plan.blocks[plan.blocks.length - 1].up_to_kwh = null; draw();
          } }, 'Remove') : null));
      }));
    };
    draw();
    return h('section', { class: 'card' },
      h('h2', null, plan.name),
      h('p', { class: 'muted small' }, 'Priced by monthly usage. One block is a flat rate. Add blocks if your plan has usage steps.'),
      h('div', { class: 'table-wrap' }, h('table', null, h('tbody', null), body)),
      h('div', { class: 'row', style: { marginTop: '8px' } }, h('button', { type: 'button', onclick: () => {
        const last = plan.blocks[plan.blocks.length - 1];
        const prev = plan.blocks.length > 1 ? plan.blocks[plan.blocks.length - 2].up_to_kwh : 0;
        plan.blocks.splice(plan.blocks.length - 1, 0, { up_to_kwh: (prev || 0) + 500, rate: last.rate });
        draw();
      } }, 'Add a usage block')));
  }

  const planSel = h('select', { 'aria-label': 'Plan used for costs', onchange: (e) => { draft.active_plan = e.target.value; } },
    Object.entries(draft.plans).map(([id, p]) => h('option', { value: id, selected: id === draft.active_plan }, p.name)));
  const tzList = (typeof Intl.supportedValuesOf === 'function') ? Intl.supportedValuesOf('timeZone') : [];
  const jsonBox = h('textarea', { spellcheck: false, 'aria-label': 'Full configuration as JSON' }, JSON.stringify(state.cfg, null, 2));
  const jsonStatus = h('span', { role: 'status', class: 'small' });

  async function save(payload, statusEl) {
    statusEl.className = 'small'; statusEl.textContent = 'Saving…';
    try {
      const res = await api('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: payload }) });
      state.cfg = res.config; state.tz = res.config.timezone;
      statusEl.className = 'small ok'; statusEl.textContent = 'Saved';
      jsonBox.value = JSON.stringify(state.cfg, null, 2);
    } catch (e) {
      statusEl.className = 'small err'; statusEl.textContent = e.message;
    }
  }

  main.replaceChildren(h('div', { class: 'stack' },
    h('section', { class: 'card' }, h('h2', null, 'Plan and charges'),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))' } },
        field('Plan used for costs', planSel, 'Reports can still compare all plans'),
        field('Fixed charge $/month', h('input', { type: 'number', step: '0.01', min: '0', value: String(draft.fixed_monthly), oninput: (e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v)) draft.fixed_monthly = v; } }), 'Basic charge from your bill'),
        field('Riders and adders ¢/kWh', centsInput(() => draft.adder_per_kwh, (v) => { draft.adder_per_kwh = v; }, 'Adders in cents per kWh'), 'Added to every kWh bought'),
        field('Export credit ¢/kWh', centsInput(() => draft.export_credit_per_kwh, (v) => { draft.export_credit_per_kwh = v; }, 'Export credit in cents per kWh'), '0 if you get none'),
        field('Time zone', h('input', { type: 'text', list: 'tzs', value: draft.timezone, oninput: (e) => { draft.timezone = e.target.value.trim(); } }), 'Rate hours follow this clock'),
        h('datalist', { id: 'tzs' }, tzList.map((z) => h('option', { value: z }))))),
    Object.entries(draft.plans).map(([id, p]) => (p.type === 'tou' ? touCard(id, p) : blocksCard(id, p))),
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'primary', onclick: () => save({
        timezone: draft.timezone, active_plan: draft.active_plan, fixed_monthly: draft.fixed_monthly,
        adder_per_kwh: draft.adder_per_kwh, export_credit_per_kwh: draft.export_credit_per_kwh, plans: draft.plans,
      }, status) }, 'Save rates'), status),
    h('details', { class: 'card' },
      h('summary', { style: { cursor: 'pointer', fontWeight: 650 } }, 'Advanced: edit all settings as JSON'),
      h('p', { class: 'muted small', style: { margin: '8px 0' } }, 'Use this to change tier hours, summer months or add plans. Invalid settings are rejected with a message.'),
      jsonBox,
      h('div', { class: 'row', style: { marginTop: '8px' } },
        h('button', { type: 'button', onclick: () => {
          let parsed;
          try { parsed = JSON.parse(jsonBox.value); } catch (e) { jsonStatus.className = 'small err'; jsonStatus.textContent = `Not valid JSON: ${e.message}`; return; }
          save(parsed, jsonStatus);
        } }, 'Apply JSON'), jsonStatus)),
    h('p', { class: 'muted small' }, 'This dashboard has no sign-in. Keep it on your home network, and do not expose it to the internet.')));
}

/* ------------------------------------------------------------------ DEVICES */
/* ------------------------------------------------------------------ DEVICES: add and list */
const SOURCE_LABEL = { tapo: 'Tapo smart plug', refoss: 'Refoss circuit', inverter: 'Solar inverter', other: 'Something else' };
const KIND_LABEL = { device: 'Smart plug or appliance', circuit: 'Circuit or breaker' };
const slug = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40);

function collectorSnippet(devs) {
  const sources = [];
  const tapo = devs.filter((d) => d.source === 'tapo');
  if (tapo.length) {
    sources.push({ type: 'tapo', email: 'YOUR_TAPO_EMAIL', password: 'YOUR_TAPO_PASSWORD',
      devices: Object.fromEntries(tapo.map((d) => [d.host || 'IP_ADDRESS', d.id])) });
  }
  const ref = devs.filter((d) => d.source === 'refoss');
  if (ref.length) {
    sources.push({ type: 'refoss', host: (ref.find((d) => d.host) || {}).host || 'IP_ADDRESS',
      circuits: Object.fromEntries(ref.map((d) => [`CHANNEL_NUMBER_FOR_${d.id}`, d.id])) });
  }
  return sources;
}

function deviceManager(C, rerender) {
  const devs = (state.cfg.devices || []).map((d) => ({ ...d }));
  const byChannel = Object.fromEntries(C.channels.map((c) => [c.channel, c]));
  const status = h('span', { role: 'status', class: 'small' });

  async function save(nextDevs, nextLabels) {
    status.className = 'small'; status.textContent = 'Saving…';
    try {
      const res = await api('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: { devices: nextDevs, labels: nextLabels } }) });
      state.cfg = res.config;
      rerender();
    } catch (e) { status.className = 'small err'; status.textContent = e.message; }
  }

  const nameIn = h('input', { type: 'text', maxlength: '60', placeholder: 'Freezer plug', 'aria-label': 'Device name', required: true });
  const hostIn = h('input', { type: 'text', maxlength: '100', placeholder: '192.168.1.60', 'aria-label': 'IP address or host name' });
  const sourceSel = h('select', { 'aria-label': 'What it connects through', onchange: () => syncKind() },
    ['tapo', 'refoss', 'other'].map((k) => h('option', { value: k }, SOURCE_LABEL[k])));
  const kindSel = h('select', { 'aria-label': 'Type of device' },
    Object.entries(KIND_LABEL).map(([k, v]) => h('option', { value: k }, v)));
  const hint = h('p', { class: 'muted small' });
  function syncKind() {
    const src = sourceSel.value;
    if (src === 'tapo') { kindSel.value = 'device'; kindSel.disabled = true; hint.textContent = 'Smart plugs report one device each. Enter the plug\'s IP address.'; }
    else if (src === 'refoss') { kindSel.value = 'circuit'; kindSel.disabled = true; hint.textContent = 'Circuits come from the Refoss EM16P. Enter its IP address (shared by all its circuits).'; }
    else { kindSel.disabled = false; hint.textContent = 'For anything else, your own script sends readings to the Ingest API with this device\'s channel name.'; }
  }
  syncKind();

  const form = h('form', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', alignItems: 'end' }, onsubmit: (e) => {
    e.preventDefault();
    const name = nameIn.value.trim();
    let id = slug(name);
    if (!id) { status.className = 'small err'; status.textContent = 'Give the device a name using letters or numbers.'; return; }
    const kind = kindSel.value;
    const base = id; let n = 2;
    while (devs.some((d) => d.kind === kind && d.id === id)) id = `${base}_${n++}`;
    const labels = { ...(state.cfg.labels || {}), [`${kind}:${id}`]: name };
    save([...devs, { id, name, kind, source: sourceSel.value, host: hostIn.value.trim() }], labels);
  } },
  h('label', { class: 'field' }, 'Name', nameIn),
  h('label', { class: 'field' }, 'Connects through', sourceSel),
  h('label', { class: 'field' }, 'Type', kindSel),
  h('label', { class: 'field' }, 'IP address', hostIn),
  h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add device')));

  const rows = devs.map((d) => {
    const ch = `${d.kind}:${d.id}`;
    const seen = byChannel[ch];
    return h('tr', null,
      h('td', null, d.name),
      h('td', null, h('code', null, ch)),
      h('td', null, SOURCE_LABEL[d.source] || d.source, d.host ? h('span', { class: 'muted' }, ` · ${d.host}`) : null),
      h('td', null, h('span', { class: `status-dot${seen && seen.online ? ' live' : ''}` }), !seen ? 'Waiting for first reading' : seen.online ? 'Receiving' : 'Quiet'),
      h('td', { class: 'num' }, h('button', { type: 'button', 'aria-label': `Remove ${d.name}`, onclick: () => {
        const labels = { ...(state.cfg.labels || {}) }; delete labels[ch];
        save(devs.filter((x) => x !== d && !(x.id === d.id && x.kind === d.kind)), labels);
      } }, 'Remove')));
  });
  const snippets = collectorSnippet(devs);

  return h('section', { class: 'card' },
    h('h2', null, 'Your devices'),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', null, 'Name'), h('th', null, 'Channel'), h('th', null, 'Connects through'), h('th', null, 'Status'), h('th', null, ''))),
      h('tbody', null, rows)))
      : h('p', { class: 'muted' }, 'No devices added yet. Add a smart plug or circuit below.'),
    h('h3', { style: { margin: '14px 0 8px' } }, 'Add a device'),
    form, hint, status,
    snippets.length ? h('div', { style: { marginTop: '12px' } },
      h('h3', { style: { marginBottom: '6px' } }, 'Collector settings for these devices'),
      h('p', { class: 'muted small' }, 'Add these entries to "sources" in collector.json on the Raspberry Pi. Removing a device here does not delete its saved history.'),
      h('pre', null, JSON.stringify(snippets, null, 2)),
      h('p', { class: 'muted small', style: { marginTop: '6px' } }, 'The Tapo and Refoss readers in the collector are not finished yet (see the files in collector/sources), so a device shows "Waiting for first reading" until they are.'))
      : null);
}

async function viewDevices() {
  main.replaceChildren(loading());
  let C; let H;
  try { [C, H] = await Promise.all([api('/api/channels'), api('/api/health')]); } catch (e) { main.replaceChildren(errBox(e)); return; }
  const labels = { ...(state.cfg.labels || {}) };
  const status = h('span', { role: 'status', class: 'small' });
  const base = location.origin;
  const snippet = `curl -X POST ${base}/api/ingest \\
  -H "X-API-Key: YOUR_INGEST_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"readings": {"grid": 1250, "solar": 0, "circuit:hvac": 640, "device:desk_plug": 85}}'`;

  main.replaceChildren(h('div', { class: 'stack' },
    deviceManager(C, viewDevices),
    h('section', { class: 'card' }, h('h2', null, 'Channels'),
      C.channels.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', null, 'Channel'), h('th', null, 'Name shown'), h('th', null, 'Last reading'), h('th', null, 'Status'))),
        h('tbody', null, C.channels.map((c) => h('tr', null,
          h('td', null, h('code', null, c.channel)),
          h('td', null, h('input', { type: 'text', value: labels[c.channel] || '', placeholder: c.label, 'aria-label': `Name for ${c.channel}`,
            oninput: (e) => { const v = e.target.value.trim(); if (v) labels[c.channel] = v; else delete labels[c.channel]; } })),
          h('td', null, ago(c.last)),
          h('td', null, h('span', { class: `status-dot${c.online ? ' live' : ''}` }), c.online ? 'Receiving' : 'Quiet'))))))
        : h('p', { class: 'muted' }, 'No channels yet. Once the collector sends readings they appear here.'),
      C.channels.length ? h('div', { class: 'row', style: { marginTop: '10px' } }, h('button', { type: 'button', class: 'primary', onclick: async () => {
        status.className = 'small'; status.textContent = 'Saving…';
        try {
          const res = await api('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: { labels } }) });
          state.cfg = res.config; status.className = 'small ok'; status.textContent = 'Saved';
        } catch (e) { status.className = 'small err'; status.textContent = e.message; }
      } }, 'Save names'), status) : null),
    h('section', { class: 'card' }, h('h2', null, 'Data source'),
      h('p', null, H.demo ? 'Showing simulated demo data.' : 'Showing your real readings.'),
      h('p', { class: 'muted small' }, 'Demo and real data are stored separately, so switching never mixes or deletes either one.'),
      H.mode_locked ? h('p', { class: 'muted small' }, 'The mode is fixed because DB_PATH is set on the server.')
        : h('div', { class: 'row' }, h('button', { type: 'button', class: 'primary', onclick: async (e) => {
          e.target.disabled = true;
          try { await setMode(!H.demo); } catch (err) { main.replaceChildren(errBox(err)); }
        } }, H.demo ? 'Switch to real data' : 'Switch to demo data'))),
    h('section', { class: 'card' }, h('h2', null, 'Send readings'),
      H.ingest_enabled ? null : h('div', { class: 'notice warn', style: { margin: '10px 0' } }, 'Ingest is off. Set the INGEST_KEY environment variable on the server and restart it to accept readings from the collector.'),
      H.demo ? h('div', { class: 'notice', style: { margin: '10px 0' } }, 'Demo mode is on, so the numbers are simulated.') : null,
      h('p', { class: 'muted' }, 'The collector posts watts to the server. Positive grid watts mean importing, negative mean exporting. Battery watts are positive while charging.'),
      h('pre', null, snippet),
      h('div', { class: 'table-wrap', style: { marginTop: '10px' } }, h('table', null,
        h('thead', null, h('tr', null, h('th', null, 'Channel'), h('th', null, 'Meaning'))),
        h('tbody', null, [
          ['grid', 'Net power at the utility meter (+ import, − export)'],
          ['solar', 'Total solar production'],
          ['battery', 'Battery power (+ charging, − discharging)'],
          ['circuit:<name>', 'A monitored circuit, such as circuit:hvac'],
          ['device:<name>', 'A smart plug or single appliance'],
        ].map(([a, b]) => h('tr', null, h('td', null, h('code', null, a)), h('td', null, b)))))))));
}

/* ------------------------------------------------------------------ demo / real mode */
async function setMode(demo) {
  await api('/api/mode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ demo }) });
  const c = await api('/api/config');
  state.cfg = c.config; state.tz = c.config.timezone; state.demo = c.demo;
  document.getElementById('demo-badge').hidden = !c.demo;
  route();
}

/* ------------------------------------------------------------------ router and theme */
const ROUTES = { overview: viewOverview, reports: viewReports, rates: viewRates, devices: viewDevices };
async function route() {
  clearView();
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  const key = ROUTES[name] ? name : 'overview';
  document.querySelectorAll('.tabs a').forEach((a) => {
    if (a.dataset.route === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.title = `${key[0].toUpperCase() + key.slice(1)} · Home energy`;
  setChartDefaults();
  main.replaceChildren(loading());
  try { await ROUTES[key](); } catch (e) { main.replaceChildren(errBox(e)); }
}

function applyTheme(t) {
  if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
}
try { applyTheme(localStorage.getItem('theme')); } catch (_) { /* storage blocked */ }
document.getElementById('theme').addEventListener('click', () => {
  const dark = document.documentElement.getAttribute('data-theme') !== 'light';
  const next = dark ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('theme', next); } catch (_) { /* ignore */ }
  route();
});
window.addEventListener('hashchange', route);
document.getElementById('refresh').addEventListener('click', () => route());
document.getElementById('demo-badge').addEventListener('click', async (e) => {
  e.target.disabled = true; e.target.textContent = 'Switching…';
  try { await setMode(false); } catch (err) { main.replaceChildren(errBox(err)); }
  e.target.disabled = false; e.target.textContent = 'Demo data · switch to real';
});

(async () => {
  try {
    const c = await api('/api/config');
    state.cfg = c.config; state.tz = c.config.timezone; state.demo = c.demo;
    document.getElementById('demo-badge').hidden = !c.demo;
  } catch (e) {
    main.replaceChildren(errBox(e));
    return;
  }
  route();
})();
})();
