(() => {
  "use strict";

  // Relation types from SPEC.md. "onkosul" reads source -> target: the target is understood after the source.
  const TYPES = {
    onkosul: { label: "Ön koşul", color: "#f5b14c" },
    destek: { label: "Destek / uygulama", color: "#3fd0c9" },
    ortak: { label: "Ortak kavram", color: "#b193ff" },
  };
  const OTHER_COLOR = "#8aa0c0";
  const STATUS_LONG = {
    verified: "doğrulandı (metinde alıntı var)",
    inference: "çıkarım (metinde alıntı yok)",
    unknown: "bilinmiyor",
  };
  const STATUS_SHORT = { verified: "doğrulandı", inference: "çıkarım", unknown: "bilinmiyor" };
  // Sector centre per branch, in radians (0 = right, positive = down).
  const GROUP_ANGLE = { onkosul: -2.6, destek: -0.55, ortak: 1.57, diger: Math.PI };
  const EXAMPLES = ["fotosentez", "mitoz", "sinir sistemi", "nükleik asitler"];
  const LABEL_FONT = "12.5px system-ui, 'Segoe UI', sans-serif";
  const CENTER_FONT = "600 15px system-ui, 'Segoe UI', sans-serif";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const $ = (id) => document.getElementById(id);
  const canvas = $("graph");
  const ctx = canvas.getContext("2d");
  const stage = $("stage");
  const panel = $("panel");
  const tip = $("tip");

  let graph = null;        // last answer of /api/graph
  let byId = new Map();    // node id -> node
  let geo = new Map();     // node id -> layout entry (canvas CSS pixels)
  let view = { w: 0, h: 0, dpr: 1 };
  let hover = null;        // { kind: "node", id } | { kind: "edge", index }
  let selected = null;     // node id whose panel is open
  let lastFocus = null;
  let lastT = 0;

  // ---- helpers -----------------------------------------------------------

  function rgba(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  function gradeText(n) {
    return n.grades && n.grades.length ? ` (${n.grades.join("-")})` : "";
  }

  function labelOf(n) {
    return (n.title || n.id) + gradeText(n);
  }

  function snippet(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // The edge that joins a node to the searched topic, if any.
  function centreEdge(id) {
    if (!graph) return null;
    return graph.edges.find(
      (e) => (e.source === graph.center && e.target === id) || (e.target === graph.center && e.source === id)
    ) || null;
  }

  function groupOf(id) {
    const e = centreEdge(id);
    return e && TYPES[e.type] ? e.type : "diger";
  }

  function edgeColor(e) {
    return (TYPES[e.type] && TYPES[e.type].color) || OTHER_COLOR;
  }

  function isCentreEdge(e) {
    return graph && (e.source === graph.center || e.target === graph.center);
  }

  // Quadratic curve between two nodes; the bulge makes the edges read as curves, not straight lines.
  function control(a, b, e) {
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const side = e.source < e.target ? 1 : -1;
    const bulge = 0.16 * len * side;
    return { x: mx - (dy / len) * bulge, y: my + (dx / len) * bulge };
  }

  function edgeStyle(e, index) {
    const centre = isCentreEdge(e);
    const hot = hover && hover.kind === "edge" && hover.index === index;
    let dash = [];
    let alpha = centre ? 0.8 : 0.3;
    if (e.status === "inference") dash = [6, 5];
    if (e.status === "unknown") { dash = [2, 4]; alpha *= 0.7; }
    let width = centre ? 1.8 : 1.1;
    if (hot) { width += 1.4; alpha = 1; }
    return { color: edgeColor(e), dash, alpha, width };
  }

  // ---- layout ------------------------------------------------------------

  function layout() {
    geo = new Map();
    if (!graph || !graph.nodes.length || !view.w) return;
    const { w, h } = view;
    const cx = w / 2;
    const cy = h / 2;
    const rx = Math.max(110, Math.min(w * 0.36, 430));
    const ry = Math.max(100, Math.min(h * 0.34, 260));

    const groups = {};
    for (const n of graph.nodes) {
      if (n.id === graph.center) continue;
      const g = groupOf(n.id);
      groups[g] = groups[g] || [];
      groups[g].push(n);
    }

    Object.keys(groups).forEach((g) => {
      const list = groups[g];
      const k = list.length;
      const step = k > 1 ? Math.min(0.42, 1.9 / (k - 1)) : 0;
      list.forEach((n, i) => {
        const a = GROUP_ANGLE[g] + (i - (k - 1) / 2) * step;
        const depth = i % 2 ? 0.8 : 1;   // alternate radius so neighbours in one sector do not stack
        geo.set(n.id, {
          x: cx + rx * depth * Math.cos(a),
          y: cy + ry * depth * Math.sin(a),
          phase: hashPhase(n.id),
          color: g === "diger" ? OTHER_COLOR : TYPES[g].color,
          isCentre: false,
        });
      });
    });

    if (byId.has(graph.center)) {
      geo.set(graph.center, { x: cx, y: cy, phase: 0, color: "#ffffff", isCentre: true });
    }

    // Keep every label inside the canvas.
    for (const [id, g] of geo) {
      const n = byId.get(id);
      ctx.font = g.isCentre ? CENTER_FONT : LABEL_FONT;
      const half = ctx.measureText(labelOf(n)).width / 2 + 10;
      g.x = Math.min(Math.max(g.x, half), w - half);
      g.y = Math.min(Math.max(g.y, 52), h - 34);
    }
  }

  function hashPhase(id) {
    let s = 0;
    for (const ch of id) s = (s * 31 + ch.charCodeAt(0)) % 997;
    return s / 100;
  }

  // Positions with a slow drift so the graph looks alive; the centre stays still.
  function pos(id, t) {
    const g = geo.get(id);
    if (g.isCentre || reduceMotion) return { x: g.x, y: g.y };
    return {
      x: g.x + 2.2 * Math.sin(t * 0.0005 + g.phase),
      y: g.y + 2.2 * Math.cos(t * 0.0006 + g.phase * 1.3),
    };
  }

  // ---- drawing -----------------------------------------------------------

  function draw(t) {
    const { w, h, dpr } = view;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const bg = ctx.createRadialGradient(w / 2, h / 2, 10, w / 2, h / 2, Math.max(w, h) * 0.7);
    bg.addColorStop(0, "#14233d");
    bg.addColorStop(1, "#050a14");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);
    if (!graph || !geo.size) return;

    const P = new Map();
    for (const id of geo.keys()) P.set(id, pos(id, t));

    // Faint links between neighbours first, then the links to the searched topic.
    const order = graph.edges.map((e, i) => i).sort((a, b) => Number(isCentreEdge(graph.edges[a])) - Number(isCentreEdge(graph.edges[b])));
    for (const i of order) {
      const e = graph.edges[i];
      const a = P.get(e.source);
      const b = P.get(e.target);
      if (!a || !b) continue;
      const st = edgeStyle(e, i);
      const c = control(a, b, e);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(c.x, c.y, b.x, b.y);
      ctx.strokeStyle = rgba(st.color, st.alpha);
      ctx.lineWidth = st.width;
      ctx.setLineDash(st.dash);
      ctx.stroke();
      ctx.restore();
    }

    for (const [id, g] of geo) {
      const p = P.get(id);
      const r = g.isCentre ? 9 : 6;
      const breath = reduceMotion ? 1 : 1 + 0.08 * Math.sin(t * 0.0021 + g.phase);
      const glowR = 30 * breath;
      const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, glowR);
      glow.addColorStop(0, rgba(g.color, 0.55));
      glow.addColorStop(1, rgba(g.color, 0));
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(p.x, p.y, glowR, 0, Math.PI * 2);
      ctx.fill();

      ctx.beginPath();
      ctx.arc(p.x, p.y, r * breath, 0, Math.PI * 2);
      ctx.fillStyle = "#f4f8ff";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = g.color;
      ctx.stroke();

      const isHot = (hover && hover.kind === "node" && hover.id === id) || selected === id;
      if (isHot) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 6, 0, Math.PI * 2);
        ctx.strokeStyle = rgba(g.color, 0.9);
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      const n = byId.get(id);
      ctx.textAlign = "center";
      if (g.isCentre) {
        ctx.font = CENTER_FONT;
        ctx.fillStyle = "#ffffff";
        ctx.fillText(labelOf(n), p.x, p.y - 18);
      } else {
        ctx.font = LABEL_FONT;
        ctx.fillStyle = isHot ? "#ffffff" : "#c9d6ea";
        ctx.fillText(labelOf(n), p.x, p.y + 20);
      }
    }
  }

  function quadPoint(a, c, b, s) {
    const u = 1 - s;
    return { x: u * u * a.x + 2 * u * s * c.x + s * s * b.x, y: u * u * a.y + 2 * u * s * c.y + s * s * b.y };
  }

  function hitTest(x, y, t) {
    let node = null;
    let bestD = 18;
    for (const [id, g] of geo) {
      const p = pos(id, t);
      const d = Math.hypot(p.x - x, p.y - y);
      if (d < bestD) { bestD = d; node = id; }
    }
    if (node) return { kind: "node", id: node };

    let edge = -1;
    let bestE = 8;
    graph.edges.forEach((e, i) => {
      const a = geo.has(e.source) && pos(e.source, t);
      const b = geo.has(e.target) && pos(e.target, t);
      if (!a || !b) return;
      const c = control(a, b, e);
      for (let s = 0; s <= 20; s++) {
        const q = quadPoint(a, c, b, s / 20);
        const d = Math.hypot(q.x - x, q.y - y);
        if (d < bestE) { bestE = d; edge = i; }
      }
    });
    return edge >= 0 ? { kind: "edge", index: edge } : null;
  }

  function tipText(hit) {
    if (hit.kind === "node") {
      const n = byId.get(hit.id);
      const title = n.title || n.id;
      if (hit.id === graph.center) return `${title}: aranan konu`;
      const e = centreEdge(hit.id);
      if (!e) return `${title}: bu konuya doğrudan bağlı değil`;
      return `${title}: ${TYPES[e.type] ? TYPES[e.type].label : e.type} · ${STATUS_SHORT[e.status] || e.status}`;
    }
    const e = graph.edges[hit.index];
    const type = TYPES[e.type] ? TYPES[e.type].label : e.type;
    const quote = e.evidence && e.evidence[0] ? `“${snippet(e.evidence[0].quote, 110)}”` : "kanıt yok";
    return `${type} · ${STATUS_LONG[e.status] || e.status}\n${quote}`;
  }

  function setHover(hit, x, y) {
    hover = hit;
    if (!hit || !graph) {
      tip.hidden = true;
      canvas.style.cursor = "default";
      return;
    }
    canvas.style.cursor = hit.kind === "node" ? "pointer" : "default";
    tip.textContent = tipText(hit);
    tip.hidden = false;
    const maxLeft = Math.max(0, view.w - tip.offsetWidth - 8);
    tip.style.left = Math.min(x + 14, maxLeft) + "px";
    tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
  }

  // ---- canvas sizing and events -----------------------------------------

  function resize() {
    const rect = stage.getBoundingClientRect();
    view.w = rect.width;
    view.h = rect.height;
    view.dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(view.w * view.dpr);
    canvas.height = Math.round(view.h * view.dpr);
    layout();
  }

  canvas.addEventListener("pointermove", (ev) => {
    if (!graph) return;
    const r = canvas.getBoundingClientRect();
    const x = ev.clientX - r.left;
    const y = ev.clientY - r.top;
    setHover(hitTest(x, y, lastT), x, y);
  });

  canvas.addEventListener("pointerleave", () => setHover(null));

  // A tap (touch) arrives as click without a move, so the click also sets the tip and opens a node.
  canvas.addEventListener("click", (ev) => {
    if (!graph) return;
    const r = canvas.getBoundingClientRect();
    const x = ev.clientX - r.left;
    const y = ev.clientY - r.top;
    const hit = hitTest(x, y, lastT);
    setHover(hit, x, y);
    if (hit && hit.kind === "node") openPanel(hit.id);
  });

  // ---- data and status ---------------------------------------------------

  function setStatus(msg, isError) {
    const s = $("status");
    s.textContent = msg;
    s.classList.toggle("error", Boolean(isError));
  }

  async function search(text) {
    const query = text.trim();
    if (!query) {
      setStatus("Bir konu yazın.");
      return;
    }
    setStatus("Aranıyor…");
    try {
      const res = await fetch("/api/graph?" + new URLSearchParams({ q: query }).toString());
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || "bilinmeyen hata");
      try { history.replaceState(null, "", "?q=" + encodeURIComponent(query)); } catch (_) { /* optional */ }
      setGraph(body);
    } catch (err) {
      setStatus("Arama yapılamadı: " + err.message, true);
    }
  }

  function setGraph(body) {
    graph = body;
    byId = new Map(body.nodes.map((n) => [n.id, n]));
    closePanel();
    setHover(null);
    hover = null;

    const empty = $("empty");
    if (!body.nodes.length) {
      $("empty-text").textContent = "Bu aramaya karşılık gelen konu yok. Başka bir konu deneyin.";
      empty.hidden = false;
      setStatus("Sonuç yok.");
    } else {
      empty.hidden = true;
      setStatus(`“${body.query}” için ${body.nodes.length} konu, ${body.edges.length} ilişki.`);
    }

    const unknowns = body.unknowns || [];
    $("unknowns-box").hidden = unknowns.length === 0;
    const ul = $("unknowns");
    ul.textContent = "";
    for (const u of unknowns) {
      const li = document.createElement("li");
      li.textContent = u;
      ul.appendChild(li);
    }

    resize();
    renderChips();
  }

  function renderChips() {
    const nav = $("chips");
    nav.textContent = "";
    if (!graph) return;
    const ordered = graph.nodes.slice().sort((a, b) => (a.id === graph.center ? -1 : b.id === graph.center ? 1 : 0));
    for (const n of ordered) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = labelOf(n);
      b.addEventListener("click", () => openPanel(n.id));
      nav.appendChild(b);
    }
  }

  // ---- side panel --------------------------------------------------------

  function isMeb(src) {
    return /MEB/i.test(`${src.ad || ""} ${src.tur || ""}`);
  }

  function relationText(e, id) {
    if (e.type === "onkosul") return e.source === id ? "Bu konudan sonra gelir" : "Bu konudan önce öğrenilir";
    if (e.type === "destek") return "Destek ilişkisi";
    if (e.type === "ortak") return "Ortak kavram";
    return e.type;
  }

  function fillPanel(n) {
    $("p-title").textContent = n.title || n.id;
    $("p-grades").textContent = n.grades && n.grades.length
      ? (n.grades.length > 1 ? "Sınıflar: " : "Sınıf: ") + n.grades.join(", ")
      : "Sınıf bilgisi yok";
    $("p-summary").textContent = n.summary || "Bu konu için özet yok.";

    const uses = $("p-uses");
    uses.textContent = "";
    const useList = n.uses && n.uses.length ? n.uses : ["Bu konu için kullanım notu yok."];
    for (const u of useList) {
      const li = document.createElement("li");
      li.textContent = u;
      uses.appendChild(li);
    }

    const study = n.study || {};
    const sources = (study.sources || []).slice().sort((a, b) => Number(isMeb(b)) - Number(isMeb(a)));
    const src = $("p-sources");
    src.textContent = "";
    if (!sources.length) {
      const li = document.createElement("li");
      li.textContent = "Kaynak listesi yok.";
      src.appendChild(li);
    }
    for (const s of sources) {
      const li = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = s.ad || "Adsız kaynak";
      li.appendChild(name);
      if (s.tur) {
        const t = document.createElement("span");
        t.className = "badge";
        t.textContent = s.tur;
        li.appendChild(t);
      }
      if (isMeb(s)) {
        const m = document.createElement("span");
        m.className = "badge verified";
        m.textContent = "MEB";
        li.appendChild(m);
      }
      if (!s.verified) {
        const w = document.createElement("span");
        w.className = "badge warn";
        w.textContent = "doğrulanmadı";
        li.appendChild(w);
      }
      if (s.url) {
        li.appendChild(document.createElement("br"));
        const a = document.createElement("a");
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "Bağlantıyı aç";
        li.appendChild(a);
      } else {
        const none = document.createElement("span");
        none.className = "muted small";
        none.textContent = " Bağlantı yok.";
        li.appendChild(none);
      }
      src.appendChild(li);
    }

    $("p-study").textContent = study.order
      ? `Sıra ${study.order}. ${study.why || ""}`.trim()
      : (study.why || "Çalışma sırası belirtilmemiş.");

    const rel = $("p-rel");
    rel.textContent = "";
    const touching = graph.edges.filter((e) => e.source === n.id || e.target === n.id);
    if (!touching.length) {
      const li = document.createElement("li");
      li.textContent = "Haritada bu konuya bağlı ilişki yok.";
      rel.appendChild(li);
    }
    for (const e of touching) {
      const other = e.source === n.id ? e.target : e.source;
      const li = document.createElement("li");
      const head = document.createElement("strong");
      const otherNode = byId.get(other);
      head.textContent = (otherNode ? otherNode.title || other : other);
      li.appendChild(head);
      const meta = document.createElement("span");
      meta.className = "muted small";
      meta.textContent = ` — ${relationText(e, n.id)} · ${STATUS_LONG[e.status] || e.status}`;
      li.appendChild(meta);
      const ev = (e.evidence || [])[0];
      if (ev) {
        const q = document.createElement("blockquote");
        q.textContent = `“${ev.quote}”`;
        const cite = document.createElement("span");
        cite.textContent = ` (${ev.passage})`;
        q.appendChild(cite);
        li.appendChild(q);
      }
      rel.appendChild(li);
    }
  }

  function openPanel(id) {
    const n = byId.get(id);
    if (!n) return;
    lastFocus = document.activeElement;
    selected = id;
    fillPanel(n);
    panel.removeAttribute("inert");
    panel.setAttribute("aria-hidden", "false");
    panel.classList.add("open");
    panel.scrollTop = 0;
    $("close").focus();
  }

  function closePanel() {
    if (!panel.classList.contains("open")) return;
    panel.classList.remove("open");
    panel.setAttribute("inert", "");
    panel.setAttribute("aria-hidden", "true");
    selected = null;
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  // ---- start -------------------------------------------------------------

  function loop(t) {
    lastT = t;
    draw(t);
    requestAnimationFrame(loop);
  }

  $("search-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    search($("q").value);
  });

  $("close").addEventListener("click", closePanel);

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closePanel();
  });

  const exWrap = $("examples");
  for (const ex of EXAMPLES) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = ex;
    b.addEventListener("click", () => {
      $("q").value = ex;
      search(ex);
    });
    exWrap.appendChild(b);
  }

  if (window.ResizeObserver) {
    new ResizeObserver(() => resize()).observe(stage);
  } else {
    window.addEventListener("resize", resize);
  }

  const initial = new URLSearchParams(location.search).get("q");
  if (initial) {
    $("q").value = initial;
    search(initial);
  }

  resize();
  requestAnimationFrame(loop);
})();
