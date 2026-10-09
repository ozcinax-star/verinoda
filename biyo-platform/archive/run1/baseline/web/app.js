// Frontend: arama kutusu, 2D nöron benzeri konu ağı ve sağdan kayan detay paneli.
// Veri yalnızca /api/graph?q=... üzerinden gelir (EBA entegrasyonu için tek sınır).

const SVG_NS = "http://www.w3.org/2000/svg";
const CENTER = { x: 500, y: 350 };
const TYPE_ORDER = { onkosul: 0, destek: 1, ortak: 2 };
const TYPE_LABEL = { onkosul: "ön koşul", destek: "destek", ortak: "ortak kavram" };
const STATUS_LABEL = {
  verified: "doğrulanmış (kanıt cümlesi pasajda bulundu)",
  inference: "çıkarım (kanıt cümlesi bulunamadı)",
  unknown: "bilinmiyor",
};

const form = document.getElementById("search-form");
const input = document.getElementById("q");
const statusEl = document.getElementById("status");
const svg = document.getElementById("graph");
const panel = document.getElementById("panel");
const panelTitle = document.getElementById("panel-title");
const panelBody = document.getElementById("panel-body");
const panelClose = document.getElementById("panel-close");

let current = null;

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function labelFor(node) {
  return `${node.title} (${node.grades.join("-")})`;
}

function setupDefs() {
  const defs = svgEl("defs");
  const glow = svgEl("filter", { id: "glow", x: "-50%", y: "-50%", width: "200%", height: "200%" });
  glow.appendChild(svgEl("feGaussianBlur", { stdDeviation: "4", result: "blur" }));
  const merge = svgEl("feMerge");
  merge.appendChild(svgEl("feMergeNode", { in: "blur" }));
  merge.appendChild(svgEl("feMergeNode", { in: "SourceGraphic" }));
  glow.appendChild(merge);
  defs.appendChild(glow);

  const marker = svgEl("marker", {
    id: "arrow-onkosul", viewBox: "0 0 10 10", refX: "9", refY: "5",
    markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
  });
  marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#ffb347" }));
  defs.appendChild(marker);
  svg.appendChild(defs);
}

function layout(graph) {
  const positions = { [graph.center]: { ...CENTER } };
  const neighbours = graph.nodes.filter((n) => n.id !== graph.center);
  const groupOf = {};
  for (const e of graph.edges) {
    const other = e.source === graph.center ? e.target : e.source;
    const rank = TYPE_ORDER[e.type];
    if (groupOf[other] === undefined || rank < groupOf[other]) groupOf[other] = rank;
  }
  neighbours.sort((a, b) => (groupOf[a.id] ?? 9) - (groupOf[b.id] ?? 9) || a.title.localeCompare(b.title, "tr"));
  const radius = neighbours.length > 6 ? 280 : 240;
  neighbours.forEach((n, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(neighbours.length, 1);
    positions[n.id] = { x: CENTER.x + radius * Math.cos(angle), y: CENTER.y + radius * Math.sin(angle) * 0.9 };
  });
  return positions;
}

function drawEdge(edge, pos) {
  const a = pos[edge.source];
  const b = pos[edge.target];
  if (!a || !b) return;
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const bend = 0.16 * len;
  const cx = mx - (dy / len) * bend;
  const cy = my + (dx / len) * bend;
  const classes = ["edge", edge.type, `status-${edge.status}`];
  const path = svgEl("path", {
    d: `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`,
    class: classes.join(" "),
  });
  if (edge.type === "onkosul") path.setAttribute("marker-end", "url(#arrow-onkosul)");
  const title = svgEl("title");
  title.textContent = `${TYPE_LABEL[edge.type]} · ${STATUS_LABEL[edge.status]}`;
  path.appendChild(title);
  svg.appendChild(path);
}

