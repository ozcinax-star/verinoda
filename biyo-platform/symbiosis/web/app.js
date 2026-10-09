/* Symbiosis page: the neuron network (landing and search), the side panel and the keys. The 3D engine
   is Verinoda's graph3d.js, copied unchanged; NeuronGraph draws the neurons on top of it (soft bodies,
   dendrite filaments, curved axons, a pulse on the prerequisite edges). */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const TAU = Math.PI * 2;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const TYPE = {
    onkosul: { color: "#2d6c97", label: "Önkoşul", width: 1.7 },
    destek: { color: "#3a8567", label: "Destek", width: 1.4 },
    ortak: { color: "#8b7f6c", label: "Ortak kavram", width: 1.2 },
  };
  const STATUS = { verified: "Kanıtlı", inference: "Çıkarım", unknown: "Bilinmiyor" };
  const DASH = { verified: [], inference: [6, 5], unknown: [1.5, 4.5] };
  const ORDER_TEXT = { 1: "Önce çalışılır", 2: "Bu konuyla birlikte çalışılır", 3: "Sonra çalışılır" };
  const PAGE = "#f5f6f8", INK = "#1c2633", FOCUS_INK = "#8f4620", ACCENT = "#2d6c97", FOCUS = "#b25d33";
  const PULSE_MS = 3400;

  const hash = (s) => {
    let h = 2166136261;
    for (const c of String(s)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); }
    return h >>> 0;
  };

  // The neuron drawing. The engine (VerinodaGraph3D) keeps the layout, the camera and the input; this
  // class replaces only what is painted, and pins the searched neuron in the middle of the space.
  class NeuronGraph extends window.VerinodaGraph3D {
    constructor(canvas, opts) {
      super(canvas, opts);
      this.focusId = null; // the searched topic: pinned in the middle, drawn larger, labelled with its grade
      this.pending = null; // { type: "focus", id } or { type: "fit" }: runs once the layout has mostly settled
      this.pulsing = false;
      this.dend = new Map();
    }

    setData(nodes, links, opts) {
      super.setData(nodes, links, opts);
      this.pulsing = this.links.some((l) => l.type === "onkosul");
    }

    tick() {
      super.tick();
      const f = this.focusId && this.byId.get(this.focusId);
      if (f) { f.x = 0; f.y = 0; f.z = 0; f.vx = 0; f.vy = 0; f.vz = 0; }
    }

    moving() { return super.moving() || (this.pulsing && this.active) || !!this.pending; }

    isFocus(n) { return n.id === this.focusId; }

    bodyR(n) { return this.rad(n) * (this.isFocus(n) ? 1.7 : 1); }

    runPending() {
      const p = this.pending;
      if (!p || this.alpha > 0.1) return;
      this.pending = null;
      if (p.type === "focus") this.flyToNode(p.id, 1000);
      else this.fit(1000);
    }

    // Dendrites: a few short filaments per neuron, in fixed 3D directions (from the topic id, so they do
    // not change between redraws). A branch that points at the viewer is short on screen, as it should be.
    dendrites(id) {
      let d = this.dend.get(id);
      if (d) return d;
      const seed = hash(id), k = 5 + (seed % 3);
      d = [];
      for (let i = 0; i < k; i++) {
        const y = 1 - (2 * (i + 0.5)) / k, r = Math.sqrt(1 - y * y), a = i * 2.39996 + (seed % 628) / 100;
        const x = Math.cos(a) * r, z = Math.sin(a) * r, len3 = Math.hypot(x, y, z) || 1;
        d.push({ x: x / len3, y: y / len3, z: z / len3, bend: (seed >> i) & 1 ? 1 : -1,
          fork: ((seed >> (i + 3)) & 1) === 1, reach: 1.9 + (((seed >> (i + 5)) & 3) * 0.25) });
      }
      this.dend.set(id, d);
      return d;
    }

    draw(now) {
      const ctx = this.ctx, dim = this.dimming();
      this.runPending();
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      this.project();
      const dist = this.cam.dist, fog = (d) => clamp(1.25 - (d / dist) * 0.5, 0.25, 1);
      const order = this.nodes.filter((n) => n.sv).sort((a, b) => b.depth - a.depth);
      for (const n of order) this.drawDendrites(ctx, n, dim, fog);
      for (const l of this.links) if (l.s.sv && l.t.sv) this.drawAxon(ctx, l, dim, fog, now);
      for (const n of order) this.drawBody(ctx, n, dim, fog, now);
      this.drawLabels(ctx, order, dim);
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
    }

    drawDendrites(ctx, n, dim, fog) {
      const R = this.bodyR(n), c = this.cam;
      const cy = Math.cos(c.yaw), sy = Math.sin(c.yaw), cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
      const base = (!dim || this.lit(n) ? 0.62 : 0.1) * fog(n.depth);
      ctx.strokeStyle = "#6f879f";
      ctx.lineCap = "round";
      for (const d of this.dendrites(n.id)) {
        const x1 = d.x * cy - d.z * sy, z1 = d.x * sy + d.z * cy, y2 = d.y * cp - z1 * sp;
        const vx = x1, vy = -y2, len = Math.hypot(vx, vy);
        if (len < 0.2) continue;
        const x0 = n.sx + vx * R * 0.8, y0 = n.sy + vy * R * 0.8;
        const x2 = n.sx + vx * R * d.reach, y2s = n.sy + vy * R * d.reach;
        const cx = (x0 + x2) / 2 - vy * R * 0.3 * d.bend, cyy = (y0 + y2s) / 2 + vx * R * 0.3 * d.bend;
        ctx.globalAlpha = base * clamp(len, 0.35, 1);
        ctx.lineWidth = Math.max(0.7, R * 0.14);
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.quadraticCurveTo(cx, cyy, x2, y2s);
        ctx.stroke();
        if (d.fork && R > 4) { // one side branch near the tip
          const fx = x0 + (x2 - x0) * 0.62, fy = y0 + (y2s - y0) * 0.62;
          ctx.beginPath();
          ctx.moveTo(fx, fy);
          ctx.lineTo(fx - vy * R * 0.55 * d.bend + vx * R * 0.2, fy + vx * R * 0.55 * d.bend + vy * R * 0.2);
          ctx.stroke();
        }
      }
    }

    // An axon: a curve from one body to the next, its bend fixed by the pair of ids. Status is the line
    // style (solid verified, dashed inference, dotted unknown). On a prerequisite the arrowhead marks the
    // target, and a small bead travels from the source to the target.
    drawAxon(ctx, l, dim, fog, now) {
      const s = l.s, t = l.t, dx = t.sx - s.sx, dy = t.sy - s.sy, L = Math.hypot(dx, dy);
      const Rs = this.bodyR(s), Rt = this.bodyR(t);
      if (L < 2 || L < (Rs + Rt) * 0.9) return;
      const ux = dx / L, uy = dy / L;
      const x0 = s.sx + ux * Rs * 0.9, y0 = s.sy + uy * Rs * 0.9;
      const x1 = t.sx - ux * Rt * 1.05, y1 = t.sy - uy * Rt * 1.05;
      const bend = (hash(l.source + ">" + l.target) & 1 ? 1 : -1) * 0.16 * L;
      const mx = (x0 + x1) / 2 - uy * bend, my = (y0 + y1) / 2 + ux * bend;
      const lit = !dim || this.linkLit(s, t);
      const a = (lit ? 0.85 : 0.07) * fog((s.depth + t.depth) / 2) * (l.status === "verified" ? 1 : 0.82);
      if (a < 0.02) return;
      const T = TYPE[l.type] || TYPE.ortak;
      ctx.globalAlpha = a;
      ctx.strokeStyle = T.color;
      ctx.lineWidth = T.width * (dim && lit ? 1.2 : 1);
      ctx.setLineDash(DASH[l.status] || DASH.unknown);
      ctx.lineDashOffset = 0;
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.quadraticCurveTo(mx, my, x1, y1);
      ctx.stroke();
      ctx.setLineDash([]);
      if (l.type !== "onkosul") return;
      const ex = x1 - mx, ey = y1 - my, el = Math.hypot(ex, ey) || 1, hx = ex / el, hy = ey / el, sz = 5;
      ctx.fillStyle = T.color;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x1 - hx * sz - hy * sz * 0.6, y1 - hy * sz + hx * sz * 0.6);
      ctx.lineTo(x1 - hx * sz + hy * sz * 0.6, y1 - hy * sz - hx * sz * 0.6);
      ctx.closePath();
      ctx.fill();
      if (!lit) return;
      const u = (now / PULSE_MS + (hash(l.source) % 1000) / 1000) % 1, v = 1 - u;
      const px = v * v * x0 + 2 * v * u * mx + u * u * x1, py = v * v * y0 + 2 * v * u * my + u * u * y1;
      ctx.globalAlpha = a * 0.22;
      ctx.beginPath(); ctx.arc(px, py, 4.4, 0, TAU); ctx.fill();
      ctx.globalAlpha = Math.min(1, a * 1.1);
      ctx.beginPath(); ctx.arc(px, py, 2.1, 0, TAU); ctx.fill();
    }

    // A soft body: a pale radial gradient, a thin membrane line, a faint nucleus. No glow.
    drawBody(ctx, n, dim, fog, now) {
      const R = this.bodyR(n), x = n.sx, y = n.sy, focus = this.isFocus(n);
      const base = (!dim || this.lit(n) ? 1 : 0.22) * fog(n.depth);
      const [c0, c1, c2, edge] = focus
        ? ["#fffaf6", "#f4dccd", "#dca88b", "#a4532b"]
        : ["#ffffff", "#edf3f8", "#c4d3e2", "#5f7b98"];
      const g = ctx.createRadialGradient(x - R * 0.3, y - R * 0.35, R * 0.1, x, y, R * 1.15);
      g.addColorStop(0, c0);
      g.addColorStop(0.6, c1);
      g.addColorStop(1, c2);
      ctx.globalAlpha = base;
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, R, 0, TAU); ctx.fill();
      ctx.strokeStyle = edge;
      ctx.lineWidth = clamp(R * 0.14, 0.8, 1.8);
      ctx.stroke();
      if (R > 6) {
        ctx.globalAlpha = base * 0.45;
        ctx.lineWidth = 0.8;
        ctx.beginPath(); ctx.arc(x + R * 0.12, y + R * 0.1, R * 0.36, 0, TAU); ctx.stroke();
      }
      if (this.selected === n) {
        ctx.globalAlpha = base;
        ctx.strokeStyle = ACCENT;
        ctx.lineWidth = 1.8;
        ctx.setLineDash([4, 4]);
        ctx.lineDashOffset = -now / 60;
        ctx.beginPath(); ctx.arc(x, y, R + 6, 0, TAU); ctx.stroke();
        ctx.setLineDash([]);
      } else if (focus) {
        ctx.globalAlpha = base * 0.5;
        ctx.strokeStyle = FOCUS;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, R + 6, 0, TAU); ctx.stroke();
      } else if (this.hover === n) {
        ctx.globalAlpha = 0.45;
        ctx.strokeStyle = ACCENT;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, R + 4, 0, TAU); ctx.stroke();
      }
    }

    // Labels: the searched neuron (name and grade above it), the selected and the hovered one, then the
    // best connected. A label that would sit on another one is left out.
    drawLabels(ctx, order, dim) {
      const want = [];
      const add = (n) => { if (n && n.sv && !want.includes(n)) want.push(n); };
      add(this.byId.get(this.focusId));
      add(this.selected);
      add(this.hover);
      const rest = order.filter((n) => !dim || this.lit(n)).sort((a, b) => b.deg - a.deg || a.depth - b.depth);
      for (const n of rest) { if (want.length >= 44) break; add(n); }
      const placed = [];
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.lineJoin = "round";
      for (const n of want) {
        const focus = this.isFocus(n), R = this.bodyR(n);
        const strong = focus || n === this.selected || n === this.hover;
        const size = focus ? 16 : clamp(11 + n.ss * 2, 11, 13);
        const title = `${n.title} (${n.grades.join("-")})`;
        ctx.font = `${strong ? 600 : 500} ${size}px system-ui, -apple-system, "Segoe UI", sans-serif`;
        const w = ctx.measureText(title).width;
        const y = focus ? n.sy - R - 12 - size - 18 : n.sy + R + 4;
        const box = { x0: n.sx - w / 2 - 4, x1: n.sx + w / 2 + 4, y0: y - 2, y1: y + size + (focus ? 20 : 4) };
        if (!strong && placed.some((p) => box.x0 < p.x1 && p.x0 < box.x1 && box.y0 < p.y1 && p.y0 < box.y1)) continue;
        placed.push(box);
        ctx.globalAlpha = dim && !this.lit(n) ? 0.35 : 1;
        ctx.lineWidth = 3;
        ctx.strokeStyle = PAGE;
        ctx.strokeText(title, n.sx, y);
        ctx.fillStyle = focus ? FOCUS_INK : INK;
        ctx.fillText(title, n.sx, y);
        if (focus) {
          ctx.font = `500 12px system-ui, -apple-system, "Segoe UI", sans-serif`;
          const line = `Sınıf ${n.grades.join("-")}`;
          ctx.strokeText(line, n.sx, y + size + 3);
          ctx.fillStyle = "#5d6a7b";
          ctx.fillText(line, n.sx, y + size + 3);
        }
      }
    }
  }

  // -- state -----------------------------------------------------------------------------------------
  // viewSeq counts the view changes (landing or search): a slow answer for an older view never replaces a newer one.
  const S = { overview: null, overviewLoading: null, details: new Map(), edges: [], focus: null,
    graphSeq: 0, searchSeq: 0, viewSeq: 0, panelFor: null };

  // Spacing: at the older -170 / 56 / 0.03 the closest pair on the landing network was 48 units apart
  // (median 66); at these values it is 76 (median 121), and the closest screen gap is no longer negative.
  const graph = new NeuronGraph($("#canvas"), {
    charge: -500, distance: 110, gravity: 0.015,
    color: () => "#ffffff", linkColor: () => "#777", theme: () => ({}),
    onSelect: (n) => onNodeClick(n),
    onHover: (n, ev) => onNodeHover(n, ev),
  });
  graph.setActive(true);

  // -- helpers ---------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function setStatus(msg, isError) {
    const s = $("#status");
    s.textContent = msg;
    s.classList.toggle("error", !!isError);
  }

  function showNotes(list) {
    const box = $("#notes");
    const ul = $("#notes-list");
    ul.textContent = "";
    for (const u of list || []) ul.append(el("li", "", u));
    box.hidden = !(list && list.length);
  }

  async function fetchJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.message || `istek başarısız (${r.status})`);
    return body;
  }

  function rememberDetails(nodes) {
    for (const n of nodes) S.details.set(n.id, n);
  }

  function gradeText(grades) {
    return grades && grades.length ? `Sınıf ${grades.join("-")}` : "";
  }

  function countStatus(edges) {
    const c = { verified: 0, inference: 0, unknown: 0 };
    for (const e of edges) c[e.status] = (c[e.status] || 0) + 1;
    return c;
  }

  function isTyping(t) {
    return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
  }

  // -- the graph: one function swaps the data, with a short fade ------------------------------------
  function showGraph({ nodes, edges, focus }) {
    const seq = ++S.graphSeq;
    const canvas = $("#canvas");
    canvas.classList.add("swap");
    setTimeout(() => {
      if (seq !== S.graphSeq) return; // a newer graph is on its way
      S.edges = edges;
      S.focus = focus;
      graph.selected = null;
      graph.hover = null;
      graph.focusId = focus;
      graph.spin = true;
      graph.autoFrame = !focus;
      graph.setData(
        nodes.map((n) => ({ id: n.id, title: n.title, grades: n.grades })),
        edges.map((e) => ({ source: e.source, target: e.target, type: e.type, status: e.status, evidence: e.evidence })),
      );
      graph.pending = focus ? { type: "focus", id: focus } : { type: "fit" };
      canvas.classList.remove("swap");
    }, 240);
  }

  // `note` (optional) replaces the count in the status line: used when a failed search falls back here.
  async function showLanding(note) {
    const view = ++S.viewSeq;
    closePanel();
    $("#q").value = "";
    setStatus("Ana ağ hazırlanıyor…");
    try {
      if (!S.overview) {
        if (!S.overviewLoading) S.overviewLoading = fetchJSON("/api/overview").then((d) => (S.overview = d));
        await S.overviewLoading;
      }
      if (view !== S.viewSeq) return; // a search started while the overview loaded: it stays
      const data = S.overview;
      rememberDetails(data.nodes);
      setStatus(note || `${data.nodes.length} konu, ${data.edges.length} kanıtlı ilişki`, !!note);
      showNotes(data.unknowns);
      showGraph({ nodes: data.nodes, edges: data.edges, focus: null });
    } catch (err) {
      S.overviewLoading = null;
      if (view === S.viewSeq) setStatus(`Ana ağ yüklenemedi: ${err.message}`, true);
    }
  }

  // Resolves to false when there is nothing to show, so the caller can fall back to the landing network.
  async function runSearch(q) {
    const query = (q || "").trim();
    if (!query) return false;
    const seq = ++S.searchSeq;
    const view = ++S.viewSeq;
    $("#q").value = query;
    setStatus(`“${query}” aranıyor…`);
    try {
      const data = await fetchJSON(`/api/graph?q=${encodeURIComponent(query)}`);
      if (seq !== S.searchSeq || view !== S.viewSeq) return true; // a newer search or the landing network came first
      showNotes(data.unknowns);
      if (!data.nodes.length) {
        setStatus(`“${query}” için konu bulunamadı.`, true);
        return false;
      }
      rememberDetails(data.nodes);
      const c = countStatus(data.edges);
      setStatus(`${data.edges.length} ilişki: ${c.verified} kanıtlı, ${c.inference} çıkarım, ${c.unknown} bilinmiyor`);
      closePanel();
      showGraph({ nodes: data.nodes, edges: data.edges, focus: data.center });
      return true;
    } catch (err) {
      if (seq === S.searchSeq && view === S.viewSeq) setStatus(`Arama yapılamadı: ${err.message}`, true);
      return false;
    }
  }

  // -- the panel: summary, then prerequisites and what it is required for, then sources ----------
  function openPanel(id) {
    const node = S.details.get(id) || { id, title: id, grades: [], summary: "", uses: [] };
    S.panelFor = id;
    $("#panel-title").textContent = node.title;
    $("#panel-grades").textContent = gradeText(node.grades);
    const body = $("#panel-body");
    body.textContent = "";
    body.append(panelBody(node));
    body.scrollTop = 0;
    $("#panel").classList.add("open");
    $("#panel").setAttribute("aria-hidden", "false");
    $("#graph").classList.add("shift");
  }

  function closePanel() {
    S.panelFor = null;
    graph.selected = null;
    $("#panel").classList.remove("open");
    $("#panel").setAttribute("aria-hidden", "true");
    $("#graph").classList.remove("shift");
  }

  function tagType(type) {
    const T = TYPE[type] || TYPE.ortak;
    return el("span", `tag tag-${type}`, T.label);
  }

  function tagStatus(status) {
    return el("span", `tag tag-${status}`, STATUS[status] || STATUS.unknown);
  }

  function relItem(other, e, why) {
    const li = el("li", "rel");
    const head = el("div", "rel-head");
    const name = other.grades && other.grades.length ? `${other.title} (${other.grades.join("-")})` : other.title;
    head.append(el("span", "rel-title", name), tagType(e.type), tagStatus(e.status));
    li.append(head, el("div", "rel-why", why));
    const quote = e.evidence && e.evidence[0] && e.evidence[0].quote;
    if (quote) li.append(el("div", "rel-quote", `“${quote}”`));
    const go = el("button", "rel-go", "Bu konunun ağını aç");
    go.type = "button";
    go.addEventListener("click", () => runSearch(other.title));
    li.append(go);
    return li;
  }

  function section(title, child) {
    const s = el("section");
    if (title) s.append(el("h3", "", title));
    if (child) s.append(child);
    return s;
  }

  function panelBody(node) {
    const frag = document.createDocumentFragment();
    const id = node.id, edges = S.edges;
    const others = (e) => {
      const oid = e.source === id ? e.target : e.source;
      return S.details.get(oid) || { id: oid, title: oid, grades: [] };
    };

    // A click on a landing neuron only opens this panel; the search starts from this button alone.
    frag.append(section("", el("p", "summary", node.summary || "Bu konu için özet yok.")));
    if (S.focus !== id) {
      const b = el("button", "btn-centre", "Bu konuyu merkez yap");
      b.type = "button";
      b.addEventListener("click", () => runSearch(node.title));
      frag.append(section("", b));
    }

    const uses = el("ul", "uses");
    for (const u of node.uses || []) uses.append(el("li", "", u));
    frag.append(section("Nerelerde kullanılır", uses.children.length ? uses : el("p", "none", "Bu konu için kullanım cümlesi pasajda bulunamadı.")));

    const before = edges.filter((e) => e.type === "onkosul" && e.target === id);
    const after = edges.filter((e) => e.type === "onkosul" && e.source === id);
    const prereqs = el("ul");
    for (const e of before) prereqs.append(relItem(others(e), e, "Bu konudan önce gelir."));
    frag.append(section("Bu konudan önce öğrenilmesi gerekenler",
      prereqs.children.length ? prereqs : el("p", "none", S.focus ? "Bu ağda bu konudan önce gelen kanıtlı bir konu yok." : "Ana ağda bu konudan önce gelen kanıtlı bir konu yok. Arayarak çevresine bakın.")));

    const needed = el("ul");
    for (const e of after) needed.append(relItem(others(e), e, "Bu konudan sonra gelir."));
    frag.append(section("Bu konu hangi konular için gerekli",
      needed.children.length ? needed : el("p", "none", S.focus ? "Bu ağda bu konuyu gerektiren kanıtlı bir konu yok." : "Ana ağda bu konuyu gerektiren kanıtlı bir konu yok.")));

    if (node.study) {
      const order = el("p", "study-order", `${ORDER_TEXT[node.study.order] || "Çalışma sırası"}. ${node.study.why || ""}`.trim());
      frag.append(section("Ne zaman çalışılmalı", order));
    }

    const related = el("ul");
    for (const e of edges) {
      if (e.type === "onkosul" || (e.source !== id && e.target !== id)) continue;
      const why = e.type === "destek" ? "İki konu birbirini destekler." : "Ortak bir kavramı paylaşır; sırası yoktur.";
      related.append(relItem(others(e), e, why));
    }
    if (related.children.length) frag.append(section("İlgili ve destekleyen konular", related));

    const sources = el("ul", "sources");
    for (const s of (node.study && node.study.sources) || []) {
      const li = el("li");
      const head = el("div", "rel-head");
      head.append(el("span", "src-name", s.ad), el("span", "src-type", `— ${s.tur}`));
      const flag = s.verified ? el("span", "tag tag-src-verified", "Doğrulandı") : el("span", "tag tag-src-unverified", "Doğrulanmadı");
      head.append(flag);
      li.append(head);
      if (s.url) {
        const link = el("a", "src-link", "Kaynağa git ↗");
        link.href = s.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        li.append(link);
      }
      sources.append(li);
    }
    const srcNote = el("p", "none", "Bağlantısı olan kaynaklar resmi adrese gider. Bağlantısı olmayan kaynakların adresi bu sürümde doğrulanmadı.");
    frag.append(section("Hangi kaynaklardan çalışılmalı", sources.children.length ? sources : el("p", "none", "Bu konu için kaynak kaydı yok.")));
    frag.lastChild.append(srcNote);
    return frag;
  }

  // -- input -----------------------------------------------------------------------------------------
  function onNodeClick(n) {
    if (!n) { closePanel(); return; }
    graph.selected = n;
    graph.spin = false;
    openPanel(n.id);
    graph.flyToNode(n.id, 900);
  }

  function onNodeHover(n, ev) {
    const tip = $("#tip");
    if (!n || !ev) { tip.hidden = true; return; }
    tip.textContent = "";
    tip.append(el("div", "", `${n.title} (${n.grades.join("-")})`));
    const anchor = graph.selected || (S.focus && graph.byId.get(S.focus));
    if (anchor && anchor !== n) {
      const e = S.edges.find((x) => (x.source === anchor.id && x.target === n.id) || (x.source === n.id && x.target === anchor.id));
      tip.append(el("small", "", e ? `${TYPE[e.type].label} · ${STATUS[e.status]}` : "Bu ağda bu konuyla doğrudan bağ yok"));
    } else {
      tip.append(el("small", "", `${(graph.adj.get(n.id) || new Set()).size} bağlantı`));
    }
    tip.hidden = false;
    tip.style.left = `${ev.clientX + 14}px`;
    tip.style.top = `${ev.clientY + 12}px`;
  }

  $("#brand").addEventListener("click", () => showLanding());

  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runSearch($("#q").value);
  });

  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => runSearch(chip.dataset.q));
  }

  $("#panel-close").addEventListener("click", () => closePanel());

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (S.panelFor !== null) closePanel();
      return;
    }
    if ((e.key === "h" || e.key === "H") && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping(e.target)) {
      e.preventDefault();
      showLanding();
    }
  });

  fetchJSON("/api/topics").then((topics) => {
    const list = $("#topic-list");
    for (const t of topics) {
      const o = el("option");
      o.value = t.title;
      list.append(o);
    }
  }).catch(() => { /* the search box works without suggestions */ });

  // ?q=... opens the search for that text as the centre. Nothing else starts the landing network then,
  // and if the text finds nothing the landing network is shown with a note.
  const initialQ = new URLSearchParams(window.location.search).get("q") || "";
  if (initialQ.trim()) {
    runSearch(initialQ).then((ok) => {
      if (!ok) showLanding(`“${initialQ.trim()}” gösterilemedi; ana ağ açıldı.`);
    });
  } else {
    showLanding();
  }
})();
