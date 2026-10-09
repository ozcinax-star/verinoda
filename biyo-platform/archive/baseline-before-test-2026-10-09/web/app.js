'use strict';

/* =========================================================================================
 * Data provider seam. Everything the page knows about the backend is this one function.
 * To plug in EBA later, point it at the new endpoint and keep the returned shape
 * ({query, center, nodes, edges, unknowns}, see SPEC.md).
 * ========================================================================================= */
async function fetchGraph(q) {
  const res = await fetch('/api/graph?q=' + encodeURIComponent(q), { headers: { Accept: 'application/json' } });
  if (!res.ok && res.status !== 500) throw new Error('HTTP ' + res.status);
  return res.json();
}

/* ---------------------------------------------------------------------------------------- */

const SVG_NS = 'http://www.w3.org/2000/svg';
const TYPE_TR = { onkosul: 'Ön koşul', destek: 'Destek / uygulama', ortak: 'Ortak kavram' };
const STATUS_TR = {
  verified: 'Doğrulandı (metinde alıntısı var)',
  inference: 'Çıkarım (alıntı yok, müfredat bilgisine dayanıyor)',
  unknown: 'Bilinmiyor (doğrulanamadı)',
};
const STATUS_SHORT = { verified: 'Doğrulandı', inference: 'Çıkarım', unknown: 'Bilinmiyor' };
const COLORS = { center: '#7cf2a6', onkosul: '#f6b44c', destek: '#4cc9f0', ortak: '#b892ff' };
const CHIPS = ['fotosentez', 'mitoz', 'sinir sistemi', 'kalıtım', 'ekosistem', 'enzimler', 'DNA'];
// Where each group of branches sits around the centre (degrees; 0 = right, 90 = down).
const SECTORS = {
  before: { angle: 180, span: 110, label: 'Önce çalış' },
  after: { angle: 0, span: 110, label: 'Sonra çalış' },
  destek: { angle: 270, span: 80, label: 'Destekler' },
  ortak: { angle: 90, span: 80, label: 'Ortak kavram' },
};

const $ = (sel) => document.querySelector(sel);
const state = { data: null, selected: null, lastFocus: null };

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  return node;
}

const gradesText = (g) => '(' + (g || []).join('-') + ')';
const nodeLabel = (n) => `${n.title} ${gradesText(n.grades)}`;
const byId = (id) => state.data.nodes.find((n) => n.id === id);
const titleOf = (id) => (byId(id) || { title: id }).title;

/* ------------------------------------ search ------------------------------------------- */

async function runSearch(q) {
  q = (q || '').trim();
  if (!q) return;
  $('#q').value = q;
  $('#loading').hidden = false;
  closePanel();
  try {
    const data = await fetchGraph(q);
    state.data = data;
    const url = new URL(location.href);
    url.searchParams.set('q', q);
    history.replaceState(null, '', url);
    render();
  } catch (err) {
    state.data = { query: q, center: null, nodes: [], edges: [], unknowns: ['Sunucuya ulaşılamadı: ' + err.message] };
    render();
  } finally {
    $('#loading').hidden = true;
  }
}

function renderNotes(unknowns) {
  const list = $('#unknowns');
  list.replaceChildren(...(unknowns || []).map((u) => el('li', { text: u })));
  $('#notes').hidden = !(unknowns && unknowns.length);
}

/* ------------------------------------ graph -------------------------------------------- */

function groupOf(edge, center) {
  if (edge.type === 'onkosul') return edge.target === center ? 'before' : 'after';
  return edge.type;
}

function wrapTitle(title) {
  if (title.length <= 18) return [title];
  const mid = Math.floor(title.length / 2);
  let cut = -1;
  for (let d = 0; d < mid; d++) {
    if (title[mid + d] === ' ') { cut = mid + d; break; }
    if (title[mid - d] === ' ') { cut = mid - d; break; }
  }
  return cut < 0 ? [title] : [title.slice(0, cut), title.slice(cut + 1)];
}