function drawNode(node, pos, isCenter) {
  const g = svgEl("g", { class: `node${isCenter ? " center" : ""}`, tabindex: "0", role: "button" });
  g.setAttribute("aria-label", labelFor(node));
  g.appendChild(svgEl("circle", {
    cx: pos.x, cy: pos.y, r: isCenter ? 36 : 24, class: isCenter ? "core" : "leaf",
  }));
  const text = svgEl("text", { x: pos.x, y: pos.y + (isCenter ? 58 : 44) });
  text.textContent = labelFor(node);
  g.appendChild(text);
  g.addEventListener("click", () => openPanel(node.id));
  g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openPanel(node.id); });
  svg.appendChild(g);
}

function render(graph) {
  svg.textContent = "";
  setupDefs();
  if (!graph.center) return;
  const pos = layout(graph);
  for (const e of graph.edges) drawEdge(e, pos);
  for (const n of graph.nodes) drawNode(n, pos[n.id], n.id === graph.center);
}

function sectionList(title, items) {
  const wrap = el("div");
  wrap.appendChild(el("h3", {}, title));
  const ul = el("ul");
  for (const item of items) ul.appendChild(item);
  wrap.appendChild(ul);
  return wrap;
}

function openPanel(id) {
  const node = current.nodes.find((n) => n.id === id);
  if (!node) return;
  panelTitle.textContent = labelFor(node);
  panelBody.textContent = "";

  const summary = el("div");
  summary.appendChild(el("h3", {}, "Özet"));
  summary.appendChild(el("p", {}, node.summary));
  panelBody.appendChild(summary);

  panelBody.appendChild(sectionList("Nerelerde kullanılır", node.uses.map((u) => el("li", {}, u))));

  const sources = node.study.sources.map((s) => {
    const li = el("li");
    li.appendChild(document.createTextNode(s.ad));
    li.appendChild(el("span", { class: "badge" }, s.tur));
    if (!s.verified) li.appendChild(el("span", { class: "badge" }, "doğrulanmadı"));
    return li;
  });
  panelBody.appendChild(sectionList("Hangi kaynaklardan çalışılmalı (MEB önce)", sources));

  const study = el("div");
  study.appendChild(el("h3", {}, "Ne zaman / hangi sırayla çalışılmalı"));
  study.appendChild(el("p", {}, `Genel sıra: ${node.study.order}. ${node.study.why}`));
  panelBody.appendChild(study);

  const rels = current.edges
    .filter((e) => e.source === id || e.target === id)
    .map((e) => {
      const otherId = e.source === id ? e.target : e.source;
      const other = current.nodes.find((n) => n.id === otherId);
      const li = el("li");
      li.appendChild(document.createTextNode(`${TYPE_LABEL[e.type]}: ${other ? other.title : otherId} `));
      li.appendChild(el("span", { class: "rel-status" }, `(${STATUS_LABEL[e.status]})`));
      return li;
    });
  panelBody.appendChild(sectionList("İlişkiler", rels.length ? rels : [el("li", {}, "Kayıtlı ilişki yok.")]));

  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  panelClose.focus();
}

function closePanel() {
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
}

async function search(q) {
  statusEl.textContent = "Aranıyor...";
  let data;
  try {
    const res = await fetch(`/api/graph?q=${encodeURIComponent(q)}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  } catch (err) {
    statusEl.textContent = `Arama yapılamadı: ${err.message}`;
    return;
  }
  current = data;
  closePanel();
  render(data);
  if (!data.center) {
    statusEl.textContent = data.unknowns[0] || "Konu bulunamadı.";
    return;
  }
  const center = data.nodes.find((n) => n.id === data.center);
  statusEl.textContent = `${center.title}: ${data.edges.length} ilişki. ${data.unknowns.join(" ")}`;
}

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (q) search(q);
});

panelClose.addEventListener("click", closePanel);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closePanel();
});

statusEl.textContent = "Bir konu yazın, örneğin mitoz, fotosentez ya da enzimler.";
