// The neuron network page: a home network of every topic, a topic network around the searched topic,
// a side panel, and a small own renderer (perspective, depth fog, eased camera). Layout and projection
// live in geometry.js; this file only draws, handles input and talks to the server.
(function () {
  "use strict";

  const G = window.NetGeometry;
  const TAU = Math.PI * 2;
  const PANEL_WIDTH = 420;
  const PULSE_SECONDS = 3.6;
  const SEGMENTS = 18;
  const FONT = '"Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif';
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ANIM = reduceMotion ? 0.3 : 1;

  const TYPE_LABEL = { onkosul: "ön koşul", destek: "destek", ortak: "ortak kavram" };
  const STATUS_LABEL = { verified: "doğrulandı", inference: "çıkarım", unknown: "bilinmiyor" };
  const STATUS_ALPHA = { verified: 0.92, inference: 0.5, unknown: 0.3 };
  const STATUS_DASH = { verified: [], inference: [5, 4], unknown: [1.5, 4] };
  const EDGE_COLOUR = { onkosul: "#2d4a62", destek: "#3d8c7c", ortak: "#8a96a2" };
  const ROLE_RIM = { centre: "#1f6f82", prereq: "#2d4a62", dependent: "#3d8c7c", other: "#7f93a4" };
  const ORDER_LABEL = { 1: "önce", 2: "şimdi (merkez)", 3: "sonra", 4: "ilgi için" };
  const INK = "#17232d";
  const MUTED = "#5a6875";

  const $ = (id) => document.getElementById(id);
  const canvas = $("net");
  const ctx = canvas.getContext("2d");
  const stage = $("stage");
  const panel = $("panel");
  const panelBody = $("panel-body");

  const state = {
    mode: "home",
    scene: null,
    homeScene: null,
    homeUnknowns: [],
    payloads: new Map(),
    unknowns: [],
    fade: 1,
    cam: { tx: 0, ty: 0, tz: 0, yaw: 0.5, pitch: -0.22, dist: 400, shiftX: 0.5 },
    panelOpen: false,
    selected: null,
    hover: null,
    tipPos: null,
    tweens: [],
    chain: Promise.resolve(),
    width: 1,
    height: 1,
    dpr: 1,
    time: 0,
    lastInput: 0,
    drag: null,
  };

  // ---- small helpers -------------------------------------------------------------------------

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function truncate(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  function setStatus(text) {
    $("hud").textContent = text;
  }

  function badge(status) {
    return el("span", "badge b-" + status, STATUS_LABEL[status] || STATUS_LABEL.unknown);
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Sunucu yanıt vermedi.");
    return data;
  }

  function focal() {
    return Math.min(state.width, state.height) * 1.1;
  }

  function fogOf(vz) {
    const d = state.cam.dist;
    const k = clamp((vz - d * 0.7) / (d * 0.8), 0, 1);
    return 1 - 0.45 * k;
  }

  // ---- scenes --------------------------------------------------------------------------------

  // A scene is the network as the renderer needs it: positions, somata, dendrites, axon curves.
  // centre is the searched topic (pinned at the origin) or null on the home network.
  function buildScene(nodeList, edgeList, centre) {
    const pos = G.layout(nodeList.map((n) => n.id), edgeList, centre);
    const nodes = new Map();
    let maxR = 1;
    for (const n of nodeList) {
      const p = pos.get(n.id);
      maxR = Math.max(maxR, Math.hypot(p.x, p.y, p.z));
      const radius = centre ? (n.id === centre ? 13 : 8) : 7;
      nodes.set(n.id, {
        id: n.id,
        title: n.title,
        grades: n.grades || [],
        rec: null,
        role: n.id === centre ? "centre" : "other",
        radius,
        x: p.x,
        y: p.y,
        z: p.z,
        dend: G.dendrites(n.id, radius),
      });
    }
    const edges = edgeList.map((e) => {
      const a = nodes.get(e.source);
      const b = nodes.get(e.target);
      const pair = [e.source, e.target].sort().join("|");
      return {
        source: e.source,
        target: e.target,
        type: e.type,
        status: e.status,
        evidence: e.evidence || [],
        a,
        b,
        cp: G.controlPoint(a, b, pair),
        phase: G.hash(pair) / 4294967296,
        pts: [],
      };
    });
    if (centre) {
      for (const e of edges) {
        if (e.type !== "onkosul") continue;
        if (e.target === centre) e.a.role = "prereq";
        if (e.source === centre) e.b.role = "dependent";
      }
    }
    return { nodes, edges, centre, maxR, homeDist: maxR * 2.2, topicDist: maxR * 4.2 };
  }

  function topicScene(payload) {
    const scene = buildScene(
      payload.nodes.map((n) => ({ id: n.id, title: n.title, grades: n.grades })),
      payload.edges,
      payload.center,
    );
    for (const n of payload.nodes) {
      const node = scene.nodes.get(n.id);
      if (node) node.rec = n;
    }
    return scene;
  }

  function resetCamera(scene) {
    const c = state.cam;
    c.tx = 0;
    c.ty = 0;
    c.tz = 0;
    c.dist = scene.centre ? scene.topicDist : scene.homeDist;
  }

  // ---- tweens (eased, one loop) --------------------------------------------------------------

  function ease(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function tween(ms, onFrame, kind) {
    return new Promise((resolve) => {
      state.tweens.push({ kind, start: performance.now(), ms: Math.max(1, ms), onFrame, resolve });
    });
  }

  function stepTweens(now) {
    const keep = [];
    for (const t of state.tweens) {
      const p = Math.min(1, (now - t.start) / t.ms);
      t.onFrame(ease(p));
      if (p >= 1) t.resolve();
      else keep.push(t);
    }
    state.tweens = keep;
  }

  function cancelCameraTweens() {
    state.tweens = state.tweens.filter((t) => {
      if (t.kind !== "cam") return true;
      t.resolve();
      return false;
    });
  }

  function cameraBusy() {
    return state.tweens.some((t) => t.kind === "cam");
  }

  function flyTo(id) {
    const scene = state.scene;
    const n = scene && scene.nodes.get(id);
    if (!n) return Promise.resolve();
    const c = state.cam;
    const from = { tx: c.tx, ty: c.ty, tz: c.tz, dist: c.dist };
    const dist = scene.centre ? scene.topicDist * 0.62 : scene.homeDist * 0.6;
    return tween(900 * ANIM, (k) => {
      c.tx = from.tx + (n.x - from.tx) * k;
      c.ty = from.ty + (n.y - from.ty) * k;
      c.tz = from.tz + (n.z - from.tz) * k;
      c.dist = from.dist + (dist - from.dist) * k;
    }, "cam");
  }

  function zoomBy(factor) {
    const scene = state.scene;
    if (!scene) return;
    const c = state.cam;
    const from = c.dist;
    const max = scene.centre ? scene.topicDist * 1.6 : scene.homeDist * 1.4;
    const to = clamp(from * factor, Math.max(60, scene.maxR * 0.9), max);
    state.lastInput = state.time;
    tween(260 * ANIM, (k) => {
      c.dist = from + (to - from) * k;
    }, "cam");
  }

  // ---- drawing -------------------------------------------------------------------------------

  function projectScene() {
    const S = state.scene;
    const c = state.cam;
    const W = state.width;
    const H = state.height;
    const F = focal();
    for (const n of S.nodes.values()) {
      const p = G.project(n, c, W, H, F);
      n.sx = p.x;
      n.sy = p.y;
      n.sc = p.s;
      n.vz = p.vz;
      n.dpts = n.dend.map((d) => ({
        ctrl: G.project({
          x: n.x + d.dir.x * d.len * 0.5 + d.curl.x * d.len * d.bend,
          y: n.y + d.dir.y * d.len * 0.5 + d.curl.y * d.len * d.bend,
          z: n.z + d.dir.z * d.len * 0.5 + d.curl.z * d.len * d.bend,
        }, c, W, H, F),
        end: G.project({ x: n.x + d.dir.x * d.len, y: n.y + d.dir.y * d.len, z: n.z + d.dir.z * d.len }, c, W, H, F),
      }));
    }
    for (const e of S.edges) {
      e.pts = [];
      for (let k = 0; k <= SEGMENTS; k++) {
        e.pts.push(G.project(G.bezier(e.a, e.cp, e.b, k / SEGMENTS), c, W, H, F));
      }
    }
  }

  function sampleAt(pts, u) {
    const f = u * (pts.length - 1);
    const i = Math.min(pts.length - 2, Math.floor(f));
    const k = f - i;
    const p = pts[i];
    const q = pts[i + 1];
    return { x: p.x + (q.x - p.x) * k, y: p.y + (q.y - p.y) * k, s: p.s + (q.s - p.s) * k, vz: p.vz + (q.vz - p.vz) * k };
  }

  function drawEdges() {
    for (const e of state.scene.edges) {
      const pts = e.pts;
      const first = pts[0];
      const last = pts[SEGMENTS];
      const fog = fogOf((first.vz + last.vz) / 2);
      const sc = (first.s + last.s) / 2;
      ctx.beginPath();
      ctx.moveTo(first.x, first.y);
      for (let k = 1; k <= SEGMENTS; k++) ctx.lineTo(pts[k].x, pts[k].y);
      ctx.strokeStyle = EDGE_COLOUR[e.type];
      ctx.lineWidth = clamp(1.2 * sc, 0.8, 2.6);
      ctx.setLineDash(STATUS_DASH[e.status] || []);
      ctx.globalAlpha = state.fade * (STATUS_ALPHA[e.status] || 0.3) * fog;
      ctx.stroke();
      if (e.type !== "ortak") {
        ctx.setLineDash([]);
        ctx.fillStyle = EDGE_COLOUR[e.type];
        ctx.beginPath();
        ctx.arc(last.x, last.y, clamp(2.2 * sc, 1.6, 3.6), 0, TAU);
        ctx.fill();
      }
    }
    ctx.setLineDash([]);
  }

  // The travelling pulse runs source to target on prerequisite edges only: the order of learning.
  function drawPulses() {
    for (const e of state.scene.edges) {
      if (e.type !== "onkosul") continue;
      const sc = (e.pts[0].s + e.pts[SEGMENTS].s) / 2;
      const fog = fogOf(e.pts[SEGMENTS].vz);
      for (let j = 0; j < 3; j++) {
        let u = (state.time / PULSE_SECONDS + e.phase - j * 0.035) % 1;
        if (u < 0) u += 1;
        const p = sampleAt(e.pts, u);
        ctx.globalAlpha = state.fade * fog * (0.85 - j * 0.27);
        ctx.fillStyle = EDGE_COLOUR.onkosul;
        ctx.beginPath();
        ctx.arc(p.x, p.y, clamp(2.6 * sc, 1.8, 4.2) * (1 - j * 0.2), 0, TAU);
        ctx.fill();
      }
    }
  }

  function somaRadius(n) {
    return Math.max(2.2, n.radius * n.sc);
  }

  function drawNeuron(n) {
    const fog = fogOf(n.vz);
    const rr = somaRadius(n);
    const rim = ROLE_RIM[n.role];
    const base = state.fade * fog;

    ctx.lineCap = "round";
    ctx.strokeStyle = rim;
    ctx.lineWidth = clamp(1.4 * n.sc, 0.8, 2.4);
    ctx.globalAlpha = base * 0.5;
    for (let i = 0; i < n.dend.length; i++) {
      const d = n.dpts[i];
      ctx.beginPath();
      ctx.moveTo(n.sx, n.sy);
      ctx.quadraticCurveTo(d.ctrl.x, d.ctrl.y, d.end.x, d.end.y);
      ctx.stroke();
    }

    const g = ctx.createRadialGradient(n.sx - rr * 0.35, n.sy - rr * 0.4, rr * 0.1, n.sx, n.sy, rr);
    if (n.role === "centre") {
      g.addColorStop(0, "#e9f5f7");
      g.addColorStop(0.55, "#6fa9b6");
      g.addColorStop(1, "#1f6f82");
    } else {
      g.addColorStop(0, "#ffffff");
      g.addColorStop(0.6, "#e7edf2");
      g.addColorStop(1, "#b9c7d3");
    }
    ctx.globalAlpha = base;
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(n.sx, n.sy, rr, 0, TAU);
    ctx.fill();
    ctx.lineWidth = 1.1;
    ctx.strokeStyle = rim;
    ctx.globalAlpha = base * 0.85;
    ctx.stroke();

    ctx.globalAlpha = base * 0.2;
    ctx.fillStyle = rim;
    ctx.beginPath();
    ctx.arc(n.sx + rr * 0.18, n.sy + rr * 0.12, rr * 0.3, 0, TAU);
    ctx.fill();

    if (isHighlighted(n)) {
      ctx.globalAlpha = state.fade * 0.8;
      ctx.strokeStyle = ROLE_RIM.centre;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(n.sx, n.sy, rr + 5, 0, TAU);
      ctx.stroke();
    }
  }

  function isHighlighted(n) {
    return (state.hover && state.hover.kind === "node" && state.hover.id === n.id) || state.selected === n.id;
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawLabel(n) {
    const fog = fogOf(n.vz);
    const rr = somaRadius(n);
    const centre = n.role === "centre";
    const gradeText = n.grades.length ? n.grades.join("-") : "";
    const lines = centre
      ? [{ text: n.title, font: "600 15px " + FONT, colour: INK }, { text: "Sınıf: " + (gradeText || "bilinmiyor"), font: "500 12px " + FONT, colour: MUTED }]
      : [{ text: gradeText ? n.title + " (" + gradeText + ")" : n.title, font: "500 12px " + FONT, colour: INK }];
    ctx.font = lines[0].font;
    let width = 0;
    for (const line of lines) {
      ctx.font = line.font;
      width = Math.max(width, ctx.measureText(line.text).width);
    }
    const padX = 7;
    const lineH = centre ? 19 : 16;
    const h = lines.length * lineH + 6;
    const w = width + padX * 2;
    const x = n.sx - w / 2;
    const y = n.sy - rr - 8 - h;
    ctx.globalAlpha = state.fade * fog * (isHighlighted(n) ? 1 : 0.92);
    ctx.fillStyle = "rgba(255,255,255,0.94)";
    roundRect(x, y, w, h, 6);
    ctx.fill();
    ctx.strokeStyle = isHighlighted(n) ? ROLE_RIM.centre : "#d6dee6";
    ctx.lineWidth = 1;
    ctx.stroke();
    lines.forEach((line, i) => {
      ctx.font = line.font;
      ctx.fillStyle = line.colour;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(line.text, n.sx, y + 3 + lineH * (i + 0.5));
    });
    ctx.textAlign = "start";
  }

  function draw() {
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    ctx.clearRect(0, 0, state.width, state.height);
    if (!state.scene) return;
    projectScene();
    drawEdges();
    drawPulses();
    const order = [...state.scene.nodes.values()].filter((n) => n.vz > 20).sort((p, q) => q.vz - p.vz);
    for (const n of order) drawNeuron(n);
    for (const n of order) drawLabel(n);
    ctx.globalAlpha = 1;
  }

  // ---- hit testing and hover -----------------------------------------------------------------

  function pickNode(px, py) {
    let best = null;
    for (const n of state.scene.nodes.values()) {
      if (n.vz <= 20) continue;
      const rr = somaRadius(n);
      const reach = Math.max(rr + 6, 12);
      if (Math.hypot(px - n.sx, py - n.sy) <= reach && (!best || n.vz < best.vz)) best = n;
    }
    return best;
  }

  function segmentDistance(px, py, a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const l2 = dx * dx + dy * dy;
    const t = l2 ? clamp(((px - a.x) * dx + (py - a.y) * dy) / l2, 0, 1) : 0;
    return Math.hypot(px - (a.x + dx * t), py - (a.y + dy * t));
  }

  function pickEdge(px, py) {
    let best = null;
    let bestD = 7;
    for (const e of state.scene.edges) {
      for (let k = 0; k < SEGMENTS; k++) {
        const d = segmentDistance(px, py, e.pts[k], e.pts[k + 1]);
        if (d < bestD) {
          bestD = d;
          best = e;
        }
      }
    }
    return best;
  }

  function edgeTipText(e) {
    const head = TYPE_LABEL[e.type] + " · " + STATUS_LABEL[e.status];
    const route = e.type === "onkosul"
      ? "Önce: " + e.a.title + "  →  sonra: " + e.b.title
      : e.a.title + "  —  " + e.b.title;
    const quote = e.evidence[0] && e.evidence[0].quote
      ? "“" + truncate(e.evidence[0].quote, 170) + "”"
      : "Ayrıntı için konuya tıklayın.";
    return [head, route, quote];
  }

  function showTip(lines, px, py) {
    const tip = $("tip");
    tip.replaceChildren(...lines.map((text, i) => el("div", i === 0 ? "tip-head" : "tip-line", text)));
    tip.hidden = false;
    const x = clamp(px + 14, 6, state.width - 260);
    const y = clamp(py + 14, 6, state.height - 90);
    tip.style.transform = "translate(" + x + "px," + y + "px)";
  }

  function hideTip() {
    $("tip").hidden = true;
  }

  // ---- panel ---------------------------------------------------------------------------------

  function section(title, children) {
    const s = el("section", "p-sec");
    s.append(el("h3", "p-h", title));
    for (const c of children) s.append(c);
    return s;
  }

  function emptyLine(text) {
    return el("p", "p-empty", text);
  }

  function relationButton(data, edge, otherId) {
    const other = data.nodes.get(otherId);
    const btn = el("button", "rel");
    btn.type = "button";
    const top = el("span", "rel-top");
    top.append(el("span", "rel-title", other ? other.title : otherId), badge(edge.status));
    btn.append(top);
    if (edge.evidence[0] && edge.evidence[0].quote) {
      btn.append(el("span", "rel-quote", "“" + truncate(edge.evidence[0].quote, 220) + "”"));
    }
    btn.addEventListener("click", () => openNode(otherId));
    return btn;
  }

  // The panel's data for a topic: the same shape whether it comes from the open topic network or from
  // a topic's own search (the landing network's panel).
  function topicPanelData() {
    return { nodes: state.scene.nodes, edges: state.scene.edges };
  }

  function panelDataOf(payload) {
    const nodes = new Map();
    for (const n of payload.nodes) nodes.set(n.id, { title: n.title, grades: n.grades || [], rec: n });
    return { nodes, edges: payload.edges };
  }

  // Hidden only for the topic the open network is already centred on.
  function centreAction(id) {
    if (state.scene.centre === id) return null;
    const btn = el("button", "centre-btn", "Bu konuyu merkez yap");
    btn.type = "button";
    btn.addEventListener("click", () => centreOn(id));
    const wrap = el("div", "panel-act");
    wrap.append(btn);
    return wrap;
  }

  function renderPanel(id, data) {
    const head = state.scene.nodes.get(id);
    $("panel-title").textContent = head.title;
    $("panel-grades").textContent = head.grades.length ? "Sınıf: " + head.grades.join("-") : "Sınıf bilgisi yok";

    const body = [];
    const action = centreAction(id);
    if (action) body.push(action);
    if (!data) {
      body.push(emptyLine("Konu ayrıntısı yükleniyor…"));
      panelBody.replaceChildren(...body);
      return;
    }

    const n = data.nodes.get(id);
    const rec = n.rec || {};
    const prereq = data.edges.filter((e) => e.type === "onkosul" && e.target === id);
    const dependents = data.edges.filter((e) => e.type === "onkosul" && e.source === id);
    const others = data.edges.filter((e) => e.type !== "onkosul" && (e.source === id || e.target === id));

    body.push(section("Kısa özet", [el("p", "summary", rec.summary || "Bu konu için özet yok.")]));
    body.push(section("Bu konudan önce öğrenilmesi gerekenler", prereq.length
      ? prereq.map((e) => relationButton(data, e, e.source))
      : [emptyLine("Bu ağda ön koşul ilişkisi yok.")]));
    body.push(section("Bu konunun gerekli olduğu konular", dependents.length
      ? dependents.map((e) => relationButton(data, e, e.target))
      : [emptyLine("Bu ağda bu konuya bağlı bir konu yok.")]));
    if (others.length) {
      body.push(section("Destek ve ortak kavramlar", others.map((e) => relationButton(data, e, e.source === id ? e.target : e.source))));
    }

    const sources = (rec.study && rec.study.sources) || [];
    const sourceItems = sources.map((src) => {
      const li = el("li", "src");
      const verified = src.verified === true && Boolean(src.url);
      li.append(el("span", "src-name", src.ad), el("span", "src-type", src.tur));
      li.append(verified
        ? el("span", "badge b-verified", "doğrulandı")
        : el("span", "badge b-unverified", "doğrulanmadı"));
      return li;
    });
    const sourceList = el("ul", "src-list");
    sourceList.append(...sourceItems);
    body.push(section("Hangi kaynaklardan çalışılmalı", sourceItems.length
      ? [sourceList]
      : [emptyLine("Bu konu için kaynak kaydı yok.")]));

    const uses = rec.uses || [];
    body.push(section("Nerelerde kullanılır", uses.length
      ? uses.map((u) => el("p", "use", u))
      : [emptyLine("Corpus'ta bu konunun kullanıldığı bir cümle bulunamadı.")]));

    if (rec.study) {
      body.push(section("Ne zaman ve hangi sırayla çalışılmalı", [
        el("p", "order", "Sıra: " + (ORDER_LABEL[rec.study.order] || "ilgi için")),
        el("p", "use", rec.study.why || ""),
      ]));
    }

    panelBody.replaceChildren(...body);
  }

  function openPanel(id, data) {
    state.selected = id;
    state.panelOpen = true;
    renderPanel(id, data);
    panel.classList.add("open");
    panelBody.scrollTop = 0;
    state.lastInput = state.time;
  }

  function closePanel() {
    if (!state.panelOpen) return;
    state.panelOpen = false;
    state.selected = null;
    panel.classList.remove("open");
  }

  function panelShift() {
    if (!state.panelOpen || state.width < 760) return 0.5;
    return (state.width - PANEL_WIDTH) / (2 * state.width);
  }

  // A click on a node zooms to it and opens its panel. On the landing network the panel is filled
  // from the topic's own search (cached per topic); the network itself does not change.
  function openNode(id) {
    if (!state.scene || !state.scene.nodes.has(id)) return;
    flyTo(id);
    if (state.mode === "home") {
      openHomePanel(id);
    } else {
      openPanel(id, topicPanelData());
    }
  }

  async function loadPayload(id) {
    if (!state.payloads.has(id)) {
      const payload = await getJSON("/api/graph?q=" + encodeURIComponent(id));
      if (payload.center !== id) throw new Error("Bu konunun ayrıntısı alınamadı.");
      state.payloads.set(id, payload);
    }
    return state.payloads.get(id);
  }

  async function openHomePanel(id) {
    openPanel(id, null);
    try {
      const data = panelDataOf(await loadPayload(id));
      if (state.selected === id) renderPanel(id, data);
    } catch (err) {
      if (state.selected === id) panelBody.replaceChildren(...[centreAction(id), emptyLine(err.message)].filter(Boolean));
    }
  }

  // "Bu konuyu merkez yap": the search view, centred on this node.
  function centreOn(id) {
    const node = state.scene && state.scene.nodes.get(id);
    if (!node) return;
    $("q").value = node.title;
    showTopic(id, "Konu aranıyor…");
  }

  // ---- scene changes (fade out, swap, fade in) -----------------------------------------------

  function enqueue(task) {
    state.chain = state.chain.then(task).catch((err) => setStatus(err.message));
    return state.chain;
  }

  async function swapTo(scene, mode) {
    await tween(220 * ANIM, (k) => {
      state.fade = 1 - k;
    }, "fade");
    state.scene = scene;
    state.mode = mode;
    state.hover = null;
    state.selected = null;
    hideTip();
    resetCamera(scene);
    await tween(420 * ANIM, (k) => {
      state.fade = k;
    }, "fade");
  }

  function homeHud() {
    const S = state.homeScene;
    const verified = S.edges.length;
    return S.nodes.size + " konu · " + verified + " doğrulanmış ilişki. Bir nörona tıklayın ya da yukarıdan arayın.";
  }

  function topicHud(payload) {
    const verified = payload.edges.filter((e) => e.status === "verified").length;
    const title = payload.nodes.find((n) => n.id === payload.center);
    return "Merkez: " + (title ? title.title : payload.center) + " · " + payload.edges.length + " ilişki (" +
      verified + " doğrulanmış). Bir nörona tıklayarak ayrıntıyı açın. Ana ekran: H";
  }

  function showUnknowns(list) {
    const ul = $("unknowns");
    ul.replaceChildren(...(list || []).map((text) => el("li", null, text)));
  }

  function fillDatalist(nodes) {
    $("topic-list").replaceChildren(...nodes.map((n) => {
      const opt = el("option");
      opt.value = n.title;
      return opt;
    }));
  }

  // Loads the landing network once (the datalist needs it too). It does not touch the screen, so a
  // search that is already showing keeps its centre and its status line.
  async function loadHomeData() {
    if (state.homeScene) return;
    try {
      const overview = await getJSON("/api/overview");
      state.homeScene = buildScene(overview.nodes, overview.edges, null);
      state.homeUnknowns = overview.unknowns;
      fillDatalist(overview.nodes);
    } catch (err) {
      if (state.mode === "home") setStatus(err.message);
    }
  }

  async function goHome() {
    closePanel();
    $("q").value = "";
    await enqueue(async () => {
      if (!state.homeScene) {
        setStatus("Ağ hazırlanıyor. İlk açılışta birkaç saniye sürebilir.");
        await loadHomeData();
        if (!state.homeScene) return;
      }
      showUnknowns(state.homeUnknowns);
      if (state.mode === "home" && state.scene === state.homeScene) {
        setStatus(homeHud());
        return;
      }
      await swapTo(state.homeScene, "home");
      setStatus(homeHud());
    });
  }

  async function showTopic(id, label) {
    setStatus(label || "Konu aranıyor…");
    await enqueue(async () => {
      const payload = await getJSON("/api/graph?q=" + encodeURIComponent(id));
      if (!payload.center) {
        state.unknowns = payload.unknowns || [];
        showUnknowns(state.unknowns);
        setStatus(payload.unknowns && payload.unknowns[0] ? payload.unknowns[0] : "Bu konu bulunamadı.");
        return;
      }
      closePanel();
      state.unknowns = payload.unknowns || [];
      showUnknowns(state.unknowns);
      await swapTo(topicScene(payload), "topic");
      setStatus(topicHud(payload));
    });
  }

  function runSearch(text) {
    const q = text.trim();
    if (q) showTopic(q, "Konu aranıyor…");
  }

  // ---- input ---------------------------------------------------------------------------------

  function canvasPoint(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  canvas.addEventListener("pointerdown", (e) => {
    const p = canvasPoint(e);
    state.drag = { id: e.pointerId, x: p.x, y: p.y, moved: 0 };
    canvas.setPointerCapture(e.pointerId);
    cancelCameraTweens();
    state.lastInput = state.time;
  });

  canvas.addEventListener("pointermove", (e) => {
    const p = canvasPoint(e);
    if (state.drag && state.drag.id === e.pointerId) {
      const dx = p.x - state.drag.x;
      const dy = p.y - state.drag.y;
      state.drag.x = p.x;
      state.drag.y = p.y;
      state.drag.moved += Math.abs(dx) + Math.abs(dy);
      if (state.drag.moved > 4) {
        state.cam.yaw += dx * 0.006;
        state.cam.pitch = clamp(state.cam.pitch + dy * 0.005, -1.35, 1.35);
        state.hover = null;
        hideTip();
        state.lastInput = state.time;
      }
      return;
    }
    if (!state.scene) return;
    const node = pickNode(p.x, p.y);
    if (node) {
      state.hover = { kind: "node", id: node.id };
      canvas.style.cursor = "pointer";
      showTip([node.title, "Tıklayın: panel açılır."], p.x, p.y);
      return;
    }
    const edge = pickEdge(p.x, p.y);
    if (edge) {
      state.hover = { kind: "edge", id: edge.source + ">" + edge.target };
      canvas.style.cursor = "default";
      showTip(edgeTipText(edge), p.x, p.y);
      return;
    }
    state.hover = null;
    canvas.style.cursor = "default";
    hideTip();
  });

  canvas.addEventListener("pointerup", (e) => {
    const drag = state.drag;
    state.drag = null;
    if (!drag || drag.id !== e.pointerId || drag.moved > 6 || !state.scene) return;
    const p = canvasPoint(e);
    const node = pickNode(p.x, p.y);
    if (!node) return;
    openNode(node.id);
  });

  canvas.addEventListener("pointerleave", () => {
    state.hover = null;
    hideTip();
  });

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    cancelCameraTweens();
    const scene = state.scene;
    if (!scene) return;
    const max = scene.centre ? scene.topicDist * 1.6 : scene.homeDist * 1.4;
    state.cam.dist = clamp(state.cam.dist * Math.exp(e.deltaY * 0.0012), Math.max(60, scene.maxR * 0.9), max);
    state.lastInput = state.time;
  }, { passive: false });

  $("zoom-in").addEventListener("click", () => zoomBy(0.8));
  $("zoom-out").addEventListener("click", () => zoomBy(1.25));
  $("panel-close").addEventListener("click", closePanel);
  $("home").addEventListener("click", () => goHome());
  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runSearch($("q").value);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (state.panelOpen) {
        closePanel();
        e.preventDefault();
      }
      return;
    }
    if ((e.key === "h" || e.key === "H") && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping()) {
      e.preventDefault();
      goHome();
    }
  });

  function isTyping() {
    const t = document.activeElement;
    return Boolean(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable));
  }

  // ---- frame loop ----------------------------------------------------------------------------

  function resize() {
    const r = stage.getBoundingClientRect();
    state.width = Math.max(1, r.width);
    state.height = Math.max(1, r.height);
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(state.width * state.dpr);
    canvas.height = Math.round(state.height * state.dpr);
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    state.time = now / 1000;
    stepTweens(now);
    const c = state.cam;
    c.shiftX += (panelShift() - c.shiftX) * (1 - Math.exp(-dt * 7));
    if (!state.drag && !cameraBusy() && state.time - state.lastInput > 3.5 && !reduceMotion) {
      c.yaw += dt * 0.07;
    }
    draw();
    requestAnimationFrame(frame);
  }

  // ?q=... opens the search view on that text. The landing network is loaded behind it, without a
  // swap, so the centre the URL asked for is the one the page ends with.
  const startQuery = new URLSearchParams(window.location.search).get("q") || "";

  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(frame);
  if (startQuery.trim()) {
    $("q").value = startQuery.trim();
    runSearch(startQuery);
    enqueue(loadHomeData);
  } else {
    goHome();
  }
})();