function layout(data, w, h) {
  const cx = w / 2;
  const cy = h / 2;
  const R = Math.max(120, Math.min(w * 0.36, h * 0.38));
  const pos = { [data.center]: { x: cx, y: cy, fixed: true } };
  const groups = { before: [], after: [], destek: [], ortak: [] };
  for (const e of data.edges) {
    const other = e.source === data.center ? e.target : e.source;
    groups[groupOf(e, data.center)].push(other);
  }
  for (const [g, ids] of Object.entries(groups)) {
    const s = SECTORS[g];
    ids.forEach((id, i) => {
      const n = ids.length;
      const a = ((n === 1 ? s.angle : s.angle - s.span / 2 + (s.span * (i + 0.5)) / n) * Math.PI) / 180;
      const r = R * (n > 3 && i % 2 ? 0.78 : 1);
      pos[id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a), group: g };
    });
  }
  // Push overlapping label boxes apart so titles stay readable.
  const boxes = Object.entries(pos).map(([id, p]) => {
    const n = data.nodes.find((x) => x.id === id);
    const lines = wrapTitle(n ? n.title : id);
    const chars = Math.max(...lines.map((l) => l.length)) + (lines.length === 1 ? 8 : 0);
    return { id, p, w: Math.max(40, chars * 7.2), h: 30 + lines.length * 16 };
  });
  const margin = 12;
  for (let iter = 0; iter < 120; iter++) {
    let moved = false;
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const A = boxes[i];
        const B = boxes[j];
        const dx = B.p.x - A.p.x;
        const dy = B.p.y + B.h / 2 - (A.p.y + A.h / 2);
        const ox = (A.w + B.w) / 2 + 6 - Math.abs(dx);
        const oy = (A.h + B.h) / 2 + 2 - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          moved = true;
          const alongY = oy < ox;
          const push = (alongY ? oy : ox) / 2 + 0.5;
          const sx = alongY ? 0 : (dx >= 0 ? 1 : -1) * push;
          const sy = alongY ? (dy >= 0 ? 1 : -1) * push : 0;
          if (A.p.fixed) { B.p.x += 2 * sx; B.p.y += 2 * sy; }
          else if (B.p.fixed) { A.p.x -= 2 * sx; A.p.y -= 2 * sy; }
          else { A.p.x -= sx; A.p.y -= sy; B.p.x += sx; B.p.y += sy; }
        }
      }
    }
    for (const b of boxes) {
      if (b.p.fixed) continue;
      b.p.x = Math.min(w - b.w / 2 - margin, Math.max(b.w / 2 + margin, b.p.x));
      b.p.y = Math.min(h - b.h - margin, Math.max(margin + 14, b.p.y));
    }
    if (!moved) break;
  }
  return { pos, R, cx, cy, groups };
}

function curve(p1, p2, r1, r2, bend) {
  const mx = (p1.x + p2.x) / 2;
  const my = (p1.y + p2.y) / 2;
  const dx = p2.x - p1.x;
  const dy = p2.y - p1.y;
  const len = Math.hypot(dx, dy) || 1;
  const qx = mx - (dy / len) * len * bend;
  const qy = my + (dx / len) * len * bend;
  const trim = (from, to, r) => {
    const vx = to.x - from.x;
    const vy = to.y - from.y;
    const l = Math.hypot(vx, vy) || 1;
    return { x: to.x - (vx / l) * r, y: to.y - (vy / l) * r };
  };
  const s = trim({ x: qx, y: qy }, p1, r1);
  const t = trim({ x: qx, y: qy }, p2, r2);
  return `M${s.x.toFixed(1)},${s.y.toFixed(1)} Q${qx.toFixed(1)},${qy.toFixed(1)} ${t.x.toFixed(1)},${t.y.toFixed(1)}`;
}

function defs() {
  const d = svg('defs');
  const glow = svg('filter', { id: 'glow', x: '-80%', y: '-80%', width: '260%', height: '260%' });
  glow.append(svg('feGaussianBlur', { stdDeviation: '4', result: 'b' }));
  const merge = svg('feMerge');
  merge.append(svg('feMergeNode', { in: 'b' }), svg('feMergeNode', { in: 'SourceGraphic' }));
  glow.append(merge);
  d.append(glow);
  for (const [k, c] of Object.entries(COLORS)) {
    const g = svg('radialGradient', { id: 'grad-' + k });
    g.append(svg('stop', { offset: '0%', 'stop-color': '#ffffff' }));
    g.append(svg('stop', { offset: '35%', 'stop-color': c }));
    g.append(svg('stop', { offset: '100%', 'stop-color': c, 'stop-opacity': '0.15' }));
    d.append(g);
    const m = svg('marker', {
      id: 'arrow-' + k, viewBox: '0 0 10 10', refX: '9', refY: '5',
      markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse',
    });
    m.append(svg('path', { d: 'M0,0 L10,5 L0,10 z', fill: c }));
    d.append(m);
  }
  return d;
}

function backdrop(g, w, h) {
  // Decorative specks only; they are not data and carry no relation.
  let seed = 7;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  for (let i = 0; i < Math.round((w * h) / 9000); i++) {
    g.append(svg('circle', {
      cx: (rand() * w).toFixed(1), cy: (rand() * h).toFixed(1), r: (rand() * 1.3 + 0.3).toFixed(2),
      fill: '#8fb3ff', opacity: (rand() * 0.25 + 0.05).toFixed(2),
    }));
  }
}

function render() {
  const data = state.data;
  const root = $('#graph');
  root.replaceChildren(svg('title', { id: 'graph-title' }));
  root.firstChild.textContent = 'Konu bağlantı haritası';
  renderNotes(data && data.unknowns);
  const hasGraph = data && data.nodes && data.nodes.length;
  $('#empty').hidden = !!hasGraph;
  if (!hasGraph) {
    if (data) {
      $('#empty').replaceChildren(
        el('p', { text: 'Bu arama için bir konu bulunamadı.' }),
        el('p', { class: 'muted', text: 'Konu adını farklı yazmayı dene ya da aşağıdaki örneklerden birini seç.' }),
      );
    }
    return;
  }

  const wrap = $('#graph-wrap');
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  root.setAttribute('viewBox', `0 0 ${w} ${h}`);
  root.append(defs());
  const bg = svg('g');
  backdrop(bg, w, h);
  root.append(bg);

  const { pos, R, cx, cy, groups } = layout(data, w, h);
  const scene = svg('g', { id: 'scene' });
  root.append(scene);

  // Group captions (only for non-empty groups).
  for (const [g, ids] of Object.entries(groups)) {
    if (!ids.length) continue;
    const s = SECTORS[g];
    const a = (s.angle * Math.PI) / 180;
    const rr = g === 'before' || g === 'after' ? R * 0.5 : R * 0.55;
    const t = svg('text', { class: 'group-label', x: cx + rr * Math.cos(a), y: cy + rr * Math.sin(a) + (g === 'destek' ? -6 : 14) });
    t.textContent = s.label;
    scene.append(t);
  }

  const edgeLayer = svg('g');
  const nodeLayer = svg('g');
  scene.append(edgeLayer, nodeLayer);

  data.edges.forEach((e, i) => {
    const p1 = pos[e.source];
    const p2 = pos[e.target];
    if (!p1 || !p2) return;
    const r1 = e.source === data.center ? 20 : 13;
    const r2 = (e.target === data.center ? 20 : 13) + (e.type === 'onkosul' ? 4 : 0);
    const d = curve(p1, p2, r1, r2, i % 2 ? 0.12 : -0.12);
    const color = e.status === 'unknown' ? 'ortak' : e.type;
    const path = svg('path', {
      id: 'edge-' + i, d,
      class: `edge ${e.type} status-${e.status}`,
      'marker-end': e.type === 'onkosul' ? `url(#arrow-${color})` : null,
      filter: e.status === 'verified' ? 'url(#glow)' : null,
    });
    path.dataset.edge = i;
    const hit = svg('path', { d, class: 'edge-hit' });
    hit.dataset.edge = i;
    edgeLayer.append(path, hit);
    if (e.status !== 'unknown') {
      const pulse = svg('circle', { r: '2.6', class: 'pulse', fill: COLORS[e.type] || '#fff', filter: 'url(#glow)' });
      const anim = svg('animateMotion', { dur: (2.4 + (i % 4) * 0.5).toFixed(1) + 's', repeatCount: 'indefinite', begin: (i * 0.37).toFixed(2) + 's' });
      const mpath = svg('mpath');
      mpath.setAttributeNS('http://www.w3.org/1999/xlink', 'href', '#edge-' + i);
      mpath.setAttribute('href', '#edge-' + i);
      anim.append(mpath);
      pulse.append(anim);
      edgeLayer.append(pulse);
    }
    for (const target of [path, hit]) {
      target.addEventListener('mouseenter', (ev) => showEdgeTip(ev, e, i));
      target.addEventListener('mousemove', moveTip);
      target.addEventListener('mouseleave', hideTip);
    }
  });

  for (const n of data.nodes) {
    const p = pos[n.id];
    if (!p) continue;
    const isCenter = n.id === data.center;
    const kind = isCenter ? 'center' : (p.group === 'before' || p.group === 'after' ? 'onkosul' : p.group);
    const r = isCenter ? 20 : 12;
    const g = svg('g', {
      class: 'node' + (isCenter ? ' center' : ''), transform: `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`,
      tabindex: '0', role: 'button', 'aria-label': nodeLabel(n) + ' — ayrıntıları aç',
    });
    g.dataset.id = n.id;
    g.append(svg('circle', { class: 'halo', r: r * 1.9, fill: `url(#grad-${kind})` }));
    g.append(svg('circle', { class: 'core', r, fill: `url(#grad-${kind})`, filter: 'url(#glow)' }));
    const lines = wrapTitle(n.title);
    const text = svg('text', { y: r + 18 });
    lines.forEach((line, li) => {
      const ts = svg('tspan', { x: 0, dy: li === 0 ? 0 : 16 });
      ts.textContent = line;
      text.append(ts);
      if (li === lines.length - 1) {
        const gt = svg('tspan', { class: 'grades' });
        gt.textContent = ' ' + gradesText(n.grades);
        text.append(gt);
      }
    });
    g.append(text);
    const tip = svg('title');
    tip.textContent = nodeLabel(n);
    g.append(tip);
    g.addEventListener('click', () => openPanel(n.id, g));
    g.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openPanel(n.id, g); }
    });
    g.addEventListener('mouseenter', () => highlight(n.id));
    g.addEventListener('mouseleave', () => highlight(null));
    nodeLayer.append(g);
  }
  markSelected();
}

function highlight(nodeId, edgeIndex) {
  const scene = $('#scene');
  if (!scene) return;
  scene.querySelectorAll('.hot').forEach((x) => x.classList.remove('hot'));
  if (nodeId == null && edgeIndex == null) { scene.classList.remove('dim'); return; }
  scene.classList.add('dim');
  state.data.edges.forEach((e, i) => {
    const on = edgeIndex != null ? i === edgeIndex : e.source === nodeId || e.target === nodeId;
    if (!on) return;
    scene.querySelector(`path.edge[data-edge="${i}"]`)?.classList.add('hot');
    for (const id of [e.source, e.target]) scene.querySelector(`.node[data-id="${id}"]`)?.classList.add('hot');
  });
  if (nodeId) scene.querySelector(`.node[data-id="${nodeId}"]`)?.classList.add('hot');
}

/* ------------------------------------ tooltip ------------------------------------------ */

function relationSentence(e) {
  const s = titleOf(e.source);
  const t = titleOf(e.target);
  if (e.type === 'onkosul') return `Önce "${s}", sonra "${t}" çalışılmalı.`;
  if (e.type === 'destek') return `"${s}" ile "${t}" birbirini destekler ya da biri diğerinde uygulanır.`;
  return `"${s}" ile "${t}" ortak kavramlar taşır; belirli bir sıra yok.`;
}

function showEdgeTip(ev, e, i) {
  highlight(null, i);
  const tip = $('#tooltip');
  const quote = e.evidence && e.evidence[0] ? e.evidence[0].quote : '';
  tip.replaceChildren(
    el('b', { text: TYPE_TR[e.type] || e.type }), ' · ', STATUS_SHORT[e.status] || e.status,
    el('div', { text: relationSentence(e) }),
    quote ? el('q', { text: quote.length > 220 ? quote.slice(0, 217) + '…' : quote }) : el('q', { text: 'Metinde alıntı yok.' }),
  );
  tip.hidden = false;
  moveTip(ev);
}

function moveTip(ev) {
  const tip = $('#tooltip');
  const x = Math.min(window.innerWidth - tip.offsetWidth - 10, ev.clientX + 14);
  const y = Math.min(window.innerHeight - tip.offsetHeight - 10, ev.clientY + 14);
  tip.style.left = Math.max(6, x) + 'px';
  tip.style.top = Math.max(6, y) + 'px';
}

function hideTip() {
  $('#tooltip').hidden = true;
  highlight(null);
}

/* ------------------------------------ panel -------------------------------------------- */

function section(title, ...children) {
  return [el('h3', { text: title }), ...children];
}

function relationBlock(e) {
  const quotes = (e.evidence || []).map((ev) =>
    el('blockquote', {}, [ev.quote, el('cite', { text: ev.passage })]));
  return el('div', { class: 'rel' }, [
    el('div', { class: 'rel-head' }, [
      el('span', { class: 'badge ' + e.type, text: TYPE_TR[e.type] || e.type }),
      el('span', { class: 'badge ' + e.status, text: STATUS_SHORT[e.status] || e.status, title: STATUS_TR[e.status] }),
    ]),
    el('div', { text: relationSentence(e) }),
    el('div', { class: 'muted', text: STATUS_TR[e.status] || '' }),
    ...(quotes.length ? quotes : [el('div', { class: 'muted', text: 'Bu bağlantı için metinde alıntı bulunamadı.' })]),
  ]);
}

function sourceItem(s) {
  const tur = s.tur || '';
  const isMeb = /^MEB|^EBA/.test(tur) || /^MEB|^EBA/.test(s.ad);
  const name = s.url && s.verified ? el('a', { href: s.url, target: '_blank', rel: 'noopener', text: s.ad }) : el('span', { text: s.ad });
  return el('li', { class: 'src' }, [
    name,
    el('div', { class: 'src-tags' }, [
      tur ? el('span', { class: 'badge ' + (isMeb ? 'meb' : 'ortak'), text: tur }) : null,
      s.verified ? el('span', { class: 'badge verified', text: 'Doğrulandı' }) : el('span', { class: 'badge unverified', text: 'Doğrulanmadı · bağlantı yok' }),
    ]),
  ]);
}

function openPanel(id, fromEl) {
  const n = byId(id);
  if (!n) return;
  state.selected = id;
  state.lastFocus = fromEl || document.activeElement;
  markSelected();
  $('#panel-title').textContent = nodeLabel(n);
  const data = state.data;
  const isCenter = id === data.center;
  const rels = data.edges.filter((e) => e.source === id || e.target === id);
  const ordered = [...data.nodes].sort((a, b) => a.study.order - b.study.order);
  const mebFirst = [...(n.study.sources || [])].sort((a, b) => rankSource(a) - rankSource(b));

  const body = [
    el('span', { class: 'grade-chip', text: 'Sınıf: ' + (n.grades || []).join('-') }),
    n.grade_note ? el('div', { class: 'warn', text: 'Sınıf bilgisi tartışmalı: ' + n.grade_note }) : null,
    ...section('Özet', el('p', { text: n.summary || 'Özet yok.' })),
    ...section('Nerelerde kullanılır',
      n.uses && n.uses.length ? el('ul', {}, n.uses.map((u) => el('li', { text: u }))) : el('p', { class: 'muted', text: 'Bilgi yok.' })),
    ...section(isCenter ? 'Bağlantıları' : `"${titleOf(data.center)}" ile ilişkisi`,
      ...(rels.length ? rels.map(relationBlock) : [el('p', { class: 'muted', text: 'Bağlantı bulunamadı.' })])),
    ...section('Hangi kaynaklardan çalışılmalı',
      el('ul', {}, mebFirst.map(sourceItem)),
      el('p', { class: 'muted', text: 'MEB kaynakları önce gelir. Hiçbir kaynak canlı olarak doğrulanmadı; bağlantı eklenmedi.' })),
    ...section('Ne zaman / hangi sırayla çalışılmalı',
      el('div', { class: 'study-head' }, [
        el('span', { class: 'order-step', text: `${n.study.order}.` }),
        el('span', { text: n.study.why }),
      ]),
      el('ol', { class: 'order-list' }, ordered.map((x) =>
        el('li', { class: x.id === id ? 'current' : null }, [nodeLabel(x)])))),
    !isCenter ? el('button', { class: 'recenter', type: 'button', text: 'Bu konuyu merkeze al', onclick: () => runSearch(n.id) }) : null,
  ];
  $('#panel-body').replaceChildren(...body.filter(Boolean));
  $('#panel-body').scrollTop = 0;
  const panel = $('#panel');
  panel.inert = false;
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  panel.focus({ preventScroll: true });
}

function rankSource(s) {
  const t = (s.tur || '') + ' ' + s.ad;
  if (/^MEB/.test(s.ad) && /ders kitab/.test(t)) return 0;
  if (/^MEB/.test(s.ad)) return 1;
  if (/^EBA/.test(s.ad)) return 2;
  return 3;
}

function closePanel() {
  const panel = $('#panel');
  if (!panel.classList.contains('open')) return;
  panel.classList.remove('open');
  panel.setAttribute('aria-hidden', 'true');
  panel.inert = true;
  state.selected = null;
  markSelected();
  if (state.lastFocus && document.contains(state.lastFocus)) state.lastFocus.focus({ preventScroll: true });
}

function markSelected() {
  document.querySelectorAll('.node.selected').forEach((x) => x.classList.remove('selected'));
  if (state.selected) document.querySelector(`.node[data-id="${state.selected}"]`)?.classList.add('selected');
}

/* ------------------------------------ wiring ------------------------------------------- */

function init() {
  $('#chips').replaceChildren(...CHIPS.map((c) => el('button', { class: 'chip', type: 'button', text: c, onclick: () => runSearch(c) })));
  $('#search-form').addEventListener('submit', (ev) => { ev.preventDefault(); runSearch($('#q').value); });
  $('#panel-close').addEventListener('click', closePanel);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closePanel(); });
  let t = null;
  window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => state.data && render(), 150); });
  const q = new URLSearchParams(location.search).get('q');
  if (q) runSearch(q);
}

init();
